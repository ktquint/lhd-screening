// Overwritten at deploy time (see .github/workflows/pages.yml) with the
// deployed commit's short SHA, so it only changes when the repo (including
// data/src and data/fdc) actually changes. Used to cache-bust per-dam
// rating curve / flow duration curve fetches without cache-busting every
// page load.
window.DATA_VERSION = "dev";
