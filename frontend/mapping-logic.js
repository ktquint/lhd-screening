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
let markers = L.layerGroup();
let activeRiverFilter = { river: '', state: '' };

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
            if (val.length < 2) return;

            const matches = allDams.filter(d => {
                const name = (d.Dam_Name || '').toLowerCase();
                const city = (d.City || '').toLowerCase();
                const state = (d['State Abbreviation'] || '').toLowerCase();
                return name.includes(val) || city.includes(val) || state.includes(val);
            }).slice(0, 10);

            matches.forEach(dam => {
                const div = document.createElement('div');
                div.style.padding = '5px';
                div.style.cursor = 'pointer';
                div.style.borderBottom = '1px solid #eee';
                div.style.fontSize = '12px';

                const place = (dam.City && dam.City.trim()) || (dam['County Name'] && dam['County Name'].trim()) || '';
                const loc = [place, dam['State Abbreviation']].filter(Boolean).join(', ');
                div.innerHTML = `<strong>${dam.Dam_Name}</strong><br><span style="color:#666;">${loc}</span>`;

                div.onmouseover = () => div.style.backgroundColor = '#f0f0f0';
                div.onmouseout = () => div.style.backgroundColor = 'white';

                div.onclick = () => {
                    const lat = parseFloat(dam.Latitude);
                    const lng = parseFloat(dam.Longitude);
                    if (!isNaN(lat) && !isNaN(lng)) {
                        map.setView([lat, lng], 12);
                        markers.eachLayer(l => {
                            if (l.getLatLng().lat === lat && l.getLatLng().lng === lng) l.openPopup();
                        });
                    }
                    input.value = dam.Dam_Name;
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
        riverInput.type = 'text';
        riverInput.placeholder = 'River name (e.g. South Platte)';
        riverInput.style.cssText = 'width:100%;padding:6px;box-sizing:border-box;border:1px solid #ccc;border-radius:3px;margin-bottom:4px;font-size:12px;';

        const stateInput = L.DomUtil.create('input', '', panel);
        stateInput.type = 'text';
        stateInput.maxLength = 2;
        stateInput.placeholder = 'State abbr. (e.g. CO)';
        stateInput.style.cssText = 'width:100%;padding:6px;box-sizing:border-box;border:1px solid #ccc;border-radius:3px;margin-bottom:6px;font-size:12px;text-transform:uppercase;';

        const btnRow = L.DomUtil.create('div', '', panel);
        btnRow.style.cssText = 'display:flex;gap:4px;margin-bottom:4px;';

        const applyBtn = L.DomUtil.create('button', '', btnRow);
        applyBtn.textContent = 'Apply Filter';
        applyBtn.style.cssText = 'flex:1;padding:6px;background:#3498db;color:white;border:none;border-radius:3px;cursor:pointer;font-size:12px;font-weight:600;';

        const clearBtn = L.DomUtil.create('button', '', btnRow);
        clearBtn.textContent = 'Clear';
        clearBtn.style.cssText = 'padding:6px 10px;background:#95a5a6;color:white;border:none;border-radius:3px;cursor:pointer;font-size:12px;';

        const filterStatus = L.DomUtil.create('div', '', panel);
        filterStatus.style.cssText = 'font-size:11px;color:#666;min-height:15px;';

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
L.control.layers(baseMaps).addTo(map);

// --- Background Hydrography (.gpkg) Loading ---
const hydroFiles = [
    'streams_702.gpkg', 'streams_703.gpkg', 'streams_704.gpkg',
    'streams_706.gpkg', 'streams_709.gpkg', 'streams_712.gpkg',
    'streams_713.gpkg', 'streams_714.gpkg', 'streams_715.gpkg'
];

async function loadHydrography() {
    hydroFiles.forEach(filename => {
        try {
            L.geoPackageFeatureLayer([], {
                geoPackageUrl: `hydrography/${filename}`,
                layerName: 'features', 
                style: { color: '#3498db', weight: 1.2, opacity: 1.0 }
            }).addTo(map);
        } catch (err) {
            console.warn(`Could not load background layer ${filename}:`, err);
        }
    });
}

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

            const qMinVal = Math.round(parseFloat(dam.Qmin));
            const qMaxVal = Math.round(parseFloat(dam.Qmax));
            const hasComid = dam.Reach_ID !== undefined && dam.Reach_ID !== null && String(dam.Reach_ID).trim() !== '';
            const hasSafetyData = !isNaN(qMinVal) && hasComid;

            if (showOnlyFatality && fatalities === 0) return;

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
            const fatalities = parseInt(dam.NumberOfFatalities) || 0;
            
            const marker = L.circleMarker([lat, lng], {
                radius: 6,
                fillColor: fatalities > 0 ? '#e74c3c' : '#3498db',
                color: 'white',
                weight: 2,
                opacity: 1,
                fillOpacity: 1
            });

            let popupContent = `
                <div class="popup-content">
                    <strong>${dam.Dam_Name}</strong><br>
                    <b>Location:</b> ${location}<br>
                    <b>Fatalities:</b> ${fatalities}<br>
                    <hr>`;
            
            if (hasComid) {
                if (hasSafetyData) {
                    popupContent += `<b>Dangerous Range:</b> ${qMinVal} - ${qMaxVal} cfs<br>`;
                }
                const safetyArgs = hasSafetyData ? `${qMinVal}, ${qMaxVal}` : `null, null`;
                popupContent += `
                    <button class="btn-check" onclick="checkForecast('${dam.Reach_ID}', ${safetyArgs}, '${dam.Dam_Name}')">
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

    // Show modal + spinner immediately
    document.getElementById('statusDisplay').innerHTML = '';
    document.getElementById('forecastSpinner').style.display = 'block';
    document.getElementById('forecastChart').style.display = 'none';
    document.getElementById('forecastModal').style.display = 'block';
    if (forecastChart) { forecastChart.destroy(); forecastChart = null; }

    try {
        const mrData = await fetch(
            `https://api.water.noaa.gov/nwps/v1/reaches/${comid}/streamflow?series=medium_range`
        ).then(r => r.json());

        const mrMean    = mrData.mediumRange?.mean?.data ?? [];
        const mrMembers = ['member1','member2','member3','member4','member5','member6']
            .map(k => mrData.mediumRange?.[k]?.data ?? []);

        const allPoints = mrMean.map((p, i) => ({
            validTime: p.validTime,
            flow:  p.flow,
            upper: Math.max(...mrMembers.map(m => m[i]?.flow ?? p.flow)),
            lower: Math.min(...mrMembers.map(m => m[i]?.flow ?? p.flow))
        }));

        if (allPoints.length === 0) {
            document.getElementById('forecastSpinner').style.display = 'none';
            document.getElementById('statusDisplay').innerHTML =
                `<strong>${damName}</strong><br>No forecast data returned for reach ${comid}.`;
            return;
        }

        _renderForecastChart(allPoints, hasSafetyRange, qMin, qMax, damName);

    } catch (err) {
        console.error("NWM API Error:", err);
        document.getElementById('forecastSpinner').style.display = 'none';
        document.getElementById('statusDisplay').innerHTML =
            `<strong>${damName}</strong><br>Error fetching NWM forecast data.`;
    }
}

function _renderForecastChart(allPoints, hasSafetyRange, qMin, qMax, damName) {
    const allFlow  = allPoints.map(p => p.flow);
    const allUpper = allPoints.map(p => p.upper);
    const allLower = allPoints.map(p => p.lower);
    const currentCfs = allFlow[0];
    const labels = allPoints.map(p =>
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
        datasets.push(
            { label: 'Max Dangerous Flow',   data: Array(allFlow.length).fill(qMax), borderColor: '#e74c3c', borderDash: [10, 5], borderWidth: 2, pointRadius: 0, fill: false },
            { label: 'Dangerous Flow Range', data: Array(allFlow.length).fill(qMin), borderColor: '#e74c3c', borderDash: [10, 5], borderWidth: 2, pointRadius: 0, fill: 0, backgroundColor: 'rgba(231, 76, 60, 0.2)' }
        );
    }
    datasets.push(
        { label: 'NWM Forecast (cfs)', data: allFlow, borderColor: '#000000', backgroundColor: 'transparent', fill: false, tension: 0.2, borderWidth: 3, pointRadius: 0 },
        { label: 'Ensemble Upper', data: allUpper, borderColor: 'rgba(52, 152, 219, 0.5)', borderWidth: 1, pointRadius: 0, fill: '+1', backgroundColor: 'rgba(52, 152, 219, 0.2)', tension: 0.2 },
        { label: 'Ensemble Lower', data: allLower, borderColor: 'rgba(52, 152, 219, 0.5)', borderWidth: 1, pointRadius: 0, fill: false, tension: 0.2 }
    );

    const ctx = document.getElementById('forecastChart').getContext('2d');
    if (forecastChart) forecastChart.destroy();
    forecastChart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            plugins: { filler: { propagate: true } },
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
        <i style="background: #3498db"></i> No Recorded Fatalities<br>
        <div class="filter-section" style="border-top: 1px solid #ccc; margin-top: 8px; padding-top: 8px;">
            <label style="cursor: pointer;">
                <input type="checkbox" id="fatalityFilter"> Show only fatality sites
            </label>
        </div>
    `;

    L.DomEvent.disableClickPropagation(div);
    
    setTimeout(() => {
        const filterCheckbox = document.getElementById('fatalityFilter');
        if (filterCheckbox) {
            filterCheckbox.addEventListener('change', renderMarkers);
        }
    }, 0);

    return div;
};
legend.addTo(map);

window.addEventListener('resize', () => { map.invalidateSize(); });
loadDams();
loadHydrography();

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