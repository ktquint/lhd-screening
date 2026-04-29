# lhd-screening

Public-facing low-head dam screening tool. Combines:
- **Frontend** (forthcoming): Leaflet map for browsing known dams + submitting new lat/lon points (adapted from `lhd-screening-site`).
- **Backend** (forthcoming): trimmed pipeline wrapping `lhd-processor` to run hydraulic analysis on a single user-submitted point, replacing the `weir_length` input with a value derived from the DEM water surface at the upstream cross-section.

## Status

Pre-pipeline: validating the assumption that the upstream DEM water-surface width
is a usable substitute for the known `weir_length`.

## Setup

This project depends on `lhd-processor` being importable. The env is a
**micromamba** env (not conda), so:

```bash
micromamba activate lhd-environment
cd ../lhd-processor && pip install -e .   # if not already installed
```

…or rely on the path-based fallback in the scripts (defaults to
`~/Developer/lhd-processor`).

If activation fails in your terminal, invoke the env's Python directly:

```bash
/opt/homebrew/Cellar/micromamba/2.4.0/envs/lhd-environment/bin/python \
    scripts/validate_water_width.py ...
```

## Validation script

```bash
python scripts/validate_water_width.py \
    --database /path/to/your_dams.xlsx \
    --results /path/to/Results \
    --output output/
```

Produces `water_width_validation.csv` and a 1:1 scatter plot comparing the
known `weir_length` (from the Sites sheet) to the upstream-XS water-surface
width measured from the DEM + ARC's estimated WSE.
