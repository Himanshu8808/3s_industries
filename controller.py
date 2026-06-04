"""
Run every state/*.py script in parallel (36 states + download_india_combined.py).

Each script saves under its own folder under DOWNLOAD_PATH. Excel files use
date+time in the filename so older files in the same folder are kept.

Environment:
  DOWNLOAD_PATH          where reports are saved (default: download_reports/)
  HEADLESS               0/false = visible browser (default); 1 = headless (no windows)
  DOWNLOAD_MODE          both (default) | combined | individual — see state/*.py
  STATE_FILTER           comma-separated module names to run only those, e.g. uttar_pradesh,maharashtra
"""
from __future__ import annotations

import concurrent.futures
import csv
import importlib.util
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

MAX_WORKERS = 8

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
CSV_LOG = LOG_DIR / "state_download_history.csv"
TEXT_LOG = LOG_DIR / "state_download_latest.txt"

CSV_FIELDS = [
    "run_id",
    "state_module",
    "state_folder",
    "status",
    "started_at",
    "ended_at",
    "duration_sec",
    "files_downloaded",
    "error",
]

logger = logging.getLogger("controller")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _discover_state_files() -> list[Path]:
    files = sorted(p for p in STATE_DIR.glob("*.py") if p.name != "__init__.py")
    filt = os.environ.get("STATE_FILTER", "").strip()
    if filt:
        allowed = {x.strip().lower().replace(".py", "") for x in filt.split(",")}
        files = [p for p in files if p.stem.lower() in allowed]
    return files


def _load_module(py_path: Path):
    spec = importlib.util.spec_from_file_location(
        f"state_job_{py_path.stem}", py_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {py_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolve_run_fn(mod) -> object:
    """
    Pick the download entry point without touching missing attributes.

    getattr(mod, "run_downloads", mod.run_all_filter_downloads) is unsafe:
    Python evaluates the default before getattr runs, so modules that only
    define run_downloads (e.g. download_india_combined) still crash.
    """
    for name in ("run_downloads", "run_india_combined_downloads", "run_all_filter_downloads"):
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    raise AttributeError(
        f"{getattr(mod, '__file__', mod)} has no run_downloads, "
        "run_india_combined_downloads, or run_all_filter_downloads"
    )


def _run_one_state(
    py_path: str,
    download_path: str,
    headless: bool,
    run_id: str,
) -> dict:
    """Executed in a child process — one state, one pass (no 30 min retry loop)."""
    path = Path(py_path)
    module_name = path.stem
    state_folder = ""
    started = _now_iso()
    t0 = datetime.now()

    try:
        mod = _load_module(path)
        state_folder = getattr(mod, "STATE_DOWNLOAD_SUBDIR", module_name)
        run_fn = _resolve_run_fn(mod)
        saved = run_fn(download_path, headless=headless)
        files_count = len(saved) if saved else 0
        status = "success" if files_count > 0 else "failed"
        error = "" if files_count > 0 else "no files saved"
    except Exception as exc:
        status = "failed"
        files_count = 0
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        traceback.print_exc()

    ended = _now_iso()
    duration = round((datetime.now() - t0).total_seconds(), 1)

    return {
        "run_id": run_id,
        "state_module": module_name,
        "state_folder": state_folder,
        "status": status,
        "started_at": started,
        "ended_at": ended,
        "duration_sec": duration,
        "files_downloaded": files_count,
        "error": error,
    }


def _append_csv_row(row: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_LOG.exists()
    with open(CSV_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _write_text_summary(run_id: str, rows: list[dict]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ok = sum(1 for r in rows if r["status"] == "success")
    fail = len(rows) - ok
    lines = [
        f"Controller run: {run_id}",
        f"Finished at: {_now_iso()}",
        f"Total states: {len(rows)}  |  success: {ok}  |  failed: {fail}",
        "",
    ]
    for r in sorted(rows, key=lambda x: x["state_module"]):
        mark = "OK" if r["status"] == "success" else "FAIL"
        lines.append(
            f"[{mark}] {r['state_module']:28}  "
            f"files={r['files_downloaded']:4}  "
            f"{r['duration_sec']:8}s  "
            f"{r['started_at']} -> {r['ended_at']}"
        )
        if r["error"]:
            lines.append(f"       error: {r['error']}")
    lines.append("")
    lines.append(f"CSV log: {CSV_LOG}")
    TEXT_LOG.write_text("\n".join(lines), encoding="utf-8")


def _log_summary(
    total: int,
    succeeded: list[str],
    failed: list[str],
    elapsed_sec: float,
) -> None:
    logger.info("=" * 60)
    logger.info("CONTROLLER SUMMARY")
    logger.info("=" * 60)
    logger.info("Total scripts run: %s", total)
    logger.info("Succeeded: %s", len(succeeded))
    logger.info("Failed: %s", len(failed))
    if failed:
        logger.info("Failed script names:")
        for name in sorted(failed):
            logger.info("  - %s", name)
    else:
        logger.info("Failed script names: (none)")
    logger.info("Total duration: %.1f seconds (%.1f minutes)", elapsed_sec, elapsed_sec / 60)
    logger.info("Text log: %s", TEXT_LOG)
    logger.info("CSV log: %s", CSV_LOG)
    logger.info("=" * 60)


def main() -> int:
    _configure_logging()

    job_start = time.monotonic()
    job_start_iso = _now_iso()
    logger.info("Job start time: %s", job_start_iso)

    download_path = os.environ.get(
        "DOWNLOAD_PATH", str(ROOT / "download_reports")
    )
    os.makedirs(download_path, exist_ok=True)

    os.environ.setdefault("DOWNLOAD_MODE", "both")
    download_mode = os.environ["DOWNLOAD_MODE"].strip().lower()

    headless = os.environ.get("HEADLESS", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    os.environ["HEADLESS"] = "1" if headless else "0"

    state_files = _discover_state_files()
    if not state_files:
        logger.error("No state scripts found in %s", STATE_DIR)
        return 1

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    total_scripts = len(state_files)

    logger.info("Controller run id: %s", run_id)
    logger.info("Scripts to run: %s", total_scripts)
    logger.info("Parallel workers: %s", MAX_WORKERS)
    logger.info("Download path: %s", download_path)
    logger.info("DOWNLOAD_MODE: %s", download_mode)
    logger.info("Headless: %s", headless)
    if headless:
        logger.info("Browsers: hidden (set HEADLESS=0 to show up to %s windows)", MAX_WORKERS)
    else:
        logger.info(
            "Browsers: up to %s visible Chromium windows at once (one per parallel worker)",
            MAX_WORKERS,
        )

    jobs = [
        (str(p.resolve()), download_path, headless, run_id) for p in state_files
    ]
    rows: list[dict] = []
    succeeded: list[str] = []
    failed: list[str] = []

    with concurrent.futures.ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures: dict[concurrent.futures.Future, str] = {}
        for job in jobs:
            script_name = Path(job[0]).stem
            logger.info("Starting: %s", script_name)
            fut = pool.submit(_run_one_state, *job)
            futures[fut] = script_name

        for fut in concurrent.futures.as_completed(futures):
            script_name = futures[fut]
            try:
                row = fut.result()
            except Exception:
                row = {
                    "run_id": run_id,
                    "state_module": script_name,
                    "state_folder": "",
                    "status": "failed",
                    "started_at": _now_iso(),
                    "ended_at": _now_iso(),
                    "duration_sec": 0,
                    "files_downloaded": 0,
                    "error": traceback.format_exc(),
                }
                logger.error(
                    "Worker crashed for %s:\n%s", script_name, row["error"]
                )

            rows.append(row)
            _append_csv_row(row)

            if row["status"] == "success":
                succeeded.append(script_name)
                logger.info(
                    "Finished successfully: %s (%s files, %.1fs)",
                    script_name,
                    row["files_downloaded"],
                    row["duration_sec"],
                )
            else:
                failed.append(script_name)
                logger.error("Failed: %s", script_name)
                if row.get("error"):
                    logger.error("Error details for %s:\n%s", script_name, row["error"])

    _write_text_summary(run_id, rows)

    elapsed = time.monotonic() - job_start
    _log_summary(total_scripts, succeeded, failed, elapsed)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
