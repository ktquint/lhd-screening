"""
Estimate dam height P from ARC outputs using the Method B (ARC-slope flat
zone) downstream sampler from Quintana & Hotchkiss (ASDSO 2026).

The upstream water-surface elevation is sampled at the same Method 2 flat-
zone cell used for crest-length estimation (caller passes it in via the
``width_info`` dict returned by ``estimate_weir_length``).  The downstream
sample is the first cell whose ARC-reported per-cell ``Slope`` reconverges
to the reference channel slope (median ``Slope`` over 100–500 m downstream),
indicating the flow has recovered to normal depth past the dam's influence.
Dam height is then recovered from the 1-D energy balance via
``solve_weir_geom(Q, L, y_T, ΔWSE)``.

Validated against the 72-dam cohort with median P_est / P_NID = 0.48 and
RMSE = 4.24 m.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from lhd_processor.hydraulics import solve_weir_geom
from screening.reach import (
    DEFAULT_REF_SLOPE_MAX,
    DEFAULT_REF_SLOPE_MIN,
    DEFAULT_SLOPE_TOL,
    pick_downstream_cell,
)


def estimate_dam_height(
    baseflow_cms: float,
    weir_length_m: float,
    reach: pd.DataFrame,
    crest_wse: float,
    ref_min_m: float = DEFAULT_REF_SLOPE_MIN,
    ref_max_m: float = DEFAULT_REF_SLOPE_MAX,
    slope_tol: float = DEFAULT_SLOPE_TOL,
) -> Optional[dict]:
    """Solve for dam height P from a Method B downstream sample.

    Parameters
    ----------
    baseflow_cms
        Long-term median discharge used as Q in the energy balance.
    weir_length_m
        Crest length L from the Method 2 width estimator.
    reach
        Reach DataFrame from ``screening.reach.build_reach`` (oriented
        upstream→downstream with signed ``x`` from snap cell).
    crest_wse
        Upstream water-surface elevation (m) — the Method 2 crest cell's
        ``DEM_Elev``.

    Returns
    -------
    dict with keys ``P_height_m``, ``H_head_m``, ``delta_wse_m``,
    ``y_t_m``, ``ds_row``, ``ds_col``, ``ds_wse``, ``ds_base``, ``ds_x_m``,
    ``ds_slope``.  Returns ``None`` if Q or L is non-positive, or if the
    upstream/downstream geometry produces a non-physical
    ``delta_wse``/``y_t`` (energy balance has no solution).
    """
    if baseflow_cms <= 0 or weir_length_m <= 0:
        return None
    if reach is None or reach.empty:
        return None

    ds = pick_downstream_cell(
        reach, ref_min=ref_min_m, ref_max=ref_max_m, slope_tol=slope_tol,
    )

    wse_ds = float(ds["DEM_Elev"])
    base_ds = float(ds["BaseElev"])
    delta_wse = crest_wse - wse_ds
    y_t = wse_ds - base_ds

    if delta_wse <= 0 or y_t <= 0:
        return None

    H, P = solve_weir_geom(baseflow_cms, weir_length_m, y_t, delta_wse)
    if P is None or P <= 0:
        return None

    return {
        "P_height_m": float(P),
        "H_head_m": float(H) if H is not None else None,
        "delta_wse_m": float(delta_wse),
        "y_t_m": float(y_t),
        "ds_row": int(ds["Row"]),
        "ds_col": int(ds["Col"]),
        "ds_wse": wse_ds,
        "ds_base": base_ds,
        "ds_x_m": float(ds["x"]),
        "ds_slope": (float(ds["Slope"]) if "Slope" in ds.index
                     and pd.notna(ds["Slope"]) else None),
    }
