# TODO

- [x] Check base URL for IAAI when Auction date and Run & Drive preselected.
      Done: `BrowserIAAIClient._resolve_base_url` opens /Search at startup,
      applies Run & Drive + Auction Today + Odometer 30000, and captures
      the resulting `?url=ENCODED_HASH` URL. Workers navigate straight to
      that URL and skip those clicks. Per-row `odometer_max` is ignored
      for now — see follow-up below.

- [ ] Restore per-row Odometer max for IAAI. Currently the base URL bakes in
      Odometer max = 30000 and `_scrape_one` ignores any per-row value with
      a log warning. Either parametrize the base URL per row (one warmup
      per distinct odometer) or apply odometer post-base-URL navigation.

- [x] Why do we have two sessions for bidfax? Investigate whether bidfax
      launches Chrome more than once per script run (or per daily pipeline)
      and whether the sessions can be consolidated into one.

- [x] Remove too-old lots without a price from the workbook.
      Done: `bidfax_run.py` deletes In-Progress rows whose Auction Date is
      more than `--stale-cutoff-days` days old (default 7) from price CSVs
      AND the workbook. Pure date helpers (`_parse_auction_date`,
      `_is_stale`) are conservative — unparseable dates never trigger
      deletion. Stale lots are also dropped from the bidfax queue so we
      don't waste a query on rows we're about to delete.

- [x] Group messages from concurrent threads/tabs in the logs.
      Done: `core/job_log.py` installs a contextvars-aware sys.stdout proxy;
      each parallel worker enters `async with job_log():` and its prints
      buffer per-task, flushing as one contiguous block at exit. Applied
      to IAAI's filter-row workers and bidfax's per-lot workers.
