// The launch form, generated from `macvision --describe-args` (contract 2) so a flag
// added to the parser shows up here with no dashboard change. Values are {dest: value};
// what is untouched is omitted, so the parser's default applies.
const STORAGE_KEY = "macvision.launch";
const PREVIEW_DEBOUNCE_MS = 250;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function loadValues() {
  try {
    const v = JSON.parse(localStorage.getItem(STORAGE_KEY));
    return v && typeof v === "object" && !Array.isArray(v) ? v : {};
  } catch { return {}; }
}
function saveValues(values) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(values)); } catch { /* unavailable */ }
}

function elapsed(sinceSeconds) {
  if (typeof sinceSeconds !== "number") return "";
  const sinceMs = sinceSeconds > 1e12 ? sinceSeconds : sinceSeconds * 1000;  // tolerate ms
  const s = Math.max(0, Math.floor((Date.now() - sinceMs) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

let api, root, form, preview, previewErr, message, stateLine, commandLine;
let startBtn, stopBtn, resetBtn;
let spec = null, values = {}, proc = null;
let previewTimer = null, previewSeq = 0, ticker = null, busy = false;
let probing = false, offProbe = null;

function setValue(dest, v) {
  if (v === "" || v == null || v === false) delete values[dest];
  else values[dest] = v;
  saveValues(values);
  schedulePreview();
}

function field(arg) {
  const wrap = el("div", "field");
  const id = `arg-${arg.dest}`;
  const name = arg.flag || arg.dest;
  const def = arg.default == null ? "" : String(arg.default);
  const current = values[arg.dest];
  let input;
  switch (arg.kind) {
    case "bool":
      input = el("input");
      input.type = "checkbox";
      input.checked = current === true;
      input.addEventListener("change", () => setValue(arg.dest, input.checked));
      break;
    case "choice": {
      input = el("select");
      const o = el("option", null, def ? `default (${def})` : "default");
      o.value = "";
      input.append(o);
      for (const c of arg.choices || []) {
        const oc = el("option", null, String(c));
        oc.value = String(c);
        input.append(oc);
      }
      input.value = current != null ? String(current) : "";
      input.addEventListener("change", () => setValue(arg.dest, input.value));
      break;
    }
    case "int":
    case "float":
      input = el("input");
      input.type = "number";
      input.step = arg.kind === "float" ? "any" : "1";
      input.placeholder = def;
      input.value = current != null ? String(current) : "";
      // Sent as a number; a non-integer in an int field is the server's 400 to report.
      input.addEventListener("input", () => setValue(arg.dest, input.value === "" ? "" : Number(input.value)));
      break;
    default:
      input = el("input");
      input.type = "text";
      input.placeholder = def;
      input.value = current != null ? String(current) : "";
      input.addEventListener("input", () => setValue(arg.dest, input.value));
  }
  input.id = id;
  if (arg.help) input.title = arg.help;
  const help = el("small", "help", arg.help || "");
  if (arg.help) help.title = arg.help;
  if (arg.kind === "bool") {
    const row = el("label", "check");
    row.append(input, name);
    wrap.append(row, help);
  } else {
    const label = el("label", null, name);
    label.htmlFor = id;
    if (arg.help) label.title = arg.help;
    wrap.append(label, input, help);
  }
  return wrap;
}

// Stored values outlive the parser they came from: a renamed flag, or a choice that is
// gone, would make every preview and Start a 400 with no field left to clear it from.
function pruneValues() {
  const known = new Map();
  for (const group of spec.groups || []) {
    for (const arg of group.args || []) if (arg.dest && !arg.oneshot) known.set(arg.dest, arg);
  }
  let changed = false;
  for (const dest of Object.keys(values)) {
    const arg = known.get(dest);
    const ok = !!arg && (arg.kind !== "choice" || (arg.choices || []).some((c) => String(c) === String(values[dest])));
    if (!ok) { delete values[dest]; changed = true; }
  }
  if (changed) saveValues(values);
}

function renderForm() {
  form.replaceChildren();
  if (!spec) return;
  for (const group of spec.groups || []) {
    const args = (group.args || []).filter((a) => !a.oneshot);   // probes live in their own panel
    if (!args.length) continue;
    const fs = el("fieldset");
    fs.append(el("legend", null, group.title || "options"));
    for (const arg of args) fs.append(field(arg));
    form.append(fs);
  }
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, PREVIEW_DEBOUNCE_MS);
}

async function runPreview() {
  const mine = ++previewSeq;
  try {
    const r = await api.postJSON("/api/preview", { values });
    if (mine !== previewSeq) return;    // a newer preview is in flight
    preview.textContent = r.command || (Array.isArray(r.argv) ? r.argv.join(" ") : "");
    previewErr.textContent = "";
  } catch (e) {
    if (mine !== previewSeq) return;
    previewErr.textContent = `preview: ${e.message}`;
  }
}

function syncButtons() {
  const running = !!proc && proc.state === "running";
  startBtn.disabled = busy || running || probing;   // a probe may hold the cameras
  stopBtn.disabled = busy || !running;
}

function renderState() {
  if (!proc) { stateLine.textContent = "state: ?"; commandLine.hidden = true; return; }
  let text = `state: ${proc.state}`;
  if (proc.pid != null && proc.state !== "idle") text += ` · pid ${proc.pid}`;
  if (proc.exit_code != null) text += ` · exit ${proc.exit_code}`;
  const e = elapsed(proc.since);
  if (e) text += proc.state === "running" ? ` · up ${e}` : ` · ${e} ago`;
  stateLine.textContent = text;
  commandLine.hidden = !proc.command;
  commandLine.textContent = proc.command || "";
}

async function post(path, body, verb) {
  busy = true;
  syncButtons();
  message.textContent = "";
  try {
    await api.postJSON(path, body);
  } catch (e) {
    message.textContent = `${verb}: ${e.message}`;
  } finally {
    busy = false;
    syncButtons();
  }
}

export default {
  id: "launcher",
  title: "Launcher",
  events: ["process"],

  mount(el_, ctx) {
    api = ctx.api;
    root = el_;
    values = loadValues();
    spec = null; proc = null; busy = false; probing = false;

    const bar = el("div", "toolbar");
    startBtn = el("button", "primary", "Start");
    stopBtn = el("button", "danger", "Stop");
    resetBtn = el("button", null, "Reset form");
    bar.append(startBtn, stopBtn, resetBtn);
    stateLine = el("div", "state mono", "state: ?");
    commandLine = el("div", "state mono muted", "");
    commandLine.hidden = true;
    message = el("div", "error", "");
    preview = el("pre", "mono", "");
    previewErr = el("div", "error", "");
    form = el("div");
    root.append(bar, stateLine, commandLine, message, el("div", "muted", "command preview"), preview, previewErr, form);

    startBtn.addEventListener("click", () => post("/api/start", { values }, "start"));
    stopBtn.addEventListener("click", () => post("/api/stop", {}, "stop"));
    resetBtn.addEventListener("click", () => {
      values = {};
      saveValues(values);
      renderForm();
      schedulePreview();
    });
    syncButtons();
    ticker = setInterval(renderState, 1000);
    // probes.js announces its runs on the bus: --list-cameras opens every camera, the
    // conflict that panel warns about, so Start waits for it.
    offProbe = ctx.bus.on("probe", (p) => { probing = !!(p && p.running); syncButtons(); });

    api.getJSON("/api/args").then((s) => {
      spec = s;
      pruneValues();
      renderForm();
      schedulePreview();
    }, (e) => {
      message.textContent = `could not load /api/args: ${e.message}`;
    });
  },

  update(type, data) {
    if (type !== "process") return;
    proc = data;
    renderState();
    syncButtons();
  },

  unmount() {
    clearInterval(ticker);
    clearTimeout(previewTimer);
    if (offProbe) offProbe();
  },
};
