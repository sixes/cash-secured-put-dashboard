// Patches values that the server already rendered. Every number on the page is
// readable without this file; SSE only overwrites cells that have since moved.
(function () {
  "use strict";

  var FMT = {
    bid: 2,
    ask: 2,
    mid: 3,
    rel_spread_pct: 2,
    premium_pct: 3,
    annualized_pct: 2,
  };
  var SUFFIX = { rel_spread_pct: "%" };

  function fmt(field, value) {
    if (value === null || value === undefined) return "\u2014";
    return value.toFixed(FMT[field]) + (SUFFIX[field] || "");
  }

  function setText(el, text) {
    if (!el || el.textContent === text) return;
    el.textContent = text;
    el.classList.remove("flash");
    // Reading offsetWidth restarts the animation; without it a cell that changes
    // twice in a row only flashes once.
    void el.offsetWidth;
    el.classList.add("flash");
  }

  function flagFor(row) {
    if (row.bid === null || row.bid === undefined || row.bid <= 0) {
      return { text: "no bid", cls: "badge bad" };
    }
    if (!row.liquid) return { text: "wide", cls: "badge warn" };
    return null;
  }

  function setFlag(cell, flag) {
    if (!cell) return;
    var badge = cell.firstElementChild;
    if (!flag) {
      if (badge) cell.removeChild(badge);
      return;
    }
    if (!badge) {
      badge = document.createElement("span");
      cell.appendChild(badge);
    }
    badge.className = flag.cls;
    badge.textContent = flag.text;
  }

  function patchStatus(status) {
    var dot = document.querySelector("[data-status-dot]");
    var label = document.querySelector("[data-status-label]");
    var quota = document.querySelector(".quota");
    if (dot) {
      dot.className =
        "dot " +
        (!status.connected
          ? "bad"
          : status.stale && status.session_open
            ? "warn"
            : status.session_open
              ? "ok"
              : "idle");
    }
    if (label) label.textContent = status.label;
    if (quota) {
      quota.textContent =
        "quota " +
        status.quota_spent +
        "/" +
        (status.quota_spent + status.quota_available) +
        " per min";
    }
  }

  function patchPanel(panel, data) {
    var spot = panel.querySelector("[data-spot]");
    if (spot && data.spot !== null && data.spot !== undefined) {
      setText(spot, data.spot.toFixed(2));
    }

    // One match per tenor, so membership rather than equality. The frame carries rows
    // only for subscribed contracts; every other row keeps its server-rendered dashes.
    var matches = data.matches || [];
    (data.rows || []).forEach(function (row) {
      var tr = panel.querySelector('tr[data-symbol="' + row.symbol + '"]');
      if (!tr) return;
      Object.keys(FMT).forEach(function (field) {
        setText(tr.querySelector('[data-f="' + field + '"]'), fmt(field, row[field]));
      });
      setFlag(tr.querySelector('[data-f="flags"]'), flagFor(row));
      tr.classList.toggle("match", matches.indexOf(row.symbol) !== -1);
      tr.classList.toggle("best", row.symbol === data.best);
    });
  }

  document.querySelectorAll("section.panel[data-ticker]").forEach(function (panel) {
    // The server-built URL carries the effective screening parameters. Falling back to
    // the bare ticker would stream frames computed from the defaults, which would then
    // overwrite a page rendered with different ones.
    var url =
      panel.dataset.stream || "/api/stream/" + encodeURIComponent(panel.dataset.ticker);
    var es = null;

    function connect() {
      // EventSource reconnects on its own, so a dropped stream needs no retry logic
      // here. The server's own link status is what the indicator reflects.
      es = new EventSource(url);
      es.addEventListener("patch", function (ev) {
        var data;
        try {
          data = JSON.parse(ev.data);
        } catch (err) {
          return;
        }
        if (data.status) patchStatus(data.status);
        patchPanel(panel, data);
      });
      es.onerror = function () {
        var dot = document.querySelector("[data-status-dot]");
        var label = document.querySelector("[data-status-label]");
        if (dot) dot.className = "dot bad";
        if (label) label.textContent = "reconnecting";
      };
    }

    connect();

    // Registered before the toggle's early return: an error panel carries no live toggle
    // but still offers Remove.
    var remove = panel.querySelector("[data-remove]");
    if (remove) {
      remove.addEventListener("click", function (ev) {
        // The href stays the no-JS fallback; following it here would rescreen every other
        // ticker from cold and blank the page for minutes.
        ev.preventDefault();
        if (es) {
          es.close();
          es = null;
        }
        var jump = document.querySelector(
          '.result-jump-bar a[href="#' + panel.id + '"]'
        );
        if (jump) jump.remove();
        var chip = document.querySelector(
          '.ticker-bar input[name="ticker"][value="' + remove.dataset.remove + '"]'
        );
        if (chip) chip.checked = false;
        panel.remove();
      });
    }

    var toggle = panel.querySelector("[data-live-toggle]");
    var paused = panel.querySelector("[data-paused]");
    if (!toggle) return;
    toggle.hidden = false;
    toggle.addEventListener("click", function () {
      if (es) {
        // Closing the stream disconnects the request, and the server only polls views
        // with a viewer — so this is what stops billing option quota for this ticker.
        es.close();
        es = null;
        toggle.textContent = "Resume live";
        if (paused) paused.hidden = false;
      } else {
        connect();
        toggle.textContent = "Stop live";
        if (paused) paused.hidden = true;
      }
    });
  });
})();
