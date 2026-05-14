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

                const loc = [dam.City, dam['State Abbreviation']].filter(Boolean).join(', ');
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
    const filterEl = document.getElementById('forecastFilter');
    const showOnlyForecast = filterEl ? filterEl.checked : false;

    allDams.forEach(dam => {
        const lat = parseFloat(dam.Latitude);
        const lng = parseFloat(dam.Longitude);
        
        if (!isNaN(lat) && !isNaN(lng)) {
            // Filter out sites that don't have a US State abbreviation
            if (!dam["State Abbreviation"]) return;

            const qMinVal = Math.round(parseFloat(dam.Qmin));
            const qMaxVal = Math.round(parseFloat(dam.Qmax));
            const hasSafetyData = !isNaN(qMinVal) && dam.LinkNo;

            if (showOnlyForecast && !hasSafetyData) return;

            const city = dam.City || "Unknown City";
            const state = dam["State Abbreviation"] || "";
            const location = city + (state ? `, ${state}` : "");
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
            
            if (hasSafetyData) {
                popupContent += `
                    <b>Dangerous Range:</b> ${qMinVal} - ${qMaxVal} cfs
                    <button class="btn-check" onclick="checkSafety('${dam.LinkNo}', ${qMinVal}, ${qMaxVal}, '${dam.Dam_Name}')">
                        Check Live Forecast
                    </button>`;
            } else {
                popupContent += `<i>Safety flow range data unavailable for this site.</i>`;
            }

            popupContent += `</div>`;
            marker.bindPopup(popupContent);
            markers.addLayer(marker); 
        }
    });
    
    map.addLayer(markers); 
}

// 4. GEOGLOWS API Integration & Plotting
async function checkSafety(linkNo, qMin, qMax, damName) {
    const url = `https://geoglows.ecmwf.int/api/v2/forecast/${linkNo}?format=json`;
    
    try {
        const response = await fetch(url);
        const data = await response.json();

        if (data && data.flow_median && data.flow_median.length > 0) {
            const nowLocal = new Date();
            nowLocal.setMinutes(0, 0, 0);

            const startIndex = data.datetime.findIndex(timeStr => new Date(timeStr).getTime() >= nowLocal.getTime());
            const finalStart = startIndex === -1 ? 0 : startIndex;

            const slicedMedian = data.flow_median.slice(finalStart);
            const slicedUpper = (data.flow_uncertainty_upper || []).slice(finalStart);
            const slicedLower = (data.flow_uncertainty_lower || []).slice(finalStart);
            const slicedDatetime = (data.datetime || []).slice(finalStart);

            const cfsMedian = slicedMedian.map(cms => cms * 35.3147);
            const cfsUpper = slicedUpper.map(cms => cms * 35.3147);
            const cfsLower = slicedLower.map(cms => cms * 35.3147);
            
            const currentCfs = cfsMedian[0];

            const isAnyDangerous = cfsMedian.some((_, i) => {
                const low = cfsLower[i];
                const high = cfsUpper[i];
                return high >= qMin && low <= qMax;
            });

            const labels = slicedDatetime.map(timeStr => {
                const date = new Date(timeStr);
                return date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit' });
            });

            let statusText = `<strong>${damName}</strong><br>`;
            statusText += `Current Forecast: ${currentCfs.toFixed(0)} cfs | Range: ${qMin.toFixed(0)}-${qMax.toFixed(0)} cfs<br>`;
            
            if (isAnyDangerous) {
                statusText += `<span style="color:red; font-weight:bold;">⚠️ WARNING: DANGEROUS CONDITIONS FORECASTED ⚠️</span>`;
            } else {
                statusText += `<span style="color:green; font-weight:bold;">✅ Status: Safe for Forecast Period</span>`;
            }

            document.getElementById('statusDisplay').innerHTML = statusText;
            document.getElementById('forecastModal').style.display = 'block';

            const ctx = document.getElementById('forecastChart').getContext('2d');
            if (forecastChart) forecastChart.destroy();

            forecastChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [
                        { label: 'Max Dangerous Flow', data: Array(cfsMedian.length).fill(qMax), borderColor: '#e74c3c', borderDash: [10, 5], borderWidth: 2, pointRadius: 0, fill: false },
                        { label: 'Dangerous Flow Range', data: Array(cfsMedian.length).fill(qMin), borderColor: '#e74c3c', borderDash: [10, 5], borderWidth: 2, pointRadius: 0, fill: 0, backgroundColor: 'rgba(231, 76, 60, 0.2)' },
                        { label: 'Median Forecast (cfs)', data: cfsMedian, borderColor: '#000000', backgroundColor: 'transparent', fill: false, tension: 0.2, borderWidth: 3, pointRadius: 0 },
                        { label: 'Forecast Uncertainty (Upper)', data: cfsUpper, borderColor: 'rgba(52, 152, 219, 0.5)', borderWidth: 1, pointRadius: 0, fill: '+1', backgroundColor: 'rgba(52, 152, 219, 0.2)' },
                        { label: 'Forecast Uncertainty (Lower)', data: cfsLower, borderColor: 'rgba(52, 152, 219, 0.5)', borderWidth: 1, pointRadius: 0, fill: false }
                    ]
                },
                options: {
                    responsive: true,
                    plugins: { filler: { propagate: true } },
                    scales: {
                        y: { beginAtZero: true, title: { display: true, text: 'Streamflow (cfs)' } },
                        x: { ticks: { maxTicksLimit: 10 } }
                    }
                }
            });
        }
    } catch (err) {
        console.error("API Error:", err);
    }
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
                <input type="checkbox" id="forecastFilter"> Hide sites without forecasts
            </label>
        </div>
    `;

    L.DomEvent.disableClickPropagation(div);
    
    setTimeout(() => {
        const filterCheckbox = document.getElementById('forecastFilter');
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
    let cfdLoaded = false;

    async function loadCfdToolbox() {
        if (cfdLoaded) return;
        const target = document.getElementById('cfd-content');
        try {
            const res = await fetch('cfd-toolbox/README.md');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const md = await res.text();
            const renderer = new marked.Renderer();
            const origImage = renderer.image.bind(renderer);
            renderer.image = (href, title, text) => {
                if (href && !/^(https?:)?\/\//.test(href) && !href.startsWith('cfd-toolbox/')) {
                    href = 'cfd-toolbox/' + href;
                }
                return origImage(href, title, text);
            };
            const origLink = renderer.link.bind(renderer);
            renderer.link = (href, title, text) => {
                if (href && !/^(https?:|mailto:|#)/.test(href) && !href.startsWith('cfd-toolbox/')) {
                    href = 'cfd-toolbox/' + href;
                }
                if (href && /\.(png|jpe?g|gif|svg|webp)$/i.test(href)) {
                    const alt = (text || '').replace(/"/g, '&quot;');
                    return `<figure><img src="${href}" alt="${alt}"${title ? ` title="${title}"` : ''}><figcaption><a href="${href}" target="_blank" rel="noopener">${text}</a></figcaption></figure>`;
                }
                return origLink(href, title, text);
            };
            target.classList.remove('cfd-loading');
            target.innerHTML = marked.parse(md, { renderer });
            cfdLoaded = true;
        } catch (err) {
            console.error('Failed to load CFD toolbox README:', err);
            target.innerHTML = `<p style="color:#c0392b;">Could not load CFD Toolbox documentation: ${err.message}</p>`;
        }
    }

    function activate(tabName) {
        buttons.forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tabName));
        panels.forEach(p => p.classList.toggle('active', p.id === `tab-${tabName}`));
        if (tabName === 'forecasts') {
            setTimeout(() => map.invalidateSize(), 0);
        } else if (tabName === 'cfd-toolbox') {
            loadCfdToolbox();
        }
    }

    buttons.forEach(btn => {
        btn.addEventListener('click', () => activate(btn.dataset.tab));
    });
})();