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
let ratingCurvesChart;
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
            (pos) => map.flyTo([pos.coords.latitude, pos.coords.longitude], 13, { duration: 1.5 }),
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
        input.id = 'globalSearchInput';
        input.type = 'text';
        input.placeholder = 'Search dams, city, state...';
        input.style.width = '100%';
        input.style.padding = '6px';
        input.style.boxSizing = 'border-box';
        input.style.border = '1px solid #ccc';
        input.style.borderRadius = '3px';

        const resultsDiv = L.DomUtil.create('div', '', panel);
        resultsDiv.id = 'globalSearchResults';
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
                    map.flyTo([lat, lng], 14, { duration: 1.5 }); // Pushed zoom slightly tighter for context
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

            if (typeof window.highlightStateBoundary === 'function') {
                window.highlightStateBoundary(sq);
            }

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
            map.flyToBounds(bounds, { padding: [60, 60], maxZoom: 13, duration: 1.5 });
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
            if (typeof window.clearStateHighlight === 'function') {
                window.clearStateHighlight();
            }
        });

        L.DomEvent.on(riverInput, 'keydown', (e) => { if (e.key === 'Enter') applyRiverFilter(); });
        L.DomEvent.on(stateInput, 'keydown', (e) => { if (e.key === 'Enter') applyRiverFilter(); });

        // Expose function globally to open the panel
        window.openSearchPanel = () => {
            if (panel.style.display !== 'block') {
                panel.style.display = 'block';
                setTimeout(() => input.focus(), 0);
            }
        };

        window.closeSearchPanel = () => {
            if (panel.style.display === 'block') {
                panel.style.display = 'none';
            }
        };

        return container;
    }
});
map.addControl(new SearchControl());

// Add the background maps button (Layers Control)
const layerControl = L.control.layers(baseMaps).addTo(map);

// --- Active Filters Badge ---
const ActiveFiltersControl = L.Control.extend({
    options: { position: 'bottomleft' },
    onAdd: function() {
        const container = L.DomUtil.create('div', 'leaflet-control');
        container.id = 'activeFiltersBadge';
        container.style.cssText = 'display:none; background:rgba(255,255,255,0.95); padding:8px 12px; border-radius:4px; box-shadow:0 1px 5px rgba(0,0,0,0.4); font-size:12px; font-weight:bold; color:#2c3e50; cursor:pointer; border-left:4px solid #3498db; margin-bottom: 20px; transition: all 0.2s ease;';
        container.title = 'Click to clear all filters';
        
        L.DomEvent.disableClickPropagation(container);
        
        container.onmouseover = () => container.style.background = '#f8f9fa';
        container.onmouseout = () => container.style.background = 'rgba(255,255,255,0.95)';
        
        container.onclick = (e) => {
            L.DomEvent.stopPropagation(e);
            // Trigger the global escape key logic to clear everything
            document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
        };
        
        return container;
    }
});
map.addControl(new ActiveFiltersControl());

window.updateActiveFiltersBadge = () => {
    const badge = document.getElementById('activeFiltersBadge');
    if (!badge) return;
    
    const stateInput = document.getElementById('globalStateInput');
    const riverInput = document.getElementById('globalRiverInput');
    const fatalityCheckbox = document.getElementById('fatalityFilter');
    
    let active = [];
    if (stateInput && stateInput.value.trim()) active.push(`State: ${stateInput.value.trim().toUpperCase()}`);
    if (riverInput && riverInput.value.trim()) active.push(`River: ${riverInput.value.trim()}`);
    if (fatalityCheckbox && fatalityCheckbox.checked) active.push(`Fatalities Only`);
    
    if (active.length > 0) {
        badge.style.display = 'block';
        badge.innerHTML = `Active Filters: <span style="font-weight:normal; color:#555;">${active.join(' | ')}</span> <span style="color:#e74c3c; margin-left:12px; border-left: 1px solid #ccc; padding-left: 10px;">&times; Clear All</span>`;
    } else {
        badge.style.display = 'none';
    }
};

// 2. Load Dam Data
async function loadDams() {
    try {
        // Append a timestamp to the URL to force the browser to bypass its cache. This ensures that users always get the most up-to-date data without needing to manually clear their cache. In development, this is crucial to see changes immediately. In production, it guarantees that any updates to the dam data are reflected for all users without delay.
        allDams = await d3.csv(`data/full_lhd_website.csv?v=${new Date().getTime()}`);
        // NWM does not produce forecasts for Hawaii or Puerto Rico, so exclude those dams everywhere.
        const NON_CONUS_STATES = new Set(['HI', 'PR']);
        allDams = allDams.filter(d => !NON_CONUS_STATES.has((d['State Abbreviation'] || '').toUpperCase()));
        renderMarkers();
        console.log("Dam markers clustered and initialized.");
    } catch (err) { 
        console.error("Error loading CSV:", err); 
    }
}

// Helper to scale marker size based on zoom level
function getMarkerRadiusAndWeight() {
    const zoom = map.getZoom();
    if (zoom <= 5) return { radius: 3, weight: 0.5 };
    if (zoom <= 8) return { radius: 4, weight: 0.5 };
    if (zoom <= 11) return { radius: 6, weight: 1.0 };
    return { radius: 8, weight: 1.5 };
}

// Dynamically resize markers whenever the map zoom level changes
map.on('zoomend', () => {
    const { radius, weight } = getMarkerRadiusAndWeight();
    markers.eachLayer(layer => {
        if (layer.setRadius) {
            layer.setRadius(radius);
            layer.setStyle({ weight: weight });
        }
    });
});

// 3. Render Markers with Filter Logic
function renderMarkers() {
    markers.clearLayers(); 
    const filterEl = document.getElementById('fatalityFilter');
    const showOnlyFatality = filterEl ? filterEl.checked : false;
    
    const { radius, weight } = getMarkerRadiusAndWeight();

    allDams.forEach(dam => {
        const lat = parseFloat(dam.Latitude);
        const lng = parseFloat(dam.Longitude);
        
        if (!isNaN(lat) && !isNaN(lng)) {
            // Filter out sites that don't have a US State abbreviation
            if (!dam["State Abbreviation"]) return;

            // Hide dams the LHDI review has flagged as non-LHD or removed
            const EXCLUDED_REVIEW_STATUSES = new Set([
                'Removed',
                'Confirmed not a LHD',
                'Appears to not be LHD'
            ]);
            if (EXCLUDED_REVIEW_STATUSES.has((dam.Review_Status || '').trim())) return;

            const fatalities = parseInt(dam.NumberOfFatalities) || 0;

            if (showOnlyFatality && fatalities === 0) return;

            // Backend writes the envelope in cms; NWPS forecasts are in cfs. Convert at parse.
            const CMS_TO_CFS = 35.3147;
            let qMinVal = Math.round(parseFloat(dam.Qmin_env) * CMS_TO_CFS);
            let qMaxVal = Math.round(parseFloat(dam.Qmax_env) * CMS_TO_CFS);
            
            if (qMinVal >= 100) qMinVal = Math.round(qMinVal / 100) * 100;
            if (qMaxVal >= 100) qMaxVal = Math.round(qMaxVal / 100) * 100;
            const hasComid = dam.Reach_ID !== undefined && dam.Reach_ID !== null && String(dam.Reach_ID).trim() !== '';
            const hasSafetyData = !isNaN(qMinVal) && hasComid;
            
            // Determine marker color based on data priority tier
            let markerColor = '#95a5a6'; // Default: Gray (Missing hydro link / screening safety data)
            if (fatalities > 0) {
                markerColor = '#e74c3c'; // Priority 1: Red (Fatality recorded)
            } else if (hasSafetyData) {
                markerColor = '#f1c40f'; // Priority 2: Yellow (Dangerous flow range calculated)
            } else if (hasComid) {
                markerColor = '#3498db'; // Priority 3: Blue (Live forecast available)
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
                radius: radius,
                fillColor: markerColor,
                color: '#1a252f', // Subtle dark border
                weight: weight,   // Scaled stroke
                opacity: 0.8,    // Slightly transparent stroke
                fillOpacity: 1  
            });

            const _displayName = displayName(dam);
            
            marker.bindTooltip(_displayName, { direction: 'top', offset: [0, -6] });
            
            let popupContent = `
                <div class="popup-content">
                    <strong>${_displayName}</strong><br>
                    <b>Location:</b> ${location}<br>
                    <b>Fatalities:</b> ${fatalities}<br>
                    <hr>`;
            
            const heightFt = parseFloat(dam.Dam_Height_GIS_Ft);
            const lengthFt = parseFloat(dam.Dam_Length_GIS_Ft);
            const twA      = parseFloat(dam.Tailwater_a);
            const twB      = parseFloat(dam.Tailwater_b);
            const rp100Cms = parseFloat(dam.Rp100_cms);
            
            const hasRatingCurves = isFinite(heightFt) && heightFt > 0
                && isFinite(lengthFt) && lengthFt > 0
                && isFinite(twA) && isFinite(twB)
                && isFinite(rp100Cms) && rp100Cms > 0;

            if (hasComid || hasRatingCurves) {
                if (hasSafetyData) {
                    popupContent += `<b>Dangerous Range:</b> ${qMinVal} - ${qMaxVal} cfs<br>`;
                }
                const safetyArgs = hasSafetyData ? `${qMinVal}, ${qMaxVal}` : `null, null`;
                
                let onClickActions = ['openCombinedPanel()'];
                if (hasComid) onClickActions.push(`checkForecast('${dam.Reach_ID}', ${safetyArgs}, ${jsAttrLiteral(_displayName)})`);
                if (hasRatingCurves) onClickActions.push(`showRatingCurves(${heightFt}, ${lengthFt}, ${twA}, ${twB}, ${rp100Cms}, ${jsAttrLiteral(_displayName)})`);
                
                popupContent += `
                    <button class="btn-check" onclick="${onClickActions.join('; ')}">
                        Live Forecast
                    </button>`;
            } else {
                popupContent += `<i>No forecast or rating curve available for this site.</i>`;
            }

            popupContent += `</div>`;
            marker.bindPopup(popupContent);
            markers.addLayer(marker); 
        }
    });
    
    map.addLayer(markers); 

    if (typeof window.updateBoundaryZOrder === 'function') {
        window.updateBoundaryZOrder();
    }
    
    if (typeof window.updateActiveFiltersBadge === 'function') {
        window.updateActiveFiltersBadge();
    }
}

window.openCombinedPanel = () => {
    const cModal = document.getElementById('combinedModal');
    cModal.classList.add('is-open');
    
    if (cModal.style.transform !== 'none') {
        cModal.style.transform = 'none';
        cModal.style.top = '80px';
        cModal.style.left = 'calc(50% - 360px)';
    }
    document.getElementById('forecastContainer').style.display = 'none';
    document.getElementById('ratingCurvesContainer').style.display = 'none';
};

// 4. National Water Model Forecast (NOAA NWPS API, NHDPlus V2 COMID = Reach_ID)
// Medium-range only: ~10d 3-hourly ensemble mean + member spread as uncertainty band.
// Units: API returns ft³/s (cfs) — no conversion needed.
async function checkForecast(comid, qMin, qMax, damName) {
    // Reach_ID arrives as a pandas-style float string ("10376596.0") — NWPS wants an integer.
    comid = String(comid).replace(/\.0+$/, '');
    const hasSafetyRange = qMin !== null && !isNaN(qMin) && qMax !== null && !isNaN(qMax);

    // FIX: Clear out the old header immediately so it doesn't distract the user while loading
    document.getElementById('statusDisplay').innerHTML = `<strong>${damName}</strong><br><span style="color:#888;">Loading forecast details...</span>`;

    // Show modal + spinner immediately
    document.getElementById('forecastContainer').style.display = 'flex';
    document.getElementById('forecastSpinner').style.display = 'block';
    document.getElementById('forecastChart').style.display = 'none';

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

    const validUppers = allUpper.filter(v => v != null && !isNaN(v));
    const maxForecast = validUppers.length > 0 ? Math.max(...validUppers) : 10;
    const yAxisMax = Math.max(maxForecast, 10) * 1.2;

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
                    max: yAxisMax
                },
                x: { ticks: { maxTicksLimit: 10 } }
            }
        }
    });
}

// 4b. Rating curves (tailwater / conjugate / flip) computed client-side.
// Inputs: height + length in ft, tailwater coeffs (D[m] = a * Q[cms]^b), Rp100 in cms.
// Display: Q in cfs on x-axis, depth in ft on y-axis.
function showRatingCurves(heightFt, lengthFt, a, b, rp100Cms, damName) {
    document.getElementById('ratingCurvesContainer').style.display = 'flex';

    const { tailwater, conjugate, flip, dangerConj, dangerFlip } =
        window.LHDHydraulics.buildRatingCurvesFt(heightFt, lengthFt, a, b, rp100Cms);

    const intersections = [];
    for (let i = 0; i < tailwater.length - 1; i++) {
        if (conjugate[i].y !== null && conjugate[i+1].y !== null && tailwater[i].y !== null && tailwater[i+1].y !== null) {
            const diff1 = tailwater[i].y - conjugate[i].y;
            const diff2 = tailwater[i+1].y - conjugate[i+1].y;
            if (diff1 * diff2 <= 0 && diff1 !== diff2) {
                const t = diff1 / (diff1 - diff2);
                if (t > 0 || i === 0) {
                    const x = tailwater[i].x + t * (tailwater[i+1].x - tailwater[i].x);
                    const y = tailwater[i].y + t * (tailwater[i+1].y - tailwater[i].y);
                    intersections.push({x, y, label: 'Intersection (Tailwater & Conjugate)'});
                }
            }
        }
        if (flip[i].y !== null && flip[i+1].y !== null && tailwater[i].y !== null && tailwater[i+1].y !== null) {
            const diff1 = tailwater[i].y - flip[i].y;
            const diff2 = tailwater[i+1].y - flip[i+1].y;
            if (diff1 * diff2 <= 0 && diff1 !== diff2) {
                const t = diff1 / (diff1 - diff2);
                if (t > 0 || i === 0) {
                    const x = tailwater[i].x + t * (tailwater[i+1].x - tailwater[i].x);
                    const y = tailwater[i].y + t * (tailwater[i+1].y - tailwater[i].y);
                    intersections.push({x, y, label: 'Intersection (Tailwater & Flip)'});
                }
            }
        }
    }

    document.getElementById('ratingCurvesHeader').innerHTML =
        `<strong>${damName}</strong><br>` +
        `<span style="color:#7f8c8d; font-size: 12px;">` +
        `P = ${heightFt.toFixed(1)} ft &nbsp;·&nbsp; L = ${lengthFt.toFixed(0)} ft &nbsp;·&nbsp; ` +
        `tailwater D = ${a.toPrecision(3)}·Q<sup>${b.toFixed(3)}</sup> ` +
        `(SI) &nbsp;·&nbsp; Q<sub>max</sub> = ${(rp100Cms * 35.3147).toFixed(0)} cfs (Rp100)` +
        `</span>`;

    const ctx = document.getElementById('ratingCurvesChart').getContext('2d');
    if (ratingCurvesChart) ratingCurvesChart.destroy();
    ratingCurvesChart = new Chart(ctx, {
        type: 'line',
        data: {
            datasets: [
                { label: 'Tailwater (yₜ)',  data: tailwater,
                  order: 1, borderColor: '#3498db', backgroundColor: '#3498db',
                  pointRadius: 0, borderWidth: 2, tension: 0.2, spanGaps: true,
                  pointStyle: 'line' },
                { label: 'Conjugate depth (y₂)', data: conjugate,
                  order: 1, borderColor: '#27ae60', backgroundColor: '#27ae60',
                  pointRadius: 0, borderWidth: 2, tension: 0.2, spanGaps: true,
                  pointStyle: 'line' },
                { label: 'Flip depth (y_flip)', data: flip,
                  order: 1, borderColor: '#e74c3c', backgroundColor: '#e74c3c',
                  pointRadius: 0, borderWidth: 2, tension: 0.2, spanGaps: true,
                  borderDash: [6, 4], pointStyle: 'line' },
                { label: 'Danger Zone Conj', data: dangerConj,
                  order: 1, borderColor: 'transparent', backgroundColor: 'transparent',
                  pointRadius: 0, borderWidth: 0, tension: 0.2, spanGaps: false },
                { label: 'Danger Zone', data: dangerFlip,
                  order: 1, borderColor: 'transparent', backgroundColor: 'rgba(231, 76, 60, 0.25)',
                  pointRadius: 0, borderWidth: 0, tension: 0.2, spanGaps: false, fill: '-1' },
                { label: 'Intersections', data: intersections,
                  order: 0, type: 'scatter', borderColor: '#2c3e50', backgroundColor: '#2c3e50',
                  pointRadius: 5, pointHoverRadius: 7, pointHitRadius: 15, showLine: false },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            parsing: false,
            onClick: (event, elements, chart) => {
                if (elements.length > 0) {
                    const element = elements[0];
                    const dataset = chart.data.datasets[element.datasetIndex];
                    if (dataset.label === 'Intersections') {
                        const pt = dataset.data[element.index];
                        alert(`${pt.label}\n\nCoordinates:\nx: ${pt.x.toFixed(2)} cfs\ny: ${pt.y.toFixed(2)} ft`);
                    }
                }
            },
            plugins: {
                legend: { 
                    labels: { 
                        usePointStyle: true,
                        filter: (item) => !item.text.includes('Danger Zone') && !item.text.includes('Intersections')
                    } 
                },
                tooltip: {
                    filter: (tooltipItem) => !tooltipItem.dataset.label.includes('Danger Zone'),
                    callbacks: {
                        title: (items) => {
                            if (items[0].dataset.label === 'Intersections') return items[0].raw.label;
                            return `Q = ${items[0].parsed.x.toFixed(0)} cfs`;
                        },
                        label: (item) => {
                            if (item.dataset.label === 'Intersections') {
                                return `x: ${item.parsed.x.toFixed(2)} cfs, y: ${item.parsed.y.toFixed(2)} ft`;
                            }
                            return `${item.dataset.label}: ${item.parsed.y.toFixed(2)} ft`;
                        },
                    },
                },
                zoom: {
                    limits: {
                        x: {min: 'original', max: 'original'},
                        y: {min: 'original', max: 'original'}
                    },
                    pan: {
                        enabled: true,
                        mode: 'xy',
                    },
                    zoom: {
                        wheel: {
                            enabled: true,
                        },
                        pinch: {
                            enabled: true
                        },
                        mode: 'xy',
                    }
                }
            },
            scales: {
                x: {
                    type: 'linear',
                    position: 'bottom',
                    title: { display: true, text: 'Discharge Q (cfs)' },
                },
                y: {
                    title: { display: true, text: 'Depth (ft)' },
                    beginAtZero: true,
                },
            },
        },
    });
}
window.showRatingCurves = showRatingCurves;

// 5. Legend and Filter Integration
const legend = L.control({ position: 'bottomright' });
legend.onAdd = function (map) {
    const div = L.DomUtil.create('div', 'info legend');
    div.innerHTML = `
        <strong>Dam Status</strong><br>
        <i style="background: #e74c3c"></i> Fatality Recorded<br>
        <i style="background: #f1c40f"></i> Dangerous Range Calculated<br>
        <i style="background: #3498db"></i> Live Forecast Available<br>
        <i style="background: #95a5a6"></i> Location Info Only
    `;

    L.DomEvent.disableClickPropagation(div);
    return div;
};
legend.addTo(map);

window.addEventListener('resize', () => { map.invalidateSize(); });

// --- Draggable + resizable floating panels (forecast + rating curves) ---
function closeCombinedPanel() {
    document.getElementById('combinedModal').classList.remove('is-open');
    document.getElementById('forecastSliderWrap').style.display = 'none';
}
window.closeCombinedPanel = closeCombinedPanel;

function enablePanelDragResize(panelId, headerId, onResize) {
    const panel  = document.getElementById(panelId);
    const handle = document.getElementById(headerId);
    if (!panel || !handle) return;

    let dragging = false;
    let startX = 0, startY = 0, startLeft = 0, startTop = 0;

    function pinAbsolute() {
        const r = panel.getBoundingClientRect();
        panel.style.left = r.left + 'px';
        panel.style.top  = r.top  + 'px';
        panel.style.transform = 'none';
        panel.style.margin = '0';
    }

    handle.addEventListener('mousedown', (e) => {
        if (e.target.closest('.close')) return;
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

    if (typeof ResizeObserver !== 'undefined' && typeof onResize === 'function') {
        const ro = new ResizeObserver(() => onResize());
        ro.observe(panel);
    }

    const MIN_W = 340, MIN_H = 260;
    let resizing = null;

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
}

enablePanelDragResize('combinedModal', 'combinedModalHeader',
    () => { 
        if (forecastChart) forecastChart.resize(); 
        if (ratingCurvesChart) ratingCurvesChart.resize(); 
    });

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
        let selectedStateLayer = null;
        let stateLayersMap = {};

        function getDynamicStyles() {
            const zoom = map.getZoom();
            let baseWeight = 1; // Thinner border at national scale
            if (zoom > 4 && zoom <= 6) baseWeight = 2;
            if (zoom > 6 && zoom <= 8) baseWeight = 3;
            if (zoom > 8) baseWeight = 4;
            
            return {
                defaultStyle: { color: 'black', weight: baseWeight, fillOpacity: 0.05 },
                hoverStyle: { color: 'black', weight: baseWeight + 1, fillOpacity: 0.2 },
                selectedStyle: { color: '#3498db', weight: baseWeight + 2, fillOpacity: 0.2 }
            };
        }

        window.updateBoundaryZOrder = () => {
            if (window.stateBoundaryLayer) {
                if (map.getZoom() <= 4) {
                    window.stateBoundaryLayer.bringToFront();
                } else {
                    window.stateBoundaryLayer.bringToBack();
                }
                
                // Update dynamic stroke width on zoom
                const styles = getDynamicStyles();
                window.stateBoundaryLayer.eachLayer(layer => {
                    if (layer === selectedStateLayer) {
                        layer.setStyle(styles.selectedStyle);
                    } else {
                        layer.setStyle(styles.defaultStyle);
                    }
                });
            }
        };
        map.on('zoomend', window.updateBoundaryZOrder);

        const response = await fetch(url);
        const data = await response.json();
        

        window.clearStateHighlight = () => {
            if (selectedStateLayer) {
                selectedStateLayer.setStyle(getDynamicStyles().defaultStyle);
                selectedStateLayer = null;
            }
        };

        window.highlightStateBoundary = (stateAbbr) => {
            window.clearStateHighlight();
            if (stateAbbr && stateLayersMap[stateAbbr]) {
                const layer = stateLayersMap[stateAbbr];
                layer.setStyle(getDynamicStyles().selectedStyle);
                selectedStateLayer = layer;
            }
        };

        const stateBoundaryLayer = L.geoJSON(data, {
            style: function() { return getDynamicStyles().defaultStyle; },
            onEachFeature: function(feature, layer) {
                const stateAbbr = feature.properties.stusps || feature.properties.STUSPS || feature.properties.STUSAB || feature.properties.STATE;
                if (stateAbbr) {
                    stateLayersMap[stateAbbr.toUpperCase()] = layer;
                }

                // Visual feedback on hover
                layer.on('mouseover', function() {
                    if (selectedStateLayer !== layer) {
                        layer.setStyle(getDynamicStyles().hoverStyle);
                    }
                });
                layer.on('mouseout', function() {
                    if (selectedStateLayer !== layer) {
                        layer.setStyle(getDynamicStyles().defaultStyle);
                    }
                });
                
                // Double click to zoom back out (deselect)
                layer.on('dblclick', function(e) {
                    // Prevent the default map double-click zoom from interfering on canvas
                    map.doubleClickZoom.disable();
                    setTimeout(() => map.doubleClickZoom.enable(), 500);

                    // Prevent the default map double-click zoom
                    if (e.originalEvent) {
                        L.DomEvent.stopPropagation(e.originalEvent);
                    }
                    
                    if (selectedStateLayer !== layer) {
                        return; // Only act if it's the currently selected state
                    }

                    const stateInput = document.getElementById('globalStateInput');
                    const applyBtn = document.getElementById('globalApplyFilterBtn');
                    
                    if (stateInput && applyBtn) {
                        stateInput.value = ''; // Clear state filter
                        applyBtn.click();      // Trigger the UI's existing filter logic
                    }
                    
                    map.flyTo([39.82, -98.57], 4, { duration: 1.5 });
                    if (typeof window.closeSearchPanel === 'function') {
                        window.closeSearchPanel();
                    }
                });

                // Click to filter by state
                layer.on('click', function(e) {
                    // Extract state abbreviation (Census shapefiles usually use STUSPS)
                    const stateAbbr = feature.properties.stusps || feature.properties.STUSPS || feature.properties.STUSAB || feature.properties.STATE;
                    if (stateAbbr) {
                        const upperAbbr = stateAbbr.toUpperCase();
                        const stateInput = document.getElementById('globalStateInput');
                        const applyBtn = document.getElementById('globalApplyFilterBtn');
                        
                        if (stateInput && applyBtn) {
                            // Only apply if it is not already the selected state
                            if (stateInput.value.toUpperCase() !== upperAbbr) {
                                stateInput.value = upperAbbr; // Toggle on
                                if (selectedStateLayer) {
                                    selectedStateLayer.setStyle(getDynamicStyles().defaultStyle);
                                }
                                layer.setStyle(getDynamicStyles().selectedStyle);
                                selectedStateLayer = layer;
                            
                                // Trigger the UI's existing filter logic
                                applyBtn.click();
                                
                                // Adjust zoom to state boundary
                                setTimeout(() => { map.flyToBounds(layer.getBounds(), { duration: 1.5, padding: [50, 50] }); }, 50);
                                if (typeof window.openSearchPanel === 'function') {
                                    window.openSearchPanel();
                                }
                            }
                        }
                    }
                });
            }
        });
        
        window.stateBoundaryLayer = stateBoundaryLayer;

        // Add it to the top-right layer control menu
        layerControl.addOverlay(stateBoundaryLayer, "State Boundaries");
        
        // If you want the boundaries to be visible immediately on load, uncomment the next line:
        stateBoundaryLayer.addTo(map);
        window.updateBoundaryZOrder();
    } catch (error) {
        console.error(`Failed to load JSON from ${url}:`, error);
    }
}

loadStateBoundaries('boundaries/cb_2025_us_state_20m_conus.json');

// --- Global Escape Key Handler ---
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        // Clear all filters if they are applied
        const stateInput = document.getElementById('globalStateInput');
        const riverInput = document.getElementById('globalRiverInput');
        const fatalityCheckbox = document.getElementById('fatalityFilter');
        const applyBtn = document.getElementById('globalApplyFilterBtn');
        const searchInput = document.getElementById('globalSearchInput');
        const searchResults = document.getElementById('globalSearchResults');
        
        let filtersChanged = false;
        if (stateInput && stateInput.value !== '') {
            stateInput.value = '';
            filtersChanged = true;
        }
        if (riverInput && riverInput.value !== '') {
            riverInput.value = '';
            filtersChanged = true;
        }
        if (fatalityCheckbox && fatalityCheckbox.checked) {
            fatalityCheckbox.checked = false;
            filtersChanged = true;
        }
        if (searchInput && searchInput.value !== '') {
            searchInput.value = '';
            if (searchResults) {
                searchResults.innerHTML = '';
            }
        }
        
        if (filtersChanged && applyBtn) {
            applyBtn.click();
        }
        
        // Zoom out to national view
        map.flyTo([39.82, -98.57], 4, { duration: 1.5   });
        
        // Close any open panels or popups
        if (typeof window.closeSearchPanel === 'function') {
            window.closeSearchPanel();
        }
        if (typeof window.closeCombinedPanel === 'function') {
            window.closeCombinedPanel();
        }
        map.closePopup();
    }
});