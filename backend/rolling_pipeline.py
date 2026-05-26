import os
import shutil
import subprocess
import pandas as pd
from pathlib import Path

# Configurations
STAGING_DIR = Path("/data/lhd_staging")
MASTER_CSV = Path("frontend/data/full_lhd_website.csv")
FINAL_EXPORT_DIR = Path("/data/lhd_final_summaries")

def run_rolling_pipeline():
    # 1. Load your master screening dataset
    df = pd.read_csv(MASTER_CSV)
    
    # Example: Chunking by a regional attribute like State, HUC, or an arbitrary subset
    # Sorting or grouping ensures spatial proximity, minimizing overlapping tile downloads
    if "State" in df.columns:
        batches = df.groupby("State")
    else:
        # Fallback: Split into static row batches of ~600 dams each
        chunk_size = 600
        df['batch_id'] = df.index // chunk_size
        batches = df.groupby('batch_id')

    FINAL_EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    for batch_name, batch_df in batches:
        print(f"\n==================================================")
        print(f"PROCESSING SPATIAL BATCH: {batch_name}")
        print(f"==================================================")
        
        # Write out a temporary CSV for just this local chunk
        temp_csv = STAGING_DIR / f"temp_dams_{batch_name}.csv"
        batch_df.to_csv(temp_csv, index=False)
        
        try:
            # Step 1: Download ONLY the tiles required for this local batch
            print("Executing Step 1: Staging flowlines and downloading raw 3DEP DEM tiles...")
            subprocess.run([
                "python", "backend/stage_nhd_dem.py",
                "--staging-dir", str(STAGING_DIR),
                "--dams-csv", str(temp_csv),
                "--workers", "8",
                "--download-workers", "8"
            ], check=True)
            
            # Step 2: Trim per-dam DEMs + constant land cover raster + Manning_n.txt
            print("Executing Step 2: Trim per-dam DEMs plus constant land cover raster and Manning_n.txt...")
            subprocess.run([
                "python", "backend/build_trimmed_dems.py",
                "--staging-dir", str(STAGING_DIR),
                "--workers", "8"
            ], check=True)

            # Step 3: Rasterize NHD flowlines to the cleaned stream grid
            print("Executing Step 3: Rasterizing NHD flowlines to the cleaned stream grid...")
            subprocess.run([
                "python", "backend/build_stream_rasters.py",
                "--staging-dir", str(STAGING_DIR),
                "--workers", "8"
            ], check=True)
            
            # Step 4: Per-dam NWM streamflow CSVs (q_ep_50 + rp100)
            print("Executing Step 4: Generating NWM streamflow CSVs...")
            subprocess.run([
                "python", "backend/build_streamflow_csv.py",
                "--staging-dir", str(STAGING_DIR),
                "--workers", "8"
            ], check=True)

            # Step 5: Run ARC - writes VDT.txt, Curve.csv, XS.txt, arc_done.json
            print("Executing Step 5: Running ARC...")
            subprocess.run([
                "python", "backend/run_arc_batch.py",
                "--staging-dir", str(STAGING_DIR),
                "--workers", "8"
            ], check=True)

            # Step 6: Post-ARC geometry
            print("Executing Step 6: Post-ARC geometry processing...")
            subprocess.run([
                "python", "backend/run_analysis_batch.py",
                "--staging-dir", str(STAGING_DIR),
                "--workers", "8"
            ], check=True)

            # Step 7: Copy final summaries to a safe export location
            print("Executing Step 7: Copying final summaries to export directory...")
            results_dir = STAGING_DIR / "RESULTS"
            for summary_path in results_dir.glob("**/analysis_summary.json"):
                dam_id = summary_path.parent.name
                dest_path = FINAL_EXPORT_DIR / f"{dam_id}_analysis_summary.json"
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(summary_path, dest_path)

            
                
        except subprocess.CalledProcessError as e:
            print(f"Pipeline broken at batch {batch_name}: {e}")
            break
            
        finally:
            # Step E: PURGE the heavy raw inputs to free up space on your 2 TB drive
            print(f"Purging raw rasters for batch {batch_name} to clear space...")
            
            raw_dem_path = STAGING_DIR / "DEM" / "raw_3dep"
            local_dem_processed = STAGING_DIR / "DEM"
            raw_strm_path = STAGING_DIR / "STRM"
            
            # Wipe heavy raw 1-m downloads
            if raw_dem_path.exists():
                shutil.rmtree(raw_dem_path)
                raw_dem_path.mkdir(parents=True, exist_ok=True) # recreate clean empty directory
                
            # Wipe localized processed folder buffers if space is critically low
            # (Keep only if needed for subsequent cross-checks, otherwise clear them out)
            # shutil.rmtree(local_dem_processed)
            # shutil.rmtree(raw_strm_path)
            
            # Clean up the localized loop tracker file
            if temp_csv.exists():
                os.remove(temp_csv)
                
    print("\nAll batches completed successfully. Final summaries stored safely.")

if __name__ == "__main__":
    run_rolling_pipeline()