"""
split_data_to_files.py — one-off: explode monolithic JSON files into per-COMID files.

Reads:
  frontend/data/nwm_fdc.json              → frontend/data/fdc/<comid>.json
  frontend/data/synthetic_rating_curves.json → frontend/data/src/<comid>.json

Safe to re-run (overwrites existing files).
"""
from __future__ import annotations
import json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

def split(src_path: Path, out_dir: Path, skip_keys: set[str] = frozenset()) -> int:
    print(f"Reading {src_path} …", flush=True)
    with open(src_path) as f:
        data = json.load(f)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for key, val in data.items():
        if key in skip_keys:
            continue
        with open(out_dir / f"{key}.json", "w") as f:
            json.dump(val, f, separators=(",", ":"))
        n += 1
    print(f"  → {n} files written to {out_dir}", flush=True)
    return n

def main() -> int:
    split(
        REPO / "frontend/data/nwm_fdc.json",
        REPO / "frontend/data/fdc",
        skip_keys={"_meta"},
    )
    split(
        REPO / "frontend/data/synthetic_rating_curves.json",
        REPO / "frontend/data/src",
    )
    return 0

if __name__ == "__main__":
    sys.exit(main())
