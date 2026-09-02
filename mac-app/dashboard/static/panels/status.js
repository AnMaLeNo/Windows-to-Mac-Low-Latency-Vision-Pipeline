// Every status dict the blocks publish, rendered as a key/value tree. Nothing here
// knows a key by name: a block may add fields freely (docs/DASHBOARD.md, contract 1).
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function scalar(v) {
  if (v == null) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Math.round(v * 1000) / 1000);
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

function render(v) {
  if (Array.isArray(v)) {
    const parts = v.map((x) => (x !== null && typeof x === "object" ? JSON.stringify(x) : scalar(x)));
    return el("span", "mono", `[${parts.join(", ")}]`);
  }
  if (v !== null && typeof v === "object") {
    const keys = Object.keys(v);
    if (!keys.length) return el("span", "mono muted", "{}");
    const dl = el("dl");
    for (const k of keys) {
      dl.append(el("dt", null, k));
      const dd = el("dd");
      dd.append(render(v[k]));
      dl.append(dd);
    }
    return dl;
  }
  return el("span", "mono", scalar(v));
}

let blocks, rest, api;
const sections = new Map();

function setSection(name, value, container) {
  let d = sections.get(name);
  if (!d) {
    d = el("details", "section");
    d.open = true;
    d.append(el("summary", null, name), el("div"));
    sections.set(name, d);
    container.append(d);
  }
  d.lastChild.replaceChildren(render(value));
}

function apply(type, data) {
  if (data == null) return;
  if (type === "hello") {
    const { status, ...head } = data;
    if (status && typeof status === "object") {
      for (const [name, value] of Object.entries(status)) setSection(name, value, blocks);
    }
    setSection("hello", head, rest);
  } else {
    setSection(type, data, rest);
  }
}

export default {
  id: "status",
  title: "Status",
  events: ["hello", "telemetry", "process", "stats"],

  mount(root, ctx) {
    api = ctx.api;
    sections.clear();
    blocks = el("div");
    rest = el("div");
    root.append(blocks, rest);
    // The SSE stream replays these on connect too; the GET covers a stream that is slow
    // to open and costs one request.
    api.getJSON("/api/status").then((s) => {
      if (!s || typeof s !== "object") return;
      apply("hello", s.hello);
      apply("telemetry", s.telemetry);
      apply("process", s.process);
    }, (e) => {
      root.prepend(el("div", "error", `could not load /api/status: ${e.message}`));
    });
  },

  update(type, data) { apply(type, data); },

  unmount() {},
};
