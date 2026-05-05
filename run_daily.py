#!/usr/bin/env python3
"""
Daily auction pipeline orchestrator.

Execution is split into three phases to exploit available parallelism:

  Phase 1 (parallel chains)
    Chain A: copart_search.py → remove_duplicates.py
       1. scrape today's Copart lots                       (HTTP only)
       3. remove rescheduled Copart lots from yesterday    (file I/O)
    Chain B: iaai_search.py
       2. scrape today's IAAI lots                         (browser)
    Step 3 only needs step 1's output, so it chains right after — no
    reason to make it wait for the slower IAAI scrape to finish.

  Phase 2 (sequential) — runs after phase 1 completes
    5. bidfax_info.py iaai   — bidfax prices for yesterday's IAAI lots (browser)

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
import csv
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.chrome import find_chrome

# Child Python processes write to pipes in `run_parallel`. Block-buffered
# stdout means the user sees nothing until the subprocess exits — which
# looked like "IAAI didn't start" when it was actually just running
# silently behind buffers. Force line-buffered mode globally.
os.environ.setdefault("PYTHONUNBUFFERED", "1")


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

# Wall-clock seconds each step took. Skipped steps are absent.
_step_timings: dict[str, float] = {}

# How many cars/lots each step processed. Populated for the steps the user
# wants timing-per-car for: copart_search, iaai_search, and bidfax steps.
_car_counts:   dict[str, int]   = {}

# Canonical step names — used both as `run()`/`run_parallel()` labels and
# as keys in `_step_timings` / `_car_counts`. Defined once here so a
# rename in one place doesn't silently desync from the timing summary.
STEP_COPART_SEARCH      = "1. Copart search (today)"
STEP_IAAI_SEARCH        = "2. IAAI search (today)"
STEP_REMOVE_DUPLICATES  = "3. Remove Copart duplicates (yesterday vs today)"
STEP_BIDFAX_COPART      = "4. Bidfax prices — Copart (yesterday)"
STEP_BIDFAX_IAAI        = "5. Bidfax prices — IAAI (yesterday)"
STEP_PRICE_REFRESH      = "6. Refresh In Progress prices"
STEP_BUILD_WORKBOOK     = "7. Build Excel workbook"
STEP_HTML_REPORT        = "8. Generate HTML report"


def _record(name: str, status: str, detail: str = "") -> None:
    _step_results.append((name, status, detail))


def _record_timing(name: str, elapsed_seconds: float) -> None:
    _step_timings[name] = elapsed_seconds


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

    started = time.monotonic()
    result  = subprocess.run(cmd)
    _record_timing(step_name, time.monotonic() - started)
    if result.returncode != 0:
        print(f"\n[FAIL] {step_name} exited with code {result.returncode} — stopping.")
        _record(step_name, "fail", f"exit {result.returncode}")
        sys.exit(result.returncode)

    print(f"\n[OK] {step_name}")
    _record(step_name, "ok")


def _prefix_for(name: str) -> str:
    """Short tag like '[1]' derived from the leading step number in a name."""
    head = name.split(" ", 1)[0].rstrip(".")
    return f"[{head}] "


def run_parallel_chains(chains: list[list[tuple[str, list[str]]]]) -> None:
    """Run multiple chains concurrently and stream their output live.

    Each `chain` is a sequence of `(name, cmd)` steps that runs in order
    within its own thread; chains run in parallel. If any step inside a
    chain fails, the rest of that chain is skipped (its later steps are
    not attempted) but the other chains keep running. Exits if any
    chain failed at all, after all chains have finished.

    Output streaming is unchanged from the previous flat `run_parallel`:
    each child's stdout is read line-by-line, prefixed with the step's
    leading tag, and emitted under a shared lock so lines stay readable
    even when chains interleave. A single-step chain (`[[(name, cmd)]]`)
    is the parallel-of-one case.
    """
    _lock    = threading.Lock()
    failures: list[tuple[str, int]] = []

    def _exec_step(name: str, cmd: list[str]) -> int:
        """Run one step inside a chain. Returns the subprocess exit code."""
        prefix = _prefix_for(name)
        with _lock:
            print(f"\n{'=' * 60}", flush=True)
            print(f"[STEP] {name} [parallel]",  flush=True)
            print(f"{'=' * 60}",                flush=True)
            print(f"  cmd: {' '.join(cmd)}\n",  flush=True)

        started = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge so stderr stays in-order with stdout
            text=True,
            bufsize=1,                 # line-buffered
        )
        try:
            for line in proc.stdout:
                with _lock:
                    print(prefix + line, end="", flush=True)
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
        proc.wait()
        _record_timing(name, time.monotonic() - started)

        with _lock:
            if proc.returncode == 0:
                print(f"\n[OK] {name}", flush=True)
                _record(name, "ok")
            else:
                print(f"\n[FAIL] {name} exited with code {proc.returncode}", flush=True)
                _record(name, "fail", f"exit {proc.returncode}")
                failures.append((name, proc.returncode))
        return proc.returncode

    def _run_chain(chain: list[tuple[str, list[str]]]) -> None:
        for name, cmd in chain:
            if _exec_step(name, cmd) != 0:
                # Stop this chain; remaining steps are skipped because
                # they typically depend on the failing one's output.
                break

    threads = [threading.Thread(target=_run_chain, args=(c,)) for c in chains]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if failures:
        for name, rc in failures:
            print(f"\n[FAIL] {name} exited with code {rc} — stopping.")
        sys.exit(failures[0][1])


def _format_duration(seconds: float) -> str:
    """Render a duration in the most natural unit.

    < 60s   →  '12.3s'
    < 1h    →  '3m 45s'
    1h+     →  '1h 23m'
    """
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _count_csv_rows(path: Path) -> int:
    """Count data rows (excluding the header) in a CSV. 0 if the file
    doesn't exist or can't be read."""
    if not path.exists():
        return 0
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return max(0, sum(1 for _ in fh) - 1)
    except OSError:
        return 0


def _count_in_progress(output_dir: Path) -> int:
    """Count rows whose Price column is 'In Progress' across every
    `<auction>_price_<date>.csv` in `output_dir`. This is the input size
    for step 6 (price_refresh) — sampled before the step runs so the
    summary's 'time per car' is meaningful."""
    total = 0
    for path in sorted(output_dir.glob("*_price_*.csv")):
        try:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    if (row.get("Price") or "").strip() == "In Progress":
                        total += 1
        except OSError:
            continue
    return total


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

    _print_timing_section()


def _print_timing_section() -> None:
    """Render the timing/throughput block. Skipped when no timings were
    captured (e.g. dry-run with only skips). Steps that don't track car
    counts (remove-duplicates, build-workbook, html generation) are
    intentionally absent — per-car timing isn't meaningful for them."""
    if not _step_timings:
        return

    total_elapsed = sum(_step_timings.values())
    new_cars      = sum(_car_counts.get(k, 0)
                        for k in (STEP_COPART_SEARCH, STEP_IAAI_SEARCH))

    print("\n[TIMING]")
    print(f"  Total time : {_format_duration(total_elapsed)}")
    print(f"  New cars   : {new_cars}")

    sections: list[tuple[str, list[str]]] = [
        ("Copart search", [STEP_COPART_SEARCH]),
        ("IAAI search",   [STEP_IAAI_SEARCH]),
        ("Bidfax",        [STEP_BIDFAX_COPART, STEP_BIDFAX_IAAI, STEP_PRICE_REFRESH]),
    ]
    label_width = max(len(label) for label, _ in sections)
    for label, step_keys in sections:
        secs = sum(_step_timings.get(k, 0) for k in step_keys)
        cars = sum(_car_counts.get(k, 0)   for k in step_keys)
        if secs == 0 and cars == 0:
            # All sub-steps were skipped — nothing to show.
            continue
        per_car = (f"{_format_duration(secs / cars)} per car"
                   if cars > 0 else "—")
        print(f"  {label.ljust(label_width)} : "
              f"{_format_duration(secs)}  ·  {cars} cars  ·  {per_car}")


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
        # Resolve yesterday's search dates up front so phase 1 can chain
        # step 3 (which depends on them) onto the copart-search step.
        copart_date = _find_recent_search(output, "copart", _yesterday)
        iaai_date   = _find_recent_search(output, "iaai",   _yesterday)

        # Bidfax-step inputs come from yesterday's search CSVs — record
        # those counts now so the timing summary has them even if a step
        # fails mid-run.
        if copart_date:
            _car_counts[STEP_BIDFAX_COPART] = _count_csv_rows(
                output / f"copart_search_{copart_date}.csv")
        if iaai_date:
            _car_counts[STEP_BIDFAX_IAAI] = _count_csv_rows(
                output / f"iaai_search_{iaai_date}.csv")

        # ---- Phase 1 (parallel chains) -----------------------------------
        # Chain A: copart_search → remove_duplicates. Step 3 only needs
        #          step 1's output, so it can start as soon as step 1
        #          finishes — no need to wait for step 2 (IAAI).
        # Chain B: iaai_search alone. Browser-bound, the long pole.
        chain_copart: list[tuple[str, list[str]]] = [
            (STEP_COPART_SEARCH, [
                py, s("copart_search.py"),
                "--input",  str(filters / "copart_filters.csv"),
                "--output", o(f"copart_search_{today}.csv"),
            ]),
        ]
        if copart_date:
            chain_copart.append((STEP_REMOVE_DUPLICATES, [
                py, s("remove_duplicates.py"),
                "--auction", "copart",
                "--src",  o(f"copart_search_{copart_date}.csv"),
                "--dest", o(f"copart_search_{today}.csv"),
            ]))
        else:
            skip(STEP_REMOVE_DUPLICATES, "no recent copart search file found")

        run_parallel_chains([
            chain_copart,
            [(STEP_IAAI_SEARCH, [
                py, s("iaai_search.py"),
                "--input",       str(filters / "iaai_filters.csv"),
                "--output",      o(f"iaai_search_{today}.csv"),
                "--profile-dir", str(chrome_profile),
                *bp,
            ])],
        ])

        # Phase 1 done — capture how many lots each scraper produced.
        _car_counts[STEP_COPART_SEARCH] = _count_csv_rows(output / f"copart_search_{today}.csv")
        _car_counts[STEP_IAAI_SEARCH]   = _count_csv_rows(output / f"iaai_search_{today}.csv")

        # ---- Phase 2 (sequential): IAAI bidfax for yesterday -------------
        # Step 3 used to live here alongside step 5; with step 3 chained
        # into phase 1, only step 5 remains.
        if iaai_date:
            run(STEP_BIDFAX_IAAI, [
                py, s("bidfax_info.py"),
                "--auction", "iaai",
                "--date",    iaai_date,
                "--dir",     str(output),
                "--cache",   bidfax_cache,
                "--log",     str(logs / "bidfax_deletions.json"),
                *bp,
            ])
        else:
            skip(STEP_BIDFAX_IAAI, "no recent iaai search file found")

        # ---- Phase 3 (sequential): Copart bidfax → refresh → workbook → HTML
        # Steps 4, 6, 7, 8 share bidfax_cache.json and/or auction_results.xlsx;
        # they must remain sequential.
        if copart_date:
            run(STEP_BIDFAX_COPART, [
                py, s("bidfax_info.py"),
                "--auction", "copart",
                "--date",    copart_date,
                "--dir",     str(output),
                "--cache",   bidfax_cache,
                "--log",     str(logs / "bidfax_deletions.json"),
                *bp,
            ])
        else:
            skip(STEP_BIDFAX_COPART, "no recent copart search file found")

        # Step 6's input is "all In-Progress rows in price CSVs at this
        # moment" — sample it before the step runs so the timing summary's
        # 'time per car' is meaningful (after the step, those rows are
        # priced and the count would be ~0).
        _car_counts[STEP_PRICE_REFRESH] = _count_in_progress(output)

        # Step 6 deliberately does NOT use the shared Chrome (no --browser-port).
        # The shared session accumulates bidfax cookies / reCAPTCHA score from
        # step 4 + step 5; if those are throttled when step 6 starts, every
        # query bounces. Spawning a fresh Chrome here gives price_refresh a
        # clean session, restoring the recovery rate to standalone-run levels.
        run(STEP_PRICE_REFRESH, [
            py, s("price_refresh.py"),
            "--dir",      str(output),
            "--cache",    bidfax_cache,
            "--workbook", workbook,
        ])

        run(STEP_BUILD_WORKBOOK, [
            py, s("build_workbook.py"),
            "--dir",      str(output),
            "--workbook", workbook,
            "--log",      str(logs / "processed_files.json"),
        ])

        run(STEP_HTML_REPORT, [
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
