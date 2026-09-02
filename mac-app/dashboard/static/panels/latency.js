// Per-frame timing: the last 200 samples, client-side medians, a sparkline, and the
// server's own [stats] line. `decide` is the comparable number (docs/PROTOCOL.md).
const MAX_SAMPLES = 200;
const REDRAW_MS = 100;
const CHART_H = 80;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function median(xs) {
  if (!xs.length) return null;
  const s = xs.slice().sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
const fmt = (v) => (v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(1));

let samples = [];          // {decide, e2e, mac}
let tiles = {}, chart, g, statsDl, summaryPre;
let lastPid = null, helloT = null;
let redrawTimer = null, lastRedraw = 0;
let colors = { decide: "#35d07f", e2e: "#5aa9ff" };
let observer = null;

function renderTiles() {
  const pick = (k) => samples.map((s) => s[k]).filter((v) => typeof v === "number");
  const decide = pick("decide"), e2e = pick("e2e"), mac = pick("mac");
  tiles.decideMed.textContent = fmt(median(decide));
  tiles.decideMax.textContent = fmt(decide.length ? Math.max(...decide) : null);
  tiles.e2eMed.textContent = fmt(median(e2e));
  tiles.e2eMax.textContent = fmt(e2e.length ? Math.max(...e2e) : null);
  tiles.macMed.textContent = fmt(median(mac));
  tiles.n.textContent = String(samples.length);
}

function drawChart() {
  const dpr = window.devicePixelRatio || 1;
  const w = chart.clientWidth || 300;
  const pw = Math.round(w * dpr), ph = Math.round(CHART_H * dpr);
  if (chart.width !== pw || chart.height !== ph) { chart.width = pw; chart.height = ph; }
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.clearRect(0, 0, pw, ph);
  if (samples.length < 2) return;
  let max = 0;
  for (const s of samples) {
    if (typeof s.decide === "number" && s.decide > max) max = s.decide;
    if (typeof s.e2e === "number" && s.e2e > max) max = s.e2e;
  }
  max = Math.max(max, 1);
  const pad = 3 * dpr;
  const lw = 1.5 * dpr;
  const step = pw / (MAX_SAMPLES - 1);
  const right = pw - lw / 2;                 // newest sample at the right edge, its stroke not clipped
  const y = (v) => ph - pad - (v / max) * (ph - 2 * pad);
  g.lineWidth = lw;
  g.lineJoin = "round";
  for (const key of ["e2e", "decide"]) {   // decide drawn last: it is the number that matters
    g.strokeStyle = colors[key];
    g.beginPath();
    let pen = false;
    for (let i = 0; i < samples.length; i++) {
      const v = samples[i][key];
      if (typeof v !== "number") { pen = false; continue; }
      const x = right - (samples.length - 1 - i) * step;
      if (pen) g.lineTo(x, y(v)); else g.moveTo(x, y(v));
      pen = true;
    }
    g.stroke();
  }
  g.fillStyle = colors.e2e;
  g.font = `${10 * dpr}px ui-monospace, Menlo, monospace`;
  g.textBaseline = "top";
  g.fillText(`${max.toFixed(1)} ms`, pad, pad);
}

// Frames can arrive at 30+/s; the picture only needs to move ten times a second.
function scheduleRedraw() {
  if (redrawTimer) return;
  const wait = Math.max(0, REDRAW_MS - (performance.now() - lastRedraw));
  redrawTimer = setTimeout(() => {
    redrawTimer = null;
    lastRedraw = performance.now();
    renderTiles();
    drawChart();
  }, wait);
}

function renderStats(data) {
  const st = data.stats || {};
  const count = (v) => (v == null ? "—" : String(v));
  const rows = [
    ["e2e median", fmt(st.e2e_median_ms)], ["e2e max", fmt(st.e2e_max_ms)],
    ["decide median", fmt(st.decide_median_ms)], ["offset (calibrated out)", fmt(st.offset_ms)],
    ["stale dropped", count(data.stale_dropped)], ["dropped writes", count(data.dropped_writes)],
  ];
  statsDl.replaceChildren();
  for (const [k, v] of rows) {
    statsDl.append(el("dt", null, k));
    statsDl.append(el("dd", "mono", v));
  }
  summaryPre.textContent = data.summary || "";
}

function reset() {
  samples = [];               // a new run: old samples would pollute its medians
  statsDl.replaceChildren();
  summaryPre.textContent = "";
  scheduleRedraw();
}

function tile(key, label) {
  const t = el("div", "tile");
  t.append(el("div", "k", label));
  const v = el("div", "v", "—");
  t.append(v);
  tiles[key] = v;
  return t;
}

export default {
  id: "latency",
  title: "Latency",
  events: ["frame", "stats", "hello", "process"],

  mount(root) {
    samples = []; tiles = {}; lastPid = null; helloT = null;
    const css = getComputedStyle(document.documentElement);
    colors = {
      decide: css.getPropertyValue("--decide").trim() || colors.decide,
      e2e: css.getPropertyValue("--e2e").trim() || colors.e2e,
    };
    const grid = el("div", "tiles");
    grid.append(tile("decideMed", "decide med"), tile("decideMax", "decide max"),
      tile("e2eMed", "e2e med"), tile("e2eMax", "e2e max"), tile("macMed", "mac med"), tile("n", "samples"));
    chart = el("canvas", "chart");
    g = chart.getContext("2d");
    const legend = el("div", "legend");
    for (const [key, name] of [["decide", "decide_ms"], ["e2e", "e2e_ms"]]) {
      const item = el("span", "mono");
      const swatch = el("i");
      swatch.style.background = colors[key];
      item.append(swatch, name);
      legend.append(item);
    }
    const explain = el("p", "explain",
      "decide = packet received → trigger byte written. It ignores the display and the source, " +
      "so it is the figure to compare between runs; e2e~ (Windows agent) and e2e> (camera) are not comparable.");
    statsDl = el("dl");
    summaryPre = el("pre", "mono", "");
    const head = el("h3", "muted", "last [stats]");
    head.style.cssText = "margin:8px 0 4px;font-size:12px;text-transform:uppercase";
    root.append(grid, chart, legend, explain, head, statsDl, summaryPre);
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => drawChart());
      observer.observe(chart);
    }
    renderTiles();
  },

  update(type, data) {
    if (type === "frame") {
      const t = data.timing || {};
      samples.push({ decide: t.decide_ms, e2e: t.e2e_ms, mac: t.mac_ms });
      if (samples.length > MAX_SAMPLES) samples.splice(0, samples.length - MAX_SAMPLES);
      scheduleRedraw();
    } else if (type === "stats") {
      // The server replays the previous run's last stats on connect: older than this
      // run's hello is not this run.
      if (typeof data.t === "number" && typeof helloT === "number" && data.t < helloT) return;
      renderStats(data);
    } else if (type === "hello") {
      // A new pid is a new run, whoever started it. The runner's `process` usually says
      // so first (below); the hello covers a macvision the dashboard did not launch.
      if (typeof data.t === "number") helloT = data.t;
      if (data.pid != null && data.pid !== lastPid) { lastPid = data.pid; reset(); }
    } else if (type === "process") {
      if (data.state === "running" && data.pid != null && data.pid !== lastPid) { lastPid = data.pid; reset(); }
    }
  },

  unmount() {
    clearTimeout(redrawTimer);
    redrawTimer = null;
    if (observer) observer.disconnect();
  },
};
