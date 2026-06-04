"""
Download combined VAHAN sales for all India (3 Excel files).

Selects "All Vahan4 Running States" on the State dropdown, then
"All Vahan4 Running Office" on the RTO dropdown (country-wide combined data).

Run from project root:
  python state/download_india_combined.py

Env:
  DOWNLOAD_PATH   output root (default: download_reports/)
  HEADLESS        1 = headless browser
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent
ROOT = STATE_DIR.parent
INDIA_STATE_MENU_LABEL = "All Vahan4 Running States (36/36)"
INDIA_DOWNLOAD_SUBDIR = "india"
STATE_DOWNLOAD_SUBDIR = INDIA_DOWNLOAD_SUBDIR  # used by controller.py logs
DEFAULT_DOWNLOAD_PATH = os.path.join(ROOT, "download_reports")


def _load_vahan() -> object:
    path = STATE_DIR / "uttar_pradesh.py"
    spec = importlib.util.spec_from_file_location("vahan_up", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_india_combined_downloads(
    base_download_dir: str,
    *,
    headless: bool,
) -> list[str]:
    v = _load_vahan()
    v.STATE_DOWNLOAD_SUBDIR = INDIA_DOWNLOAD_SUBDIR

    url = v.REPORT_URL
    saved_paths: list[str] = []

    with v.sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=v._browser_launch_args(headless),
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 720} if headless else None,
            no_viewport=not headless,
        )
        page = context.new_page()
        page.set_default_timeout(v.DEFAULT_TIMEOUT)

        print(f"\nSelecting India: {INDIA_STATE_MENU_LABEL}")
        v._goto_and_select_state(page, url, INDIA_STATE_MENU_LABEL)

        merged = v.list_merged_rto_labels(page)
        combined_label = v.find_combined_state_rto_label(merged)
        v.save_combined_rto_info(base_download_dir, combined_label)

        out_root = os.path.join(
            base_download_dir, INDIA_DOWNLOAD_SUBDIR, v.COMBINED_RTO_FOLDER
        )
        print(f"\n========== India combined: {combined_label} ==========")
        print(f"Saving under: {out_root}/")
        v._setup_rto_report(page, combined_label)
        paths = v._download_three_filters_for_rto(
            page,
            base_download_dir,
            combined_label,
            output_subfolder=v.COMBINED_RTO_FOLDER,
        )
        saved_paths.extend(paths)

        context.close()
        browser.close()

    print("Browser closed")
    print(f"Total files saved: {len(saved_paths)}")
    return saved_paths


def run_downloads(
    base_download_dir: str,
    *,
    headless: bool,
    report_url: str | None = None,
    rto_limit: int | None = None,
) -> list[str]:
    """Entry point for controller.py (country-wide combined data only)."""
    del report_url, rto_limit
    return run_india_combined_downloads(base_download_dir, headless=headless)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    download_path = os.environ.get("DOWNLOAD_PATH", DEFAULT_DOWNLOAD_PATH)
    os.makedirs(download_path, exist_ok=True)
    headless = os.environ.get("HEADLESS", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    while True:
        try:
            print(
                f"\nStarting India combined download -> "
                f"{download_path}/{INDIA_DOWNLOAD_SUBDIR}/all_vahan4_running_office/"
            )
            paths = run_india_combined_downloads(download_path, headless=headless)
            print("\nAll downloads complete:")
            for path in paths:
                print(" ", path)
            break
        except Exception as exc:
            print("\nERROR:", exc)
            print("Retrying in 30 min...")
            time.sleep(1800)


if __name__ == "__main__":
    main()
