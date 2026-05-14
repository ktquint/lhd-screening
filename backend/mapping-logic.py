//-- 1. Map initialization --//

const map = L.map('map').setView([39.5, -98.5], 4);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);


//-- 2. Clusters --//

const damCluster = L.markerClusterGroup();
const nwmCluster = L.markerClusterGroup();

map.addLayer(damCluster);
map.addLayer(nwmCluster);


//-- 3. Globals --//

let allDamFeatures = [];


//-- 4. Styling --//

function getHazardColor(h) {

    if (h === "High") return "red";

    if (h === "Significant") return "orange";

    if (h === "Low") return "green";

    return "gray";
}


//-- 5. Filters --//

function getSelectedHazards() {

    return Array.from(
        document.querySelectorAll('.filter-box input:checked')
    ).map(cb => cb.value);
}


//-- 6. Forecast --//

function parseForecastData(data) {

    let times = [];
    let flows = [];

    // FORMAT 1
    // { times: [], flows: [] }

    if (data.times && data.flows) {

        times = data.times;
        flows = data.flows;
    }

    // FORMAT 2
    // NOAA timeSeries format

    else if (data.forecast && data.forecast.timeSeries) {

        times = data.forecast.timeSeries.map(
            d => d.validTime || d.time
        );

        flows = data.forecast.timeSeries.map(
            d => d.value || d.flow
        );
    }

    // FORMAT 3
    // Generic array

    else if (Array.isArray(data)) {

        times = data.map(
            d => d.time || d.validTime || d.datetime
        );

        flows = data.map(
            d => d.flow || d.value || d.discharge
        );
    }

    // FORMAT 4
    // Nested hydrograph object

    else if (data.hydrograph?.series) {

        times = data.hydrograph.series.map(
            d => d.time
        );

        flows = data.hydrograph.series.map(
            d => d.flow
        );
    }

    return {
        times,
        flows
    };
}

async function loadForecastForDam(feature) {

    const props = feature.properties;

    const comid = props.COMID || props.comid;

    if (!comid) {

        alert("No COMID available");

        return;
    }

    try {

        //-- Flask Forecast --//

        const response = await fetch(
            `/api/nwm_forecast/${comid}`
        );

        const ForecastData = await response.json();
        const parsedForecast = parseForecastData(ForecastData);
        const times = parsedForecast.times;
        const flows = parsedForecast.flows;

        //-- NOAA Gauge Info --//

        const gaugeResponse = await fetch(
            `https://api.water.noaa.gov/nwps/v1/gauges/${comid}`
        );

        const gaugeData = await gaugeResponse.json();

        //-- Modal Elements --//

        const modal =
            document.getElementById('forecastModal');

        const statusDisplay =
            document.getElementById('statusDisplay');

        const nwmInfo =
            document.getElementById('nwmInfo');

        modal.style.display = "block";

        //-- Left Panel --//

        statusDisplay.innerHTML = `
            <h2>${props.name || "Dam"}</h2>

            <p><strong>COMID:</strong> ${comid}</p>

            <p><strong>Hazard:</strong>
            ${props.hazard || "N/A"}</p>
        `;

        //-- Right Panel --//

        nwmInfo.innerHTML = `
            <strong>Gauge Name:</strong>
            ${gaugeData.name || "N/A"}<br>

            <strong>Latitude:</strong>
            ${gaugeData.latitude || "N/A"}<br>

            <strong>Longitude:</strong>
            ${gaugeData.longitude || "N/A"}<br>

            <strong>Current Flow:</strong>
            ${gaugeData.flow || "N/A"} cfs<br>

            <strong>Status:</strong>
            ${gaugeData.status || "N/A"}<br>
        `;

        //-- Hydrograph --//

        const ctx =
            document.getElementById('forecastChart')
            .getContext('2d');

        if (window.forecastChart) {

            window.forecastChart.destroy();
        }

        window.forecastChart = new Chart(ctx, {

            type: 'line',

            data: {

                labels: times,

                datasets: [{
                    label: 'NWM Forecast Flow (cfs)',
                    data: flows,
                    borderWidth: 2,
                    tension: 0.25
                }]
            },

            options: {

                responsive: true,

                interaction: {
                    mode: 'index',
                    intersect: false
                },

                scales: {

                    x: {
                        ticks: {
                            maxTicksLimit: 8
                        }
                    },

                    y: {
                        title: {
                            display: true,
                            text: 'Flow (cfs)'
                        }
                    }
                }
            }
        });

    }
    catch(err) {

        console.error("Forecast Error:", err);

        alert("Failed to load forecast.");
    }
}


//-- 7. Render dams --//

function renderDams() {

    damCluster.clearLayers();

    const selected = getSelectedHazards();

    allDamFeatures.forEach(feature => {

        const hazard =
            feature.properties.hazard ||
            feature.properties.Hazard ||
            feature.properties.HAZARD;

        if (!selected.includes(hazard)) return;

        const [lon, lat] =
            feature.geometry.coordinates;

        const marker = L.circleMarker(
            [lat, lon],
            {
                radius: 6,
                fillColor: getHazardColor(hazard),
                color: "#fff",
                weight: 1,
                fillOpacity: 0.8
            }
        );

        marker.bindTooltip(
            feature.properties.name ||
            feature.properties.Name ||
            "Dam"
        );

        marker.on('click', () => {

            loadForecastForDam(feature);

        });

        damCluster.addLayer(marker);
    });
}


//-- 8. Load GeoPackage --//

fetch('dams.gpkg')

    .then(r => r.arrayBuffer())

    .then(b => geopackage.open(b))

    .then(gpkg => {

        console.log("GeoPackage loaded");

        gpkg.getFeatureTables().forEach(table => {

            const features =
                gpkg.iterateGeoJSONFeatures(table);

            for (let f of features) {

                allDamFeatures.push(f);
            }
        });

        console.log(
            "Dam count:",
            allDamFeatures.length
        );

        renderDams();
    })

    .catch(err => {

        console.error(
            "GeoPackage Load Error:",
            err
        );

    });


//-- 9. Load NWM --//

function parseGauge(site) {

    return {

        name:
            site.name ||
            site.gaugeName ||
            site.stationName ||
            "Unknown Gauge",

        latitude:
            site.latitude ||
            site.lat ||
            site.location?.latitude,

        longitude:
            site.longitude ||
            site.lon ||
            site.lng ||
            site.location?.longitude,

        flow:
            site.flow ||
            site.discharge ||
            site.streamflow ||
            site.value ||
            "N/A",

        status:
            site.status ||
            site.condition ||
            "Unknown"
    };
}

function loadNWM() {

    fetch(
        "https://api.water.noaa.gov/nwps/v1/gauges"
    )

    .then(r => r.json())

    .then(data => {

        console.log(data);

        nwmCluster.clearLayers();

        //-- NOAA may return items[] --//

        const gauges =
            data.items || data.features || data;

        if (!Array.isArray(gauges)) {

            console.error(
                "NWM response is not an array"
            );

            return;
        }

        gauges.forEach(siteRaw => {

            const site =
                siteRaw.properties || siteRaw;

            const parsed =
                parseGauge(site);

            if (!parsed.latitude || !parsed.longitude) {
                return;
            }

            const marker = L.circleMarker(
                [parsed.latitude, parsed.longitude],
                {
                    radius: 5,
                    fillColor: "blue",
                    color: "#fff",
                    fillOpacity: 0.7
                }
            );

            marker.bindTooltip(
                `<strong>${parsed.name}</strong><br>
                Flow: ${parsed.flow} cfs`,
                {
                    sticky: true
                }
            );

            nwmCluster.addLayer(marker);

        });

    })

    .catch(err => {

        console.error(
            "NWM Load Error:",
            err
        );

    });
}

//-- Initial Load --//

loadNWM();

setInterval(loadNWM, 600000);


//-- 10. Hook filters --//

document.querySelectorAll(
    '.filter-box input'
)

.forEach(cb => {

    cb.addEventListener(
        'change',
        renderDams
    );

});