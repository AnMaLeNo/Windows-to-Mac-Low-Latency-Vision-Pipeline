// The oneshot flags (--list-ports, --list-cameras): one button each, output verbatim.
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

let api, bus, buttons = [], out, message;
let running = false, probing = false;

function syncButtons() {
  for (const b of buttons) b.disabled = running || probing;
}

async function probe(arg, button) {
  probing = true;
  syncButtons();
  bus.emit("probe", { running: true });   // the launcher holds Start while a probe owns the cameras
  const label = button.textContent;
  button.textContent = `${label} …`;
  message.textContent = "";
  try {
    const r = await api.postJSON("/api/oneshot", { flag: arg.flag });
    out.replaceChildren();
    out.append(el("h3", "mono", `${arg.flag} → exit ${r.exit_code}`));
    out.append(el("pre", "mono", r.stdout || "(no stdout)"));
    if (r.stderr) out.append(el("pre", "mono stderr", r.stderr));
  } catch (e) {
    message.textContent = `${arg.flag}: ${e.message}`;
  } finally {
    button.textContent = label;
    probing = false;
    syncButtons();
    bus.emit("probe", { running: false });
  }
}

export default {
  id: "probes",
  title: "Probes",
  events: ["process"],

  mount(root, ctx) {
    api = ctx.api;
    bus = ctx.bus;
    buttons = []; probing = false; running = false;
    const bar = el("div", "toolbar");
    message = el("div", "error", "");
    out = el("div", "probe-out");
    root.append(
      bar,
      el("div", "warn", "Probing cameras opens every index in turn: it conflicts with a running camera source, so the probes are disabled while macvision runs."),
      message, out);

    api.getJSON("/api/args").then((spec) => {
      const oneshots = [];
      for (const group of spec.groups || []) {
        for (const arg of group.args || []) if (arg.oneshot && arg.flag) oneshots.push(arg);
      }
      if (!oneshots.length) { bar.append(el("span", "muted", "no oneshot flags in this parser")); return; }
      for (const arg of oneshots) {
        const b = el("button", "mono", arg.flag);
        if (arg.help) b.title = arg.help;
        b.addEventListener("click", () => probe(arg, b));
        buttons.push(b);
        bar.append(b);
      }
      syncButtons();
    }, (e) => {
      message.textContent = `could not load /api/args: ${e.message}`;
    });
  },

  update(type, data) {
    if (type !== "process") return;
    running = data.state === "running";
    syncButtons();
  },

  unmount() {},
};
