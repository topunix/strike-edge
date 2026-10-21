/* Charts for /report. Vanilla JS plus Chart.js 4.4.1 (pinned CDN). */
(function () {
  "use strict";
  var COLORS = { strike: "#2a5bd7", hyperliquid: "#d9730d", okx: "#12907f" };
  var NAMES = { strike: "Strike", hyperliquid: "Hyperliquid", okx: "OKX" };
  var charts = {};
  var sel = document.getElementById("market");
  var msg = document.getElementById("chart-msg");

  function fail(text) {
    msg.textContent = text;
    msg.hidden = false;
  }

  function dateTick(v) {
    return new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
  }

  function base(yTitle, yFmt) {
    var narrow = window.innerWidth < 640;
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      interaction: { mode: "index", intersect: false },
      elements: { point: { radius: 0, hitRadius: 8 }, line: { borderWidth: 2 } },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 14, boxHeight: 3, font: { size: narrow ? 11 : 12 } } },
        tooltip: { callbacks: { title: function (it) { return it.length ? dateTick(it[0].parsed.x) + " " + new Date(it[0].parsed.x).toISOString().slice(11, 16) + " UTC" : ""; } } }
      },
      scales: {
        x: { type: "linear", title: { display: true, text: "Date (UTC)" }, ticks: { callback: dateTick, maxTicksLimit: narrow ? 4 : 8, maxRotation: 0 } },
        y: { title: { display: true, text: yTitle }, ticks: { callback: yFmt, maxTicksLimit: narrow ? 6 : 8 } }
      }
    };
  }

  function draw(id, config) {
    var canvas = document.getElementById(id);
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(canvas, config);
    canvas.dataset.rendered = "1";
  }

  function render(data) {
    var fund = [];
    Object.keys(data.venues_history).forEach(function (v) {
      var label = NAMES[v] + (v === "okx" ? " (8h rate spread evenly per hour)" : "");
      fund.push({
        label: label,
        borderColor: COLORS[v],
        backgroundColor: COLORS[v],
        spanGaps: false,
        data: data.venues_history[v].map(function (p) {
          return { x: p.ts_ms, y: p.funding_rate_per_hour === null ? null : p.funding_rate_per_hour * 100 };
        })
      });
    });
    var cfg1 = { type: "line", data: { datasets: fund }, options: base("Funding, % per hour", function (v) { return v.toFixed(4) + "%"; }) };
    draw("chart-funding", cfg1);

    var cum = [];
    var minX = Infinity, maxX = -Infinity;
    Object.keys(data.cumulative_net_usd_on_10k).forEach(function (other) {
      var dirs = data.cumulative_net_usd_on_10k[other];
      Object.keys(dirs).forEach(function (d) {
        var long_strike = d === "long_strike_short_other";
        var label = long_strike ? "Long Strike / short " + NAMES[other] : "Long " + NAMES[other] + " / short Strike";
        var pts = dirs[d].map(function (p) { return { x: p.ts_ms, y: p.cumulative_net_usd }; });
        if (pts.length) { minX = Math.min(minX, pts[0].x); maxX = Math.max(maxX, pts[pts.length - 1].x); }
        cum.push({
          label: label,
          borderColor: COLORS[other],
          backgroundColor: COLORS[other],
          borderDash: long_strike ? [] : [6, 3],
          data: pts
        });
      });
    });
    if (cum.length) {
      cum.push({ label: "Zero (costs fully covered)", borderColor: "#7a8494", borderDash: [2, 2], borderWidth: 1, data: [{ x: minX, y: 0 }, { x: maxX, y: 0 }] });
    }
    var cfg2 = { type: "line", data: { datasets: cum }, options: base("Estimated net, USD", function (v) { return (v < 0 ? "-" : "") + "$" + Math.abs(v).toFixed(0); }) };
    draw("chart-cum", cfg2);
    msg.hidden = true;
  }

  function load(symbol) {
    fetch("/api/v1/history?symbol=" + encodeURIComponent(symbol) + "&days=30")
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(render)
      .catch(function (e) { fail("Could not load chart data (" + e.message + ")."); });
  }

  function start() {
    if (typeof Chart === "undefined") {
      fail("Charts could not load (Chart.js unavailable). The tables below have the same numbers.");
      return;
    }
    sel.addEventListener("change", function () { load(sel.value); });
    load(sel.value);
  }

  if (document.readyState === "complete") start();
  else window.addEventListener("load", start);
})();
