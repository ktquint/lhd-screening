"""Fetch LHDI review fields from the NID API and merge them into the master CSV.

For every row in ``frontend/data/full_lhd_website.csv`` that has an ``LHD_ID``,
hit ``GET https://nid.sec.usace.army.mil/api/lowhead-dams/{id}/inventory`` and
pull four fields:

    reviewStatus         -> Review_Status         (column already exists; refreshed)
    lhdReviewer          -> LHD_Reviewer          (new)
    reviewNotes          -> Review_Notes          (new)
    reviewNotesDetails   -> Review_Notes_Details  (new)

A timestamped backup of the master CSV is written before merging.
"""

from __future__ import annotations

import csv
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

MASTER_CSV = Path(__file__).resolve().parent.parent / "frontend" / "data" / "full_lhd_website.csv"
ENDPOINT = "https://nid.sec.usace.army.mil/api/lowhead-dams/{lhd_id}/inventory"
WORKERS = 16
TIMEOUT = 30
REVIEW_FIELDS = ("reviewStatus", "lhdReviewer", "reviewNotes", "reviewNotesDetails",
                 "updated", "lastEditedDate")
COLUMN_MAP = {
    "reviewStatus":      "Review_Status",
    "lhdReviewer":       "LHD_Reviewer",
    "reviewNotes":       "Review_Notes",
    "reviewNotesDetails":"Review_Notes_Details",
    "updated":           "LHDI_Updated",
    "lastEditedDate":    "LHDI_Last_Edited",
}


def normalize_lhd_id(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return str(int(float(raw)))
    except ValueError:
        return None


def fetch_one(session: requests.Session, lhd_id: str) -> tuple[str, dict | None, str | None]:
    url = ENDPOINT.format(lhd_id=lhd_id)
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return lhd_id, None, f"HTTP {r.status_code}"
        data = r.json()
        return lhd_id, {f: data.get(f) for f in REVIEW_FIELDS}, None
    except Exception as e:
        return lhd_id, None, repr(e)


def fetch_all(lhd_ids: list[str]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    errors: list[tuple[str, str]] = []
    start = time.time()
    with requests.Session() as session, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(fetch_one, session, lid) for lid in lhd_ids]
        for i, fut in enumerate(as_completed(futures), 1):
            lid, fields, err = fut.result()
            if fields is not None:
                results[lid] = fields
            else:
                errors.append((lid, err or "unknown"))
            if i % 500 == 0 or i == len(futures):
                elapsed = time.time() - start
                print(f"  {i}/{len(futures)}  ok={len(results)}  err={len(errors)}  {elapsed:.1f}s", flush=True)
    if errors:
        print(f"\n{len(errors)} errors. First 10:")
        for lid, e in errors[:10]:
            print(f"  {lid}: {e}")
    return results


def merge_into_csv(csv_path: Path, results: dict[str, dict]) -> None:
    backup = csv_path.with_suffix(csv_path.suffix + f".bak.{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(csv_path, backup)
    print(f"backup -> {backup.name}")

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    new_cols = [COLUMN_MAP[f] for f in REVIEW_FIELDS if COLUMN_MAP[f] not in fieldnames]
    if "Review_Status" in fieldnames:
        insert_at = fieldnames.index("Review_Status") + 1
    else:
        insert_at = len(fieldnames)
        fieldnames.append("Review_Status")
    for col in new_cols:
        if col not in fieldnames:
            fieldnames.insert(insert_at, col)
            insert_at += 1

    updated = 0
    diffs_review_status = 0
    for row in rows:
        lid = normalize_lhd_id(row.get("LHD_ID", ""))
        if not lid or lid not in results:
            for col in new_cols:
                row.setdefault(col, "")
            continue
        fields = results[lid]
        new_rs = fields.get("reviewStatus") or ""
        old_rs = row.get("Review_Status") or ""
        if new_rs != old_rs:
            diffs_review_status += 1
        for api_field, csv_col in COLUMN_MAP.items():
            val = fields.get(api_field)
            row[csv_col] = "" if val is None else str(val)
        updated += 1

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"rows updated: {updated}")
    print(f"Review_Status values that changed: {diffs_review_status}")
    print(f"new columns added: {new_cols}")


def main() -> int:
    if not MASTER_CSV.exists():
        print(f"missing: {MASTER_CSV}", file=sys.stderr)
        return 1

    with open(MASTER_CSV, newline="") as f:
        reader = csv.DictReader(f)
        lhd_ids = []
        seen = set()
        for row in reader:
            lid = normalize_lhd_id(row.get("LHD_ID", ""))
            if lid and lid not in seen:
                seen.add(lid)
                lhd_ids.append(lid)
    print(f"unique LHD_IDs to fetch: {len(lhd_ids)}")

    results = fetch_all(lhd_ids)
    print(f"\nfetched {len(results)}/{len(lhd_ids)} records\n")

    merge_into_csv(MASTER_CSV, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
