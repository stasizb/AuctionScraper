(function () {
  /* ── Utilities ─────────────────────────────────────────────── */
  function activePanel() {
    return document.querySelector('.tab-panel.active');
  }

  function updateCount(panel) {
    var countEl = document.getElementById('row-count');
    if (!countEl || !panel) return;
    var mainTable = panel.querySelector('.main-table');
    if (!mainTable) return;
    var visible = Array.from(mainTable.querySelectorAll('tbody tr:not(.no-results)'))
                       .filter(function (r) { return r.style.display !== 'none'; }).length;
    countEl.textContent = visible + ' row(s)';
  }

  function getCheckedModels(panel) {
    var cbs = panel.querySelectorAll('.model-cb:not([data-all])');
    if (!cbs.length) return null;
    var checked = Array.from(cbs).filter(function (c) { return c.checked; });
    if (checked.length === cbs.length) return null;   /* all checked = no filter */
    return new Set(checked.map(function (c) { return c.value; }));
  }

  function applyFilters(panel) {
    if (!panel) return;
    var searchInput = document.getElementById('search-input');
    var q      = searchInput ? searchInput.value.trim().toLowerCase() : '';
    var models = getCheckedModels(panel);

    panel.querySelectorAll('.filterable-table').forEach(function (table) {
      var tbody = table.tBodies[0];
      if (!tbody) return;
      var visible = 0;
      Array.from(tbody.rows).forEach(function (tr) {
        if (tr.classList.contains('no-results')) return;
        var modelOk = !models || models.has(tr.dataset.model || '');
        var textOk  = !q     || tr.textContent.toLowerCase().includes(q);
        var show    = modelOk && textOk;
        tr.style.display = show ? '' : 'none';
        if (show) visible++;
      });
      var noRes = tbody.querySelector('.no-results');
      if (noRes) noRes.style.display = visible === 0 ? '' : 'none';
    });

    updateCount(panel);
  }

  /* ── Tab switching ─────────────────────────────────────────── */
  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.tab-btn').forEach(function (b) { b.classList.remove('active'); });
      document.querySelectorAll('.tab-panel').forEach(function (p) { p.classList.remove('active'); });
      btn.classList.add('active');
      var panel = document.getElementById(btn.dataset.target);
      panel.classList.add('active');
      var searchInput = document.getElementById('search-input');
      if (searchInput) searchInput.value = '';
      applyFilters(panel);
    });
  });

  /* ── Text search ───────────────────────────────────────────── */
  var searchInput = document.getElementById('search-input');
  if (searchInput) {
    searchInput.addEventListener('input', function () {
      applyFilters(activePanel());
    });
  }

  /* ── Model filter ──────────────────────────────────────────── */
  document.querySelectorAll('.model-filter').forEach(function (filter) {
    filter.addEventListener('change', function (e) {
      var cb    = e.target;
      var panel = filter.closest('.tab-panel');
      if (cb.hasAttribute('data-all')) {
        filter.querySelectorAll('.model-cb:not([data-all])').forEach(function (c) {
          c.checked = cb.checked;
        });
      } else {
        var all        = Array.from(filter.querySelectorAll('.model-cb:not([data-all])'));
        var allChecked = all.every(function (c) { return c.checked; });
        var allCb      = filter.querySelector('[data-all]');
        if (allCb) allCb.checked = allChecked;
      }
      applyFilters(panel);
    });
  });

  /* ── Column sorting ─────────────────────────────────────────── */
  function rawVal(td) {
    return td.dataset.raw !== undefined ? td.dataset.raw : td.textContent.trim();
  }
  function numVal(s) { return parseFloat(String(s).replace(/[$,]/g, '')) || 0; }

  document.querySelectorAll('thead th').forEach(function (th) {
    if (th.closest('table.no-sort')) return;
    var ascending = true;
    th.addEventListener('click', function () {
      var table  = th.closest('table');
      var idx    = Array.from(th.parentNode.children).indexOf(th);
      var isNum  = th.dataset.type === 'number';
      var tbody  = table.tBodies[0];
      var rows   = Array.from(tbody.rows).filter(function (r) {
        return !r.classList.contains('no-results');
      });

      rows.sort(function (a, b) {
        var av = rawVal(a.cells[idx]);
        var bv = rawVal(b.cells[idx]);
        if (isNum) return ascending ? numVal(av) - numVal(bv) : numVal(bv) - numVal(av);
        return ascending ? av.localeCompare(bv, undefined, {numeric: true})
                         : bv.localeCompare(av, undefined, {numeric: true});
      });
      rows.forEach(function (r) { tbody.appendChild(r); });

      table.querySelectorAll('thead th').forEach(function (t) {
        t.classList.remove('asc', 'desc');
        var icon = t.querySelector('.sort-icon');
        if (icon) icon.textContent = ' ⇅';
      });
      th.classList.add(ascending ? 'asc' : 'desc');
      var icon = th.querySelector('.sort-icon');
      if (icon) icon.textContent = ascending ? ' ▲' : ' ▼';
      ascending = !ascending;
    });
  });

  /* ── Default sort: Auction Date descending (newest first) ───── */
  function sortByAuctionDateDesc(table) {
    var headers = table.querySelectorAll('thead th');
    for (var i = 0; i < headers.length; i++) {
      var th = headers[i];
      var first = null;
      th.childNodes.forEach(function (n) {
        if (!first && n.nodeType === Node.TEXT_NODE && n.textContent.trim()) {
          first = n.textContent.trim();
        }
      });
      if (first === 'Auction Date') {
        th.click();   /* first click: asc */
        th.click();   /* second click: desc */
        return;
      }
    }
  }

  document.querySelectorAll('table.filterable-table').forEach(sortByAuctionDateDesc);

  /* ── Mobile: start with details collapsed + menu closed ─────── */
  var mobileMQ = window.matchMedia('(max-width: 768px)');
  if (mobileMQ.matches) {
    document.querySelectorAll('details.summary-section, details.model-filter')
      .forEach(function (d) { d.removeAttribute('open'); });
  }

  /* Hamburger toggles the whole mobile menu (make tabs + model filter) */
  var hamburger = document.getElementById('mobile-menu-btn');
  if (hamburger) {
    hamburger.addEventListener('click', function () {
      var open = document.body.classList.toggle('menu-open');
      /* When the menu opens, also expand the model filter so it's usable */
      var panel = activePanel();
      if (panel) {
        var filter = panel.querySelector('details.model-filter');
        if (filter) filter.open = open;
      }
    });
  }

  /* Tapping a tab on mobile closes the menu so the cards are visible again */
  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (mobileMQ.matches) {
        document.body.classList.remove('menu-open');
      }
    });
  });

  /* ── Init sort icons + first panel ──────────────────────────── */
  document.querySelectorAll('thead th .sort-icon').forEach(function (el) {
    if (!el.textContent) el.textContent = ' ⇅';
  });
  var firstPanel = document.querySelector('.tab-panel.active');
  if (firstPanel) applyFilters(firstPanel);
})();
