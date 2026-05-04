"""
Build the per-request Flow_File CSV that ARC consumes.

For the screening pipeline we only need two flow values per stream reach:
- q_ep_50 (50% exceedance, == median daily flow) for baseflow
- rp100 (100-year return-period flow) for max flow

We pull the median from the GEOGLOWS retrospective endpoint and rp100 from
the GEOGLOWS return_periods endpoint.

ARC reads this CSV via:
    Flow_File       <out_path>
    Flow_File_ID    comid
    Flow_File_BF    q_ep_50
    Flow_File_QMax  rp100
"""
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import geoglows


COLUMNS = ["comid", "q_ep_50", "rp100"]


def build_geoglows_flow_csv(river_ids: Iterable[int], out_path: Path) -> pd.DataFrame:
    """
    For each TDX-Hydro LINKNO, fetch retrospective + rp100 from GEOGLOWS and
    write an ARC-compatible Flow_File CSV at out_path.

    The 'comid' column carries the LINKNO. ARC's Flow_File_ID parameter
    decides which CSV column to match against, so the column name doesn't
    have to literally be a NHDPlus comid.
    """
    rows = []
    for rid in river_ids:
        rid = int(rid)
        try:
            ts = geoglows.data.retrospective(river_id=rid, resolution="daily")
        except Exception as e:
            print(f"[skip] river {rid}: retrospective fetch failed: {e}")
            continue
        flow = ts.iloc[:, 0].dropna().values if not ts.empty else np.array([])
        if len(flow) == 0:
            print(f"[skip] river {rid}: no valid retrospective flow values")
            continue

        try:
            rp_df = geoglows.data.return_periods(river_id=rid)
        except Exception as e:
            print(f"[skip] river {rid}: return_periods fetch failed: {e}")
            continue
        if "return_period" in rp_df.columns:
            rp_df = rp_df.set_index("return_period")
        try:
            rp100 = float(rp_df.iloc[:, 0].loc[100])
        except (KeyError, IndexError):
            print(f"[skip] river {rid}: rp100 missing from API response")
            continue

        rows.append({
            "comid": rid,
            "q_ep_50": float(np.median(flow)),
            "rp100": rp100,
        })

    if not rows:
        raise RuntimeError("No streamflow data could be fetched for any river ID.")

    df = pd.DataFrame(rows, columns=COLUMNS)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    return df


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", required=True,
                        help="Comma-separated TDX-Hydro LINKNOs (e.g. 750115016,750115017)")
    parser.add_argument("--out", required=True, type=Path,
                        help="Output CSV path")
    args = parser.parse_args()
    ids = [int(x.strip()) for x in args.ids.split(",") if x.strip()]
    df = build_geoglows_flow_csv(ids, args.out)
    print(df)
