# TODO

- [ ] Check base URL for IAAI when Auction date and Run & Drive preselected.
      Currently `clients/iaai.py` navigates to `https://www.iaai.com/Search`
      and clicks "Run & Drive" + "Auction Today" featured filters per tab.
      If IAAI exposes a URL with these filters baked in (query params), use
      it as `IAAI_SEARCH_URL` to skip those clicks — saves ~2-3s per tab.

- [ ] Why do we have two sessions for bidfax? Investigate whether bidfax
      launches Chrome more than once per script run (or per daily pipeline)
      and whether the sessions can be consolidated into one.

- [ ] Remove too-old lots without a price from the workbook. Lots that have
      sat past their auction date with no resolved bidfax price are stale —
      define a cutoff (days since auction?) and prune them so the workbook
      doesn't grow unbounded.
