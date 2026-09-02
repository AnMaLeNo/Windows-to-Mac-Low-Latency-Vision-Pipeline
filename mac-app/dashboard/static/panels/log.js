// The child's stdout and stderr, as the runner tails them. Bounded, filterable.
const MAX_LINES = 2000;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function stamp(t) {
  if (typeof t !== "number") return "";
  const d = new Date(t > 1e12 ? t : t * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)} `;
}

let box, filterInput, autoCb;
let filter = "", lastPid = null, scrollPending = false;
let opened = false, offState = null;

function matches(line) {
  // Separators are context, not output: they stay visible under any filter.
  return !filter || line.classList.contains("sep") || line.textContent.toLowerCase().includes(filter);
}

function addLine(text, cls) {
  const line = el("div", cls ? `line ${cls}` : "line", text);
  line.hidden = !matches(line);
  box.append(line);
  while (box.childElementCount > MAX_LINES) box.firstElementChild.remove();
  if (autoCb.checked && !scrollPending) {
    scrollPending = true;   // one scroll per paint, however many lines arrive in a burst
    requestAnimationFrame(() => { scrollPending = false; box.scrollTop = box.scrollHeight; });
  }
}

export default {
  id: "log",
  title: "Log",
  events: ["log", "process"],

  mount(root, ctx) {
    filter = ""; lastPid = null; scrollPending = false; opened = false;
    const bar = el("div", "toolbar");
    filterInput = el("input");
    filterInput.type = "search";
    filterInput.placeholder = "filter";
    filterInput.addEventListener("input", () => {
      filter = filterInput.value.trim().toLowerCase();
      for (const line of box.children) line.hidden = !matches(line);
    });
    autoCb = el("input");
    autoCb.type = "checkbox";
    autoCb.checked = true;
    const autoLabel = el("label", null, "");
    autoLabel.append(autoCb, "autoscroll");
    const clear = el("button", null, "Clear");
    clear.addEventListener("click", () => box.replaceChildren());
    bar.append(filterInput, autoLabel, clear);
    box = el("div", "log");
    root.append(bar, box);
    // The server replays its log ring to every new subscription, and EventSource
    // reconnects on its own (dashboard restart, sleep/wake, a blip): every reopen after
    // the first starts the list over and lets the replay refill it. The replayed
    // `process` lands before the ring, so the pid separator comes back with it.
    offState = ctx.bus.onState((s) => {
      if (s !== "open") return;
      if (opened) { box.replaceChildren(); lastPid = null; }
      opened = true;
    });
  },

  update(type, data) {
    if (type === "log") {
      addLine(stamp(data.t) + (data.line ?? ""), data.stream === "stderr" ? "stderr" : "");
    } else if (type === "process") {
      if (data.state === "running" && data.pid != null && data.pid !== lastPid) {
        addLine(`── pid ${data.pid} started: ${data.command || (data.argv || []).join(" ")} ──`, "sep");
        lastPid = data.pid;
      } else if (data.state === "exited" && data.pid != null && data.pid === lastPid) {
        addLine(`── pid ${data.pid} exited (code ${data.exit_code ?? "?"}) ──`, "sep");
        lastPid = null;
      }
    }
  },

  unmount() { if (offState) offState(); },
};
