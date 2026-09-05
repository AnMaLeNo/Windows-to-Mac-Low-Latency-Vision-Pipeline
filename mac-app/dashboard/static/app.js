// Layout and the registry. Panels are files under ./panels/; this file knows only their
// shape: {id, title, events, mount(el, ctx), update(type, data), unmount()} plus an
// optional `wide` (the video panel spans two columns).
import panels from "./panels/index.js";
import { createBus } from "./bus.js";
import * as api from "./api.js";

const VISIBILITY_KEY = "macvision.panels";

// localStorage is a convenience: private mode or a locked-down browser may throw on
// every access, and the page has to work without it.
function loadVisibility() {
  try {
    const v = JSON.parse(localStorage.getItem(VISIBILITY_KEY));
    return v && typeof v === "object" ? v : {};
  } catch { return {}; }
}
function saveVisibility(v) {
  try { localStorage.setItem(VISIBILITY_KEY, JSON.stringify(v)); } catch { /* unavailable */ }
}

function main() {
  const bus = createBus("/events");
  const ctx = { api, bus };
  const grid = document.getElementById("panels");
  const menu = document.getElementById("panels-list");
  const visible = loadVisibility();

  for (const panel of panels) {
    const card = document.createElement("section");
    card.className = "panel" + (panel.wide ? " wide" : "");
    card.id = "panel-" + panel.id;
    const h2 = document.createElement("h2");
    h2.textContent = panel.title;
    const body = document.createElement("div");
    body.className = "body";
    card.append(h2, body);
    card.hidden = visible[panel.id] === false;
    grid.append(card);

    // One broken panel must not take the others down: every entry point is fenced.
    try {
      panel.mount(body, ctx);
    } catch (e) {
      console.error(`[${panel.id}] mount failed`, e);
      body.textContent = `panel failed to mount: ${e.message}`;
    }
    for (const type of panel.events || []) {
      bus.on(type, (data) => {
        try { panel.update(type, data); } catch (e) { console.error(`[${panel.id}] update(${type})`, e); }
      });
    }

    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !card.hidden;
    cb.addEventListener("change", () => {
      card.hidden = !cb.checked;   // hidden panels keep receiving events; that is cheap
      visible[panel.id] = cb.checked;
      saveVisibility(visible);
    });
    label.append(cb, " " + panel.title);
    menu.append(label);
  }

  const sse = document.getElementById("sse");
  const proc = document.getElementById("proc");
  const tele = document.getElementById("tele");
  bus.onState((s) => { sse.dataset.state = s; sse.textContent = `sse: ${s}`; });
  bus.on("process", (p) => {
    proc.dataset.state = p.state;
    let text = `process: ${p.state}`;
    if (p.pid != null && p.state !== "idle") text += ` pid ${p.pid}`;
    if (p.state === "exited" && p.exit_code != null) text += ` (exit ${p.exit_code})`;
    proc.textContent = text;
  });
  bus.on("telemetry", (t) => {
    tele.dataset.state = t.connected ? "on" : "off";
    tele.textContent = `telemetry: ${t.connected ? "connected" : "disconnected"}`;
  });

  window.addEventListener("pagehide", () => {
    bus.close();
    for (const p of panels) { try { p.unmount(); } catch { /* leaving anyway */ } }
  });
}

main();
