#!/usr/bin/env python3
"""
Daily auction pipeline orchestrator.

Execution is split into three phases to exploit available parallelism:

  Phase 1 (parallel)
    1. copart_search.py      — scrape today's Copart lots   (HTTP only)
    2. iaai_search.py        — scrape today's IAAI lots     (browser)

  Phase 2 (parallel)  — runs after phase 1 completes
    3. remove_duplicates.py  — remove rescheduled Copart lots (file I/O only)
    5. bidfax_info.py iaai   — bidfax prices for yesterday's IAAI lots (browser)
    Steps 3 and 5 touch entirely different files, so they are safe to overlap.

  Phase 3 (sequential) — steps share bidfax_cache.json / auction_results.xlsx
    4. bidfax_info.py copart — check Sale ended + bidfax prices for yesterday's Copart lots
    6. price_refresh.py      — retry all In Progress lots across all price CSVs
    7. build_workbook.py     — aggregate price CSVs into Excel workbook
    8. workbook_to_html.py   — generate HTML report from workbook

Directory layout expected next to the scripts:
    filters/   — copart_filters.csv, iaai_filters.csv
    caches/    — bidfax_cache.json
    logs/      — bidfax_deletions.json, processed_files.json
    output/    — search/price CSVs, workbook, html_report/

Stops immediately if any step fails.

USAGE:
    python run_daily.py
    python run_daily.py --root /path/to/project
"""

import argparse
import socket
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.chrome import find_chrome


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cdp_ready(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _start_shared_chrome(profile_dir: Path) -> tuple[subprocess.Popen, int]:
    """Launch a single Chrome instance shared across all pipeline steps."""
    port = _free_port()
    profile_dir.mkdir(parents=True, exist_ok=True)
    chrome_exe = find_chrome()
    proc = subprocess.Popen(
        [
            chrome_exe,
            f"--remote-debugging-port={port}",
            "--remote-debugging-host=127.0.0.1",
            "--no-first-run", "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-session-crashed-bubble",
            "--window-size=1400,900",
            f"--user-data-dir={profile_dir}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _cdp_ready(port):
        proc.terminate()
        raise RuntimeError(f"Shared Chrome did not start on port {port}")
    print(f"[*] Shared Chrome started on port {port}")
    return proc, port


def _find_recent_search(output_dir: Path, auction: str, before: date, max_days: int = 7) -> str | None:
    """Return the date string (YYYY_MM_DD) of the most recent <auction>_search_<date>.csv
    that exists in output_dir, starting from `before` and going back up to max_days.
    Returns None if no file is found.
    """
    for offset in range(max_days):
        candidate = before - timedelta(days=offset)
        path = output_dir / f"{auction}_search_{candidate.strftime('%Y_%m_%d')}.csv"
        if path.exists():
            if offset > 0:
                print(f"  [*] {auction} search file for {before} not found — "
                      f"using {candidate} (-{offset}d)")
            return candidate.strftime("%Y_%m_%d")
    return None


# ---------------------------------------------------------------------------
# Step-status tracking (for end-of-run summary)
# ---------------------------------------------------------------------------

# Ordered list of (name, status, detail) tuples, populated as the pipeline runs.
# status is one of: "ok", "fail", "skipped"
_step_results: list[tuple[str, str, str]] = []


def _record(name: str, status: str, detail: str = "") -> None:
    _step_results.append((name, status, detail))


def skip(step_name: str, reason: str) -> None:
    """Record a skipped step in the summary (non-failing)."""
    print(f"\n[SKIP] {step_name} — {reason}")
    _record(step_name, "skipped", reason)


def run(step_name: str, cmd: list[str]) -> None:
    """Run a command, printing a header. Exit if the process fails."""
    print(f"\n{'=' * 60}")
    print(f"[STEP] {step_name}")
    print(f"{'=' * 60}")
    print(f"  cmd: {' '.join(cmd)}\n")

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n[FAIL] {step_name} exited with code {result.returncode} — stopping.")
        _record(step_name, "fail", f"exit {result.returncode}")
        sys.exit(result.returncode)

    print(f"\n[OK] {step_name}")
    _record(step_name, "ok")


def run_parallel(steps: list[tuple[str, list[str]]]) -> None:
    """Run steps concurrently; print each step's output atomically on completion.

    Each step's stdout/stderr is buffered so output from different steps is never
    interleaved. All steps are awaited before returning. Exits if any step fails.
    """
    _lock    = threading.Lock()
    failures: list[tuple[str, int]] = []

    def _run_one(name: str, cmd: list[str]) -> None:
        header = (
            f"\n{'=' * 60}\n[STEP] {name} [parallel]\n{'=' * 60}\n"
            f"  cmd: {' '.join(cmd)}\n\n"
        )
        proc   = subprocess.run(cmd, capture_output=True, text=True)
        footer = (
            f"\n[OK] {name}\n"
            if proc.returncode == 0
            else f"\n[FAIL] {name} exited with code {proc.returncode}\n"
        )
        with _lock:
            print(header + (proc.stdout or "") + (proc.stderr or "") + footer, flush=True)
            if proc.returncode == 0:
                _record(name, "ok")
            else:
                _record(name, "fail", f"exit {proc.returncode}")
                failures.append((name, proc.returncode))

    threads = [threading.Thread(target=_run_one, args=s) for s in steps]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if failures:
        for name, rc in failures:
            print(f"\n[FAIL] {name} exited with code {rc} — stopping.")
        sys.exit(failures[0][1])


def _print_summary() -> None:
    if not _step_results:
        return
    print(f"\n{'=' * 60}")
    print("[SUMMARY] Daily pipeline")
    print(f"{'=' * 60}")

    counts   = {"ok": 0, "fail": 0, "skipped": 0}
    width    = max(len(name) for name, _, _ in _step_results)
    for name, status, detail in _step_results:
        counts[status] = counts.get(status, 0) + 1
        mark = {"ok": "✓", "fail": "✗", "skipped": "-"}.get(status, "?")
        label = {"ok": "OK", "fail": "FAIL", "skipped": "SKIPPED"}.get(status, status.upper())
        line = f"  {mark}  {name.ljust(width)}    {label}"
        if detail:
            line += f"  ({detail})"
        print(line)

    print(f"\n  totals: {counts['ok']} ok, "
          f"{counts['skipped']} skipped, "
          f"{counts['fail']} failed")


def main() -> None:
    _today     = date.today()
    _yesterday = _today - timedelta(days=1)
    today      = _today.strftime("%Y_%m_%d")

    parser = argparse.ArgumentParser(
        description="Run the full daily auction pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--root", "-r", default=".",
                        help="Project root directory (default: current dir)")
    parser.add_argument("--python", default=sys.executable,
                        help="Python interpreter to use (default: current interpreter)")
    args = parser.parse_args()

    root    = Path(args.root).resolve()
    py      = args.python
    filters = root / "filters"
    logs    = root / "logs"
    output  = root / "output"

    def s(name: str) -> str:
        """Full path to a script inside the scripts/ directory."""
        return str(root / "scripts" / name)

    def o(name: str) -> str:
        """Full path to a file in the output directory."""
        return str(output / name)

    workbook     = o("auction_results.xlsx")
    bidfax_cache = str(root / "caches" / "bidfax_cache.json")
    chrome_profile = root / "caches" / "chrome_profile_shared"

    chrome_proc, browser_port = _start_shared_chrome(chrome_profile)
    bp = ["--browser-port", str(browser_port)]

    try:
        # ---- Phase 1 (parallel): today's search scrapers -----------------
        # Step 1 uses HTTP only; step 2 uses the browser. No shared files.
        run_parallel([
            ("1. Copart search (today)", [
                py, s("copart_search.py"),
                "--input",  str(filters / "copart_filters.csv"),
                "--output", o(f"copart_search_{today}.csv"),
            ]),
            ("2. IAAI search (today)", [
                py, s("iaai_search.py"),
                "--input",       str(filters / "iaai_filters.csv"),
                "--output",      o(f"iaai_search_{today}.csv"),
                "--profile-dir", str(chrome_profile),
                *bp,
            ]),
        ])

        copart_date = _find_recent_search(output, "copart", _yesterday)
        iaai_date   = _find_recent_search(output, "iaai",   _yesterday)

        # ---- Phase 2 (parallel): dedup + IAAI bidfax ---------------------
        # Step 3 is pure file I/O (no browser, no cache).
        # Step 5 uses the browser and bidfax cache.
        # They touch entirely different files, so running together is safe.
        phase2: list[tuple[str, list[str]]] = []
        if copart_date:
            phase2.append(("3. Remove Copart duplicates (yesterday vs today)", [
                py, s("remove_duplicates.py"),
                "--auction", "copart",
                "--src",  o(f"copart_search_{copart_date}.csv"),
                "--dest", o(f"copart_search_{today}.csv"),
            ]))
        else:
            skip("3. Remove Copart duplicates (yesterday vs today)",
                 "no recent copart search file found")

        if iaai_date:
            phase2.append(("5. Bidfax prices — IAAI (yesterday)", [
                py, s("bidfax_info.py"),
                "--auction", "iaai",
                "--date",    iaai_date,
                "--dir",     str(output),
                "--cache",   bidfax_cache,
                "--log",     str(logs / "bidfax_deletions.json"),
                *bp,
            ]))
        else:
            skip("5. Bidfax prices — IAAI (yesterday)",
                 "no recent iaai search file found")

        if len(phase2) > 1:
            run_parallel(phase2)
        elif phase2:
            run(*phase2[0])

        # ---- Phase 3 (sequential): Copart bidfax → refresh → workbook → HTML
        # Steps 4, 6, 7, 8 share bidfax_cache.json and/or auction_results.xlsx;
        # they must remain sequential.
        if copart_date:
            run("4. Bidfax prices — Copart (yesterday)", [
                py, s("bidfax_info.py"),
                "--auction", "copart",
                "--date",    copart_date,
                "--dir",     str(output),
                "--cache",   bidfax_cache,
                "--log",     str(logs / "bidfax_deletions.json"),
                *bp,
            ])
        else:
            skip("4. Bidfax prices — Copart (yesterday)",
                 "no recent copart search file found")

        run("6. Refresh In Progress prices", [
            py, s("price_refresh.py"),
            "--dir",      str(output),
            "--cache",    bidfax_cache,
            "--workbook", workbook,
            *bp,
        ])

        run("7. Build Excel workbook", [
            py, s("build_workbook.py"),
            "--dir",      str(output),
            "--workbook", workbook,
            "--log",      str(logs / "processed_files.json"),
        ])

        run("8. Generate HTML report", [
            py, s("workbook_to_html.py"),
            "--workbook",     workbook,
            "--out",          o("html_report"),
            "--search-dir",   str(output),
            "--today-date",   today,
            "--bidfax-cache", bidfax_cache,
            *bp,
        ])

    finally:
        chrome_proc.terminate()
        print("\n[*] Shared Chrome terminated.")
        _print_summary()

    print(f"\n{'=' * 60}")
    print("[DONE] Daily pipeline completed successfully.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
