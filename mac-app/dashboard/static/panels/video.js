// The debug image. Boxes and the crosshair are drawn here, in the browser, from the
// same event that carried the pixels, so the overlay always belongs to what is shown.
// Colours and sizes follow macvision/display.py: boxes green, cv2.MARKER_CROSS of 12
// ROI pixels, red when the trigger fired, white otherwise.
const BOX_COLOR = "#00ff00";
const HIT_COLOR = "#ff0000";
const IDLE_COLOR = "#ffffff";
const CROSS = 12;          // total size; the arms are ±6 ROI pixels
const STALE_MS = 1500;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// overlay_text() from macvision/stats.py, same wording and spacing. Python's :.1f
// rounds half-to-even where toFixed rounds half-up; they differ only on exact binary
// ties, which measured milliseconds never are.
export function overlayText(t, seq, hit) {
  const up = t.upstream_ms != null ? t.upstream_ms : null;
  const mark = up != null ? "~" : ">";
  const upText = up != null ? up.toFixed(1) : "-.-";
  const net = t.queue_ms != null ? t.queue_ms.toFixed(1) : "-.-";
  const mac = t.mac_ms != null ? t.mac_ms.toFixed(1) : "-.-";
  const e2e = t.e2e_ms != null ? Math.round(t.e2e_ms) : "-";
  const label = t.upstream_label || "win";
  return `e2e${mark}${e2e}ms  ${label}=${upText} net+=${net} mac=${mac}  seq=${seq}  trig=${hit ? "ON" : "off"}`;
}

let wrap, img, canvas, g, placeholder, caption, meta, scaleSel;
let shown = null;          // the frame whose pixels are on screen
let token = 0;             // increments per received frame; a decode that finishes late is ignored
let lastFrameAt = -Infinity;
let arrivals = [];         // frame arrival times, pruned to the last second
let roi = null;
let procState = null;
let scale = "1x";
let timer = null, observer = null;

function applyScale() {
  const w = shown ? shown.w : roi ? roi[0] : 0;
  if (scale === "fit") {
    wrap.style.width = "100%";
    img.style.width = "100%";
  } else {
    wrap.style.width = "";
    img.style.width = w ? `${w * (scale === "2x" ? 2 : 1)}px` : "";
  }
}

function draw() {
  const dpr = window.devicePixelRatio || 1;
  const w = img.clientWidth, h = img.clientHeight;
  const pw = Math.round(w * dpr), ph = Math.round(h * dpr);
  if (canvas.width !== pw || canvas.height !== ph) {
    canvas.width = pw;
    canvas.height = ph;
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
  }
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.clearRect(0, 0, pw, ph);
  if (!shown || !w || !h || !shown.w || !shown.h) return;
  const sx = pw / shown.w, sy = ph / shown.h;   // ROI pixels -> device pixels

  g.lineWidth = 2 * dpr;
  g.strokeStyle = BOX_COLOR;
  for (const b of shown.boxes || []) {
    if (!Array.isArray(b) || b.length < 4) continue;
    g.strokeRect(b[0] * sx, b[1] * sy, (b[2] - b[0]) * sx, (b[3] - b[1]) * sy);
  }

  if (shown.cx == null || shown.cy == null) return;
  const lw = Math.max(1, Math.round(dpr));
  const snap = (v) => Math.round(v) + (lw % 2 ? 0.5 : 0);   // odd widths sit on pixel centres
  const cx = snap(shown.cx * sx), cy = snap(shown.cy * sy);
  const ax = (CROSS / 2) * sx, ay = (CROSS / 2) * sy;
  g.lineWidth = lw;
  g.strokeStyle = shown.hit ? HIT_COLOR : IDLE_COLOR;
  g.beginPath();
  g.moveTo(cx - ax, cy); g.lineTo(cx + ax, cy);
  g.moveTo(cx, cy - ay); g.lineTo(cx, cy + ay);
  g.stroke();
}

function updateMeta() {
  const now = performance.now();
  while (arrivals.length && now - arrivals[0] > 1000) arrivals.shift();
  const size = shown ? `${shown.w}×${shown.h}` : roi ? `${roi[0]}×${roi[1]}` : "?";
  meta.textContent = `${arrivals.length} fps received · ROI ${size}`;
}

function tick() {
  const stale = performance.now() - lastFrameAt > STALE_MS;
  placeholder.hidden = !stale;
  if (stale) {
    placeholder.textContent = procState && procState !== "running"
      ? `no frames — macvision is ${procState}` : "no frames yet";
  }
  updateMeta();
}

function onFrame(data) {
  const now = performance.now();
  lastFrameAt = now;
  arrivals.push(now);
  placeholder.hidden = true;
  updateMeta();
  // A hidden tab shows nothing: skip the decode, keep the counters honest.
  if (document.visibilityState === "hidden") return;
  const mine = ++token;
  img.src = data.image;
  // decode() rejects when src changes under it, which is exactly the "a newer frame
  // arrived first" case: only the frame whose pixels made it to the screen draws.
  const ready = typeof img.decode === "function"
    ? img.decode() : new Promise((resolve) => { img.onload = resolve; });
  ready.then(() => {
    if (mine !== token) return;
    shown = data;
    wrap.classList.remove("empty");
    applyScale();
    draw();
    caption.textContent = overlayText(data.timing || {}, data.seq, data.hit);
  }, () => { /* superseded */ });
}

export default {
  id: "video",
  title: "Video",
  wide: true,
  events: ["frame", "hello", "process"],

  mount(root) {
    shown = null; token = 0; lastFrameAt = -Infinity; arrivals = []; roi = null; procState = null;
    wrap = el("div", "video-wrap empty");
    img = el("img");
    img.alt = "";   // no alt text on the src-less image before the first frame
    canvas = el("canvas");
    g = canvas.getContext("2d");
    placeholder = el("div", "placeholder", "no frames yet");
    wrap.append(img, canvas, placeholder);

    caption = el("div", "caption mono", "");
    const bar = el("div", "toolbar");
    meta = el("span", "muted mono", "");
    scaleSel = el("select");
    for (const s of ["1x", "2x", "fit"]) {
      const o = el("option", null, s);
      o.value = s;
      scaleSel.append(o);
    }
    scaleSel.value = scale;
    scaleSel.addEventListener("change", () => { scale = scaleSel.value; applyScale(); });
    const lab = el("label", null, "scale ");
    lab.append(scaleSel);
    bar.append(meta, lab);
    root.append(wrap, caption, bar);

    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(draw);
      observer.observe(img);
    } else {
      window.addEventListener("resize", draw);
    }
    timer = setInterval(tick, 500);
    applyScale();
    updateMeta();
  },

  update(type, data) {
    if (type === "frame") onFrame(data);
    else if (type === "hello") { roi = Array.isArray(data.roi) ? data.roi : roi; applyScale(); updateMeta(); }
    else if (type === "process") {
      procState = data.state;
      if (data.state !== "running") { lastFrameAt = -Infinity; tick(); }  // show it now, not in 1.5s
    }
  },

  unmount() {
    clearInterval(timer);
    if (observer) observer.disconnect();
    window.removeEventListener("resize", draw);
  },
};
