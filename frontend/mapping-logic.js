// --- Disclaimer (persisted for the session) ---
(function () {
    const STORAGE_KEY = 'lhdi_disclaimer_accepted';
    const overlay = document.getElementById('disclaimer-overlay');
    if (!overlay) return;
    if (sessionStorage.getItem(STORAGE_KEY) === '1') {
        overlay.style.display = 'none';
    }
    const agreeBtn = document.getElementById('disclaimer-agree');
    if (agreeBtn) {
        agreeBtn.addEventListener('click', () => {
            sessionStorage.setItem(STORAGE_KEY, '1');
            overlay.style.display = 'none';
        });
    }
})();

// 1. Initialize Map with multiple base layers
let forecastChart;
let allDams = [];
let _forecastState = null; // { allPoints, hasSafetyRange, qMin, qMax, damName }
let markers = L.layerGroup();
let activeRiverFilter = { river: '', state: '' };

// Display-name resolver: prefer OSM_Name; fall back to Dam_Name; finally a placeholder.
// Also returns whether the underlying Dam_Name was "generic" (so callers can decide
// search/UI behavior).
const GENERIC_NAME_RE = /^(wade unspecified|low-?head dam( \(\d+\))?|lhd|added-?\d*|control structure( ?#?\d+)?|dam|unnamed|n\/?a)$/i;
function isGenericName(n) {
    if (!n) return true;
    return GENERIC_NAME_RE.test(String(n).trim());
}
function displayName(dam) {
    const osm = (dam.OSM_Name || '').trim();
    if (osm) return osm;
    const orig = (dam.Dam_Name || '').trim();
    if (orig && !isGenericName(orig)) return orig;
    if (orig) return orig; // keep generic placeholder visible if no better option
    return 'Unnamed Dam';
}
// Safely embed a JS string literal inside an HTML double-quoted attribute (onclick=...).
// JSON.stringify handles JS-string escaping (\, ", control chars), then we HTML-encode
// any chars that would break the attribute.
function jsAttrLiteral(s) {
    return JSON.stringify(String(s))
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors'
});

const satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    attribution: 'Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community'
});

const terrain = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
    attribution: 'Map data: &copy; OpenStreetMap contributors, SRTM | Map style: &copy; OpenTopoMap (CC-BY-SA)'
});

const map = L.map('map', {
    preferCanvas: true,
    layers: [osm] // Default layer
}).setView([39.82, -98.57], 4);

const NHD_BLUE = [0, 102, 204, 255];   // R,G,B,A 0-255
const flowlineSymbol = (width) => ({
    type: 'simple',
    symbol: { type: 'esriSLS', style: 'esriSLSSolid', color: NHD_BLUE, width }
});
const nhdFlowlines = L.esri.dynamicMapLayer({
    url: 'https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer',
    dynamicLayers: [
        { id: 104, source: { type: 'mapLayer', mapLayerId: 4 }, drawingInfo: { renderer: flowlineSymbol(2.0) } },
        { id: 106, source: { type: 'mapLayer', mapLayerId: 6 }, drawingInfo: { renderer: flowlineSymbol(1.5) } }
    ],
    opacity: 1.0,
    attribution: 'Hydrography &copy; USGS NHD'
}).addTo(map);

// Define base maps for the control toggle
const baseMaps = {
    "Street Map": osm,
    "Satellite": satellite,
    "Terrain": terrain
};

// SVG icons for top-left toolbar buttons
const ICON_LOCATION = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;"><circle cx="12" cy="12" r="9"></circle><circle cx="12" cy="12" r="3" fill="currentColor"></circle><line x1="12" y1="1" x2="12" y2="5"></line><line x1="12" y1="19" x2="12" y2="23"></line><line x1="1" y1="12" x2="5" y2="12"></line><line x1="19" y1="12" x2="23" y2="12"></line></svg>';
const ICON_FILTER = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"></polygon></svg>';

// Geolocation: zoom to user's current location
const GeolocationControl = L.Control.extend({
    options: { position: 'topleft' },
    onAdd: function() {
        const container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
        const button = L.DomUtil.create('a', '', container);
        button.href = '#';
        button.title = 'Show my location';
        button.setAttribute('aria-label', 'Show my location');
        button.innerHTML = ICON_LOCATION;

        L.DomEvent.disableClickPropagation(container);
        L.DomEvent.on(button, 'click', (e) => {
            L.DomEvent.preventDefault(e);
            if (!navigator.geolocation) {
                alert('Geolocation is not supported by your browser.');
                return;
            }
            navigator.geolocation.getCurrentPosition(
                (pos) => map.setView([pos.coords.latitude, pos.coords.longitude], 13),
                (err) => alert('Unable to retrieve your location: ' + err.message)
            );
        });
        return container;
    }
});
map.addControl(new GeolocationControl());

// Search (filter-icon button with collapsible search panel)
const SearchControl = L.Control.extend({
    options: { position: 'topleft' },
    onAdd: function() {
        const container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
        container.style.position = 'relative';

        const button = L.DomUtil.create('a', '', container);
        button.href = '#';
        button.title = 'Search dams';
        button.setAttribute('aria-label', 'Search dams');
        button.innerHTML = ICON_FILTER;

        const panel = L.DomUtil.create('div', '', container);
        panel.style.display = 'none';
        panel.style.position = 'absolute';
        panel.style.top = '0';
        panel.style.left = 'calc(100% + 6px)';
        panel.style.background = 'white';
        panel.style.padding = '8px';
        panel.style.borderRadius = '4px';
        panel.style.boxShadow = '0 1px 5px rgba(0,0,0,0.4)';
        panel.style.minWidth = '240px';

        const input = L.DomUtil.create('input', '', panel);
        input.type = 'text';
        input.placeholder = 'Search dams, city, state...';
        input.style.width = '100%';
        input.style.padding = '6px';
        input.style.boxSizing = 'border-box';
        input.style.border = '1px solid #ccc';
        input.style.borderRadius = '3px';

        const resultsDiv = L.DomUtil.create('div', '', panel);
        resultsDiv.style.maxHeight = '240px';
        resultsDiv.style.overflowY = 'auto';
        resultsDiv.style.marginTop = '6px';

        L.DomEvent.disableClickPropagation(container);
        L.DomEvent.disableScrollPropagation(container);

        L.DomEvent.on(button, 'click', (e) => {
            L.DomEvent.preventDefault(e);
            const isOpen = panel.style.display === 'block';
            panel.style.display = isOpen ? 'none' : 'block';
            if (!isOpen) setTimeout(() => input.focus(), 0);
        });

        input.addEventListener('input', (e) => {
            const val = e.target.value.toLowerCase();
            resultsDiv.innerHTML = '';
            
            // Wait for at least 2 characters before querying
            if (val.length < 2) return;

            // Find all matches across the dataset
            const matches = allDams.filter(d => {
                const name = (d.Dam_Name || '').toLowerCase();
                const osm  = (d.OSM_Name || '').toLowerCase();
                const city = (d.City || '').toLowerCase();
                const state = (d['State Abbreviation'] || '').toLowerCase();
                return name.includes(val) || osm.includes(val) || city.includes(val) || state.includes(val);
            });

            // --- UX UPGRADE: Search Results Counter & Empty State ---
            const countHeader = document.createElement('div');
            countHeader.style.cssText = 'font-size: 11px; font-weight: bold; color: #7f8c8d; padding: 4px 5px 6px; border-bottom: 1px solid #eee; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.03em;';
            
            if (matches.length === 0) {
                countHeader.textContent = 'No matching dams found';
                resultsDiv.appendChild(countHeader);
                return;
            } else {
                const shown = Math.min(10, matches.length);
                countHeader.textContent = `Showing top ${shown} of ${matches.length} results`;
                resultsDiv.appendChild(countHeader);
            }

            // --- UX UPGRADE: Polished List Layout ---
            matches.slice(0, 10).forEach(dam => {
                const div = document.createElement('div');
                div.style.padding = '6px 8px';
                div.style.cursor = 'pointer';
                div.style.borderBottom = '1px solid #f9f9f9';
                div.style.fontSize = '12px';
                div.style.borderRadius = '3px';
                div.style.transition = 'background-color 0.15s ease';

                const place = (dam.City && dam.City.trim()) || (dam['County Name'] && dam['County Name'].trim()) || '';
                const loc = [place, dam['State Abbreviation']].filter(Boolean).join(', ');
                
                div.innerHTML = `<strong>${displayName(dam)}</strong><br><span style="color:#7f8c8d; font-size: 11px;">${loc}</span>`;

                // Subtle blue hover effect instead of harsh gray
                div.onmouseover = () => div.style.backgroundColor = '#f0f4f8';
                div.onmouseout = () => div.style.backgroundColor = 'transparent';

                div.onclick = () => {
                    const lat = parseFloat(dam.Latitude);
                    const lng = parseFloat(dam.Longitude);
                    if (!isNaN(lat) && !isNaN(lng)) {
                        map.setView([lat, lng], 14); // Pushed zoom slightly tighter for context
                        markers.eachLayer(l => {
                            if (l.getLatLng().lat === lat && l.getLatLng().lng === lng) l.openPopup();
                        });
                    }
                    input.value = displayName(dam);
                    panel.style.display = 'none';
                };
                resultsDiv.appendChild(div);
            });
        });

        // --- River Filter section ---
        const divider = L.DomUtil.create('hr', '', panel);
        divider.style.cssText = 'margin:8px 0;border:none;border-top:1px solid #ddd;';

        const riverLabel = L.DomUtil.create('div', '', panel);
        riverLabel.style.cssText = 'font-size:11px;font-weight:bold;color:#555;margin-bottom:5px;letter-spacing:0.03em;';
        riverLabel.textContent = 'FILTER BY RIVER';

        const riverInput = L.DomUtil.create('input', '', panel);
        riverInput.id = 'globalRiverInput';
        riverInput.type = 'text';
        riverInput.placeholder = 'River name (e.g. South Platte)';
        riverInput.style.cssText = 'width:100%;padding:6px;box-sizing:border-box;border:1px solid #ccc;border-radius:3px;margin-bottom:4px;font-size:12px;';

        const stateInput = L.DomUtil.create('input', '', panel);
        stateInput.id = 'globalStateInput';
        stateInput.type = 'text';
        stateInput.maxLength = 2;
        stateInput.placeholder = 'State abbr. (e.g. CO)';
        stateInput.style.cssText = 'width:100%;padding:6px;box-sizing:border-box;border:1px solid #ccc;border-radius:3px;margin-bottom:6px;font-size:12px;text-transform:uppercase;';

        const btnRow = L.DomUtil.create('div', '', panel);
        btnRow.style.cssText = 'display:flex;gap:4px;margin-bottom:4px;';

        const applyBtn = L.DomUtil.create('button', '', btnRow);
        applyBtn.id = 'globalApplyFilterBtn';
        applyBtn.textContent = 'Apply Filter';
        applyBtn.style.cssText = 'flex:1;padding:6px;background:#3498db;color:white;border:none;border-radius:3px;cursor:pointer;font-size:12px;font-weight:600;';

        const clearBtn = L.DomUtil.create('button', '', btnRow);
        clearBtn.textContent = 'Clear';
        clearBtn.style.cssText = 'padding:6px 10px;background:#95a5a6;color:white;border:none;border-radius:3px;cursor:pointer;font-size:12px;';

        const filterStatus = L.DomUtil.create('div', '', panel);
        filterStatus.style.cssText = 'font-size:11px;color:#666;min-height:15px;';

        const filterDivider = L.DomUtil.create('hr', '', panel);
        filterDivider.style.cssText = 'margin:8px 0;border:none;border-top:1px solid #ddd;';

        const optionsLabel = L.DomUtil.create('div', '', panel);
        optionsLabel.style.cssText = 'font-size:11px;font-weight:bold;color:#555;margin-bottom:5px;letter-spacing:0.03em;';
        optionsLabel.textContent = 'FILTER OPTIONS';

        const checkboxLabel = L.DomUtil.create('label', '', panel);
        checkboxLabel.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;user-select:none;';

        const fatalityCheckbox = L.DomUtil.create('input', '', checkboxLabel);
        fatalityCheckbox.type = 'checkbox';
        fatalityCheckbox.id = 'fatalityFilter';
        fatalityCheckbox.style.cursor = 'pointer';

        const checkboxText = document.createTextNode('Show only fatality sites');
        checkboxLabel.appendChild(checkboxText);

        // Listen directly to changes right here in the toolbar control loop
        fatalityCheckbox.addEventListener('change', renderMarkers);

        function _damMatchesRiverFilter(d, rq, sq) {
            if (!d['State Abbreviation']) return false;
            if (rq) {
                const gnis   = (d.GNIS_Name        || '').toLowerCase();
                const stream = (d['River/Stream']  || '').toLowerCase();
                const river  = (d.River            || '').toLowerCase();
                if (!gnis.includes(rq) && !stream.includes(rq) && !river.includes(rq)) return false;
            }
            if (sq && (d['State Abbreviation'] || '').toUpperCase() !== sq) return false;
            return true;
        }

        function applyRiverFilter() {
            const rq = riverInput.value.trim().toLowerCase();
            const sq = stateInput.value.trim().toUpperCase();
            activeRiverFilter = { river: rq, state: sq };
            renderMarkers();

            if (!rq && !sq) {
                filterStatus.textContent = '';
                return;
            }

            const matched = allDams.filter(d => _damMatchesRiverFilter(d, rq, sq));
            const withCoords = matched.filter(d => !isNaN(parseFloat(d.Latitude)) && !isNaN(parseFloat(d.Longitude)));

            filterStatus.textContent = `${withCoords.length} dam${withCoords.length !== 1 ? 's' : ''} matched`;

            if (withCoords.length > 0) {
                const bounds = L.latLngBounds(
                    withCoords.map(d => [parseFloat(d.Latitude), parseFloat(d.Longitude)])
                );
                map.fitBounds(bounds, { padding: [60, 60], maxZoom: 13 });
            }
        }

        L.DomEvent.on(applyBtn, 'click', (e) => { L.DomEvent.preventDefault(e); applyRiverFilter(); });

        L.DomEvent.on(clearBtn, 'click', (e) => {
            L.DomEvent.preventDefault(e);
            riverInput.value = '';
            stateInput.value = '';
            activeRiverFilter = { river: '', state: '' };
            filterStatus.textContent = '';
            renderMarkers();
        });

        L.DomEvent.on(riverInput, 'keydown', (e) => { if (e.key === 'Enter') applyRiverFilter(); });
        L.DomEvent.on(stateInput, 'keydown', (e) => { if (e.key === 'Enter') applyRiverFilter(); });

        return container;
    }
});
map.addControl(new SearchControl());

// Add the background maps button (Layers Control)
const layerControl = L.control.layers(baseMaps).addTo(map);


// 2. Load Dam Data
async function loadDams() {
    try {
        allDams = await d3.csv("data/full_lhd_website.csv");
        renderMarkers();
        console.log("Dam markers clustered and initialized.");
    } catch (err) { 
        console.error("Error loading CSV:", err); 
    }
}

// 3. Render Markers with Filter Logic
function renderMarkers() {
    markers.clearLayers(); 
    const filterEl = document.getElementById('fatalityFilter');
    const showOnlyFatality = filterEl ? filterEl.checked : false;

    allDams.forEach(dam => {
        const lat = parseFloat(dam.Latitude);
        const lng = parseFloat(dam.Longitude);
        
        if (!isNaN(lat) && !isNaN(lng)) {
            // Filter out sites that don't have a US State abbreviation
            if (!dam["State Abbreviation"]) return;

            const fatalities = parseInt(dam.NumberOfFatalities) || 0;

            if (showOnlyFatality && fatalities === 0) return;

            const qMinVal = Math.round(parseFloat(dam.Qmin));
            const qMaxVal = Math.round(parseFloat(dam.Qmax));
            const hasComid = dam.Reach_ID !== undefined && dam.Reach_ID !== null && String(dam.Reach_ID).trim() !== '';
            const hasSafetyData = !isNaN(qMinVal) && hasComid;
            
            // Determine marker color based on data priority tier
            let markerColor = '#95a5a6'; // Default: Gray (Missing hydro link / screening safety data)
            if (fatalities > 0) {
                markerColor = '#e74c3c'; // Priority 1: Red (Fatality recorded)
            } else if (hasComid) {
                markerColor = '#3498db'; // Priority 2: Blue (Live forecast + screening active)
            }


            // River / stream name filter (searches GNIS_Name then River/Stream as fallback)
            const riverQ = activeRiverFilter.river.toLowerCase();
            const stateQ  = activeRiverFilter.state.toUpperCase();
            if (riverQ) {
                const gnis   = (dam.GNIS_Name        || '').toLowerCase();
                const stream = (dam['River/Stream']  || '').toLowerCase();
                const river  = (dam.River            || '').toLowerCase();
                if (!gnis.includes(riverQ) && !stream.includes(riverQ) && !river.includes(riverQ)) return;
            }
            if (stateQ && (dam['State Abbreviation'] || '').toUpperCase() !== stateQ) return;

            const place = (dam.City && dam.City.trim()) || (dam["County Name"] && dam["County Name"].trim()) || "Unknown location";
            const state = dam["State Abbreviation"] || "";
            const location = place + (state ? `, ${state}` : "");
            
            const marker = L.circleMarker([lat, lng], {
                radius: 6,
                fillColor: markerColor,
                color: 'white',
                weight: 2,
                opacity: 1,
                fillOpacity: 1
            });

            const _displayName = displayName(dam);
            let popupContent = `
                <div class="popup-content">
                    <strong>${_displayName}</strong><br>
                    <b>Location:</b> ${location}<br>
                    <b>Fatalities:</b> ${fatalities}<br>
                    <hr>`;
            
            if (hasComid) {
                if (hasSafetyData) {
                    popupContent += `<b>Dangerous Range:</b> ${qMinVal} - ${qMaxVal} cfs<br>`;
                }
                const safetyArgs = hasSafetyData ? `${qMinVal}, ${qMaxVal}` : `null, null`;
                popupContent += `
                    <button class="btn-check" onclick="checkForecast('${dam.Reach_ID}', ${safetyArgs}, ${jsAttrLiteral(_displayName)})">
                        Check Live Forecast
                    </button>`;
            } else {
                popupContent += `<i>No forecast available: this site is not linked to an NHDPlus stream reach.</i>`;
            }

            popupContent += `</div>`;
            marker.bindPopup(popupContent);
            markers.addLayer(marker); 
        }
    });
    
    map.addLayer(markers); 
}

// 4. National Water Model Forecast (NOAA NWPS API, NHDPlus V2 COMID = Reach_ID)
// Medium-range only: ~10d 3-hourly ensemble mean + member spread as uncertainty band.
// Units: API returns ft³/s (cfs) — no conversion needed.
async function checkForecast(comid, qMin, qMax, damName) {
    const hasSafetyRange = qMin !== null && !isNaN(qMin) && qMax !== null && !isNaN(qMax);

    // FIX: Clear out the old header immediately so it doesn't distract the user while loading
    document.getElementById('statusDisplay').innerHTML = `<strong>${damName}</strong><br><span style="color:#888;">Loading forecast details...</span>`;

    // Show modal + spinner immediately
    document.getElementById('forecastSpinner').style.display = 'block';
    document.getElementById('forecastChart').style.display = 'none';
    document.getElementById('forecastModal').classList.add('is-open');
    if (forecastChart) { forecastChart.destroy(); forecastChart = null; }

    try {
        const mrData = await fetch(
            `https://api.water.noaa.gov/nwps/v1/reaches/${comid}/streamflow?series=medium_range`
        ).then(r => r.json());

        const mrMean    = mrData.mediumRange?.mean?.data ?? [];
        const mrMembers = ['member1','member2','member3','member4','member5','member6']
            .map(k => mrData.mediumRange?.[k]?.data ?? []);

        // Floor current time to the nearest hour
        const nowFloor = new Date();
        nowFloor.setMinutes(0, 0, 0);
        const nowMs = nowFloor.getTime();

        const rawPoints = mrMean.map((p, i) => ({
            validTime: p.validTime,
            validMs:   new Date(p.validTime).getTime(),
            flow:  p.flow,
            upper: Math.max(...mrMembers.map(m => m[i]?.flow ?? p.flow)),
            lower: Math.min(...mrMembers.map(m => m[i]?.flow ?? p.flow))
        }));

        // Interpolate a synthetic point at the current floored hour
        const afterIdx = rawPoints.findIndex(p => p.validMs > nowMs);
        let allPoints;
        if (afterIdx > 0) {
            const p0 = rawPoints[afterIdx - 1];
            const p1 = rawPoints[afterIdx];
            const t  = (nowMs - p0.validMs) / (p1.validMs - p0.validMs);
            const lerp = (a, b) => a + t * (b - a);
            const synthetic = {
                validTime: nowFloor.toISOString(),
                validMs:   nowMs,
                flow:  lerp(p0.flow,  p1.flow),
                upper: lerp(p0.upper, p1.upper),
                lower: lerp(p0.lower, p1.lower)
            };
            allPoints = [synthetic, ...rawPoints.filter(p => p.validMs > nowMs)];
        } else {
            allPoints = rawPoints.filter(p => p.validMs >= nowMs);
        }

        if (allPoints.length === 0) {
            document.getElementById('forecastSpinner').style.display = 'none';
            document.getElementById('statusDisplay').innerHTML =
                `<strong>${damName}</strong><br>No forecast data returned for reach ${comid}.`;
            return;
        }

        _forecastState = { allPoints, hasSafetyRange, qMin, qMax, damName };

        const slider = document.getElementById('forecastSlider');
        const maxDays = Math.floor(
            (new Date(allPoints[allPoints.length - 1].validTime) - new Date(allPoints[0].validTime)) / 86400000
        );
        slider.max = maxDays;
        slider.value = Math.min(5, maxDays);
        document.getElementById('forecastSliderWrap').style.display = 'block';

        _renderForecastChart(allPoints, hasSafetyRange, qMin, qMax, damName, parseInt(slider.value));

    } catch (err) {
        console.error("NWM API Error:", err);
        document.getElementById('forecastSpinner').style.display = 'none';
        document.getElementById('statusDisplay').innerHTML =
            `<strong>${damName}</strong><br>Error fetching NWM forecast data.`;
    }
}

function _renderForecastChart(allPoints, hasSafetyRange, qMin, qMax, damName, days = 5) {
    const cutoff = new Date(allPoints[0].validTime).getTime() + days * 86400000;
    const points = allPoints.filter(p => new Date(p.validTime).getTime() <= cutoff);
    const allFlow  = points.map(p => p.flow);
    const allUpper = points.map(p => p.upper);
    const allLower = points.map(p => p.lower);
    const currentCfs = allFlow[0];
    const labels = points.map(p =>
        new Date(p.validTime).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit' })
    );

    let statusText = `<strong>${damName}</strong><br>`;
    statusText += `Current Forecast: ${currentCfs != null ? currentCfs.toFixed(0) : 'N/A'} cfs`;

    if (hasSafetyRange) {
        statusText += ` | Dangerous Range: ${qMin.toFixed(0)}-${qMax.toFixed(0)} cfs<br>`;
        const isAnyDangerous = allFlow.some(v => v != null && v >= qMin && v <= qMax);
        statusText += isAnyDangerous
            ? `<span style="color:red; font-weight:bold;">⚠️ WARNING: DANGEROUS CONDITIONS FORECASTED ⚠️</span>`
            : `<span style="color:green; font-weight:bold;">✅ Status: Safe for Forecast Period</span>`;
    } else {
        statusText += `<br><span style="color:#555;">No dangerous flow range on record for this site.</span>`;
    }
    document.getElementById('statusDisplay').innerHTML = statusText;
    document.getElementById('forecastSpinner').style.display = 'none';
    document.getElementById('forecastChart').style.display = 'block';

    const datasets = [];
    if (hasSafetyRange) {
        // Red filled box for the legend, bounded by a thicker, dashed line
        datasets.push({
            label: 'Dangerous Flow Range',
            data: Array(allFlow.length).fill(qMax), 
            borderColor: '#e74c3c', 
            borderWidth: 3,             // Thicker line
            borderDash: [8, 4],         // Dashed formatting
            pointRadius: 0, 
            fill: {
                target: {value: qMin},
                above: 'rgba(231, 76, 60, 0.25)',
                below: 'rgba(231, 76, 60, 0.25)'
            },
            backgroundColor: 'rgba(231, 76, 60, 0.25)',
            pointStyle: 'rect'
        });
    }
    
    datasets.push(
        // Core National Water Model Forecast Trend line (cfs removed from label)
        { 
            label: 'NWM Forecast', 
            data: allFlow, 
            borderColor: '#000000', 
            backgroundColor: '#000000', 
            fill: false, 
            tension: 0.2, 
            borderWidth: 3, 
            pointRadius: 0,
            pointStyle: 'line'
        },
        // Upper Bound tracker representing Flow Uncertainty (Ensemble string stripped)
        { 
            label: 'Flow Uncertainty', 
            data: allUpper, 
            borderColor: '#3498db', 
            borderWidth: 1, 
            pointRadius: 0, 
            fill: '+1', // FIX: Forces the blue fill to stretch all the way down to the next dataset (Lower Bound)
            backgroundColor: 'rgba(52, 152, 219, 0.25)', 
            tension: 0.2,
            pointStyle: 'rect'
        },
        // Lower Bound tracker used explicitly to catch the bottom edge of the uncertainty fill
        { 
            label: 'Uncertainty Lower Bound', 
            data: allLower, 
            borderColor: '#3498db', 
            borderWidth: 1, 
            pointRadius: 0, 
            fill: false, 
            tension: 0.2,
            showLine: true,
            plugins: {
                legend: {
                    display: false 
                }
            }
        }
    );

    const ctx = document.getElementById('forecastChart').getContext('2d');
    if (forecastChart) forecastChart.destroy();
    forecastChart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                filler: { propagate: true },
                legend: {
                    labels: {
                        usePointStyle: true,
                        filter: function(item) {
                            // Suppress the raw lower boundary line from appearing in the legend item array
                            return item.text !== 'Uncertainty Lower Bound';
                        }
                    }
                }
            },
            scales: {
                y: {
                    title: { display: true, text: 'Streamflow (cfs)' },
                    grace: '10%'
                },
                x: { ticks: { maxTicksLimit: 10 } }
            }
        }
    });
}

// 5. Legend and Filter Integration
const legend = L.control({ position: 'bottomright' });
legend.onAdd = function (map) {
    const div = L.DomUtil.create('div', 'info legend');
    div.innerHTML = `
        <strong>Dam Status</strong><br>
        <i style="background: #e74c3c"></i> Fatality Recorded<br>
        <i style="background: #3498db"></i> Live Forecast Available<br>
        <i style="background: #95a5a6"></i> Location Info Only
    `;

    L.DomEvent.disableClickPropagation(div);
    return div;
};
legend.addTo(map);

window.addEventListener('resize', () => { map.invalidateSize(); });

// --- Draggable + resizable forecast panel ---
function closeForecastPanel() {
    document.getElementById('forecastModal').classList.remove('is-open');
    document.getElementById('forecastSliderWrap').style.display = 'none';
}
window.closeForecastPanel = closeForecastPanel;

(function enableForecastDrag() {
    const panel  = document.getElementById('forecastModal');
    const handle = document.getElementById('forecastModalHeader');
    if (!panel || !handle) return;

    let dragging = false;
    let startX = 0, startY = 0, startLeft = 0, startTop = 0;

    function pinAbsolute() {
        // After the first drag/resize, replace `left:50%; transform:translateX(-50%)`
        // with concrete pixel left/top so drag math is straightforward.
        const r = panel.getBoundingClientRect();
        panel.style.left = r.left + 'px';
        panel.style.top  = r.top  + 'px';
        panel.style.transform = 'none';
        panel.style.margin = '0';
    }

    handle.addEventListener('mousedown', (e) => {
        if (e.target.closest('.close')) return;       // don't drag when clicking X
        e.preventDefault();
        pinAbsolute();
        dragging = true;
        startX = e.clientX; startY = e.clientY;
        const r = panel.getBoundingClientRect();
        startLeft = r.left; startTop = r.top;
        document.body.style.userSelect = 'none';
    });

    window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const dx = e.clientX - startX;
        const dy = e.clientY - startY;
        const r = panel.getBoundingClientRect();
        // clamp so the header stays on-screen
        const minLeft = -r.width + 80;
        const maxLeft = window.innerWidth - 80;
        const minTop  = 0;
        const maxTop  = window.innerHeight - 30;
        panel.style.left = Math.max(minLeft, Math.min(maxLeft, startLeft + dx)) + 'px';
        panel.style.top  = Math.max(minTop,  Math.min(maxTop,  startTop  + dy)) + 'px';
    });

    window.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        document.body.style.userSelect = '';
    });

    // Keep the Chart.js canvas in sync with the resizable panel.
    if (typeof ResizeObserver !== 'undefined') {
        const ro = new ResizeObserver(() => {
            if (typeof forecastChart !== 'undefined' && forecastChart) forecastChart.resize();
        });
        ro.observe(panel);
    }

    // 8-direction resize via edge/corner handles
    const MIN_W = 340, MIN_H = 260;
    let resizing = null;   // { dir, startX, startY, startLeft, startTop, startW, startH }

    panel.querySelectorAll('.resize-handle').forEach((h) => {
        h.addEventListener('mousedown', (e) => {
            e.preventDefault();
            e.stopPropagation();
            pinAbsolute();
            const r = panel.getBoundingClientRect();
            resizing = {
                dir: h.dataset.dir,
                startX: e.clientX, startY: e.clientY,
                startLeft: r.left, startTop: r.top,
                startW: r.width,  startH: r.height,
            };
            document.body.style.userSelect = 'none';
        });
    });

    window.addEventListener('mousemove', (e) => {
        if (!resizing) return;
        const dx = e.clientX - resizing.startX;
        const dy = e.clientY - resizing.startY;
        let { startLeft: L, startTop: T, startW: W, startH: H, dir } = resizing;

        if (dir.includes('e')) W = Math.max(MIN_W, resizing.startW + dx);
        if (dir.includes('s')) H = Math.max(MIN_H, resizing.startH + dy);
        if (dir.includes('w')) {
            const newW = Math.max(MIN_W, resizing.startW - dx);
            L = resizing.startLeft + (resizing.startW - newW);
            W = newW;
        }
        if (dir.includes('n')) {
            const newH = Math.max(MIN_H, resizing.startH - dy);
            T = resizing.startTop + (resizing.startH - newH);
            H = newH;
        }
        // Clamp to viewport
        L = Math.max(0, Math.min(window.innerWidth  - W, L));
        T = Math.max(0, Math.min(window.innerHeight - H, T));

        panel.style.left   = L + 'px';
        panel.style.top    = T + 'px';
        panel.style.width  = W + 'px';
        panel.style.height = H + 'px';
    });

    window.addEventListener('mouseup', () => {
        if (resizing) {
            resizing = null;
            document.body.style.userSelect = '';
        }
    });
})();

document.getElementById('forecastSlider').addEventListener('input', function () {
    const days = parseInt(this.value);
if (_forecastState) {
        const { allPoints, hasSafetyRange, qMin, qMax, damName } = _forecastState;
        _renderForecastChart(allPoints, hasSafetyRange, qMin, qMax, damName, days);
    }
});

loadDams();

// --- Tab navigation ---
(function setupTabs() {
    const buttons = document.querySelectorAll('.nav-button[data-tab]');
    const panels = document.querySelectorAll('.tab-panel');

    function activate(tabName) {
        buttons.forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tabName));
        panels.forEach(p => p.classList.toggle('active', p.id === `tab-${tabName}`));
        if (tabName === 'forecasts') {
            setTimeout(() => map.invalidateSize(), 0);
        }
    }

    buttons.forEach(btn => {
        btn.addEventListener('click', () => activate(btn.dataset.tab));
    });
})();

// --- CFD Toolbox Sidebar Scroll-Spy Logic ---
(function setupCFDScrollSpy() {
    const tabContainer = document.getElementById('tab-cfd-toolbox');
    if (!tabContainer) return;

    // Listen for clicks on the sidebar to smooth scroll to the section
    const navLinks = document.querySelectorAll('.cfd-sidebar a');
    navLinks.forEach(link => {
        link.addEventListener('click', function(e) {
            e.preventDefault();
            const targetId = this.getAttribute('href').substring(1);
            const targetElement = document.getElementById(targetId);
            
            if (targetElement) {
                // Scroll the tab container to the element's offset
                tabContainer.scrollTo({
                    top: targetElement.offsetTop - 20, 
                    behavior: 'smooth'
                });
            }
        });
    });

    // Use Intersection Observer to highlight the active link as the user scrolls
    const headings = document.querySelectorAll('.cfd-content h2');
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                // Remove active class from all links
                navLinks.forEach(link => link.classList.remove('active'));
                
                // Add active class to the link matching this heading
                const id = entry.target.getAttribute('id');
                const activeLink = document.querySelector(`.cfd-sidebar a[href="#${id}"]`);
                if (activeLink) activeLink.classList.add('active');
            }
        });
    }, {
        root: tabContainer,
        rootMargin: '0px 0px -80% 0px', // Triggers when heading hits the top 20% of the screen
        threshold: 0
    });

    headings.forEach(heading => observer.observe(heading));
})();

// --- Load State Boundaries JSON ---
async function loadStateBoundaries(url) {
    try {
        const response = await fetch(url);
        const data = await response.json();
        
        const stateBoundaryLayer = L.geoJSON(data, {
            style: { color: 'black', weight: 2, fillOpacity: 0.05 },
            onEachFeature: function(feature, layer) {
                // Visual feedback on hover
                layer.on('mouseover', function() {
                    layer.setStyle({ fillOpacity: 0.2, weight: 3 });
                });
                layer.on('mouseout', function() {
                    layer.setStyle({ fillOpacity: 0.05, weight: 2 });
                });
                
                // Click to filter by state
                layer.on('click', function(e) {
                    // Extract state abbreviation (Census shapefiles usually use STUSPS)
                    const stateAbbr = feature.properties.STUSPS || feature.properties.STUSAB || feature.properties.STATE;
                    if (stateAbbr) {
                        const upperAbbr = stateAbbr.toUpperCase();
                        const stateInput = document.getElementById('globalStateInput');
                        const applyBtn = document.getElementById('globalApplyFilterBtn');
                        
                        if (stateInput && applyBtn) {
                            // Toggle filter on/off
                            if (stateInput.value.toUpperCase() === upperAbbr) {
                                stateInput.value = ''; // Toggle off if already selected
                            } else {
                                stateInput.value = upperAbbr; // Toggle on
                            }
                            
                            // Trigger the UI's existing filter logic
                            applyBtn.click();
                            
                            // Adjust zoom to state boundary or reset
                            if (stateInput.value !== '') {
                                setTimeout(() => { map.fitBounds(layer.getBounds()); }, 50);
                            } else {
                                map.setView([39.82, -98.57], 4);
                            }
                        }
                    }
                });
            }
        });
        
        // Add it to the top-right layer control menu
        layerControl.addOverlay(stateBoundaryLayer, "State Boundaries");
        
        // If you want the boundaries to be visible immediately on load, uncomment the next line:
        stateBoundaryLayer.addTo(map);
    } catch (error) {
        console.error(`Failed to load JSON from ${url}:`, error);
    }
}

loadStateBoundaries('data/cb_2025_us_state_20m.json');