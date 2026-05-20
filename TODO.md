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

- [ ] Replace bidfax with direct Copart / IAAI sold-price lookups.
      Today bidfax_run.py opens a Chromium session per lot (in-place
      flow as of 4d71c3e — Cloudflare + reCAPTCHA + ~10s/lot wall time)
      just to read the final sale price + VIN + canonical URL. Bidfax
      itself is a scraper-aggregator that ingests this from Copart's /
      IAAI's own public-facing sold-lot data, then re-hosts it behind
      bot-defense. If we can hit the original source instead, we drop
      bidfax entirely and reuse the Playwright cookie warmups we
      already have for Copart (clients/copart_session.py) and IAAI
      (clients/iaai_session.py).

      Three open questions to answer before doing anything:

       1. **Does Copart's public API expose `soldFor` / `highBid` for
          ended lots?** Our `/public/lots/search-results` only returns
          active auctions, but the lot-detail SPA almost certainly
          fires an XHR with the sold price (we never sniffed it — the
          recon was focused on the search step). A ~10-min recon will
          show whether such a field exists in any reachable endpoint.

       2. **Same for IAAI's `Past Auctions`.** Their public page is
          unauthenticated; if `/Search?c=<ts>` accepts a past-date
          filter (or there's a sibling endpoint), we can plug it into
          the existing iaai_session warmup.

       3. **Coverage.** Even if both expose sold prices, do they cover
          the same set bidfax does? Sometimes auction sites prune
          ended lots from their public surface within hours; bidfax
          may still hold them. If so, bidfax remains the fallback for
          the long tail.

      Outcomes:
        * 1 + 2 + good coverage → replace bidfax entirely. bidfax_run.py
          becomes a thin coordinator over Copart/IAAI session clients.
        * 1 XOR 2 good → replace half of bidfax (one auction's lots
          take the new path, the other still uses bidfax).
        * Neither → bidfax stays; possible smaller win by replaying
          its form-submit POST with a fetched reCAPTCHA v3 token from
          a single short Playwright session (skip per-lot browser).
