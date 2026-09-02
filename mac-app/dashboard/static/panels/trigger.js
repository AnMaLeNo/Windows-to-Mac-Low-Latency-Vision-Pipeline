// The trigger state, large: the same `hit` the loop wrote to the wire, per frame.
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function since(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

let indicator, sinceLine, targetLine, droppedLine, note;
let hit = null, changedAt = 0, timer = null, lastPid = null;

function setHit(v) {
  if (v === hit) return;
  hit = v;
  changedAt = performance.now();
  indicator.textContent = v == null ? "—" : v ? "ON" : "OFF";
  indicator.classList.toggle("on", v === true);
  renderSince();
}

function renderSince() {
  sinceLine.textContent = hit == null ? "no frame yet" : `${hit ? "on" : "off"} for ${since(performance.now() - changedAt)}`;
}

export default {
  id: "trigger",
  title: "Trigger",
  events: ["frame", "hello", "stats", "telemetry"],

  mount(root) {
    hit = null; changedAt = performance.now(); lastPid = null;
    indicator = el("div", "trigger-indicator mono", "—");
    sinceLine = el("div", "state mono muted", "no frame yet");
    targetLine = el("div", "state", "target: ?");
    droppedLine = el("div", "state mono", "dropped writes: —");
    note = el("div", "warn", "");
    note.hidden = true;
    root.append(indicator, sinceLine, targetLine, droppedLine, note);
    timer = setInterval(renderSince, 1000);
  },

  update(type, data) {
    if (type === "frame") {
      setHit(!!data.hit);
      note.hidden = true;   // whatever the runner thinks, something is driving it
    } else if (type === "hello") {
      // A new pid is a new run: its first frame sets the state, the old run's does not.
      // The server replays the last hello on connect; the same pid changes nothing.
      if (data.pid != null && data.pid !== lastPid) { lastPid = data.pid; setHit(null); }
      const t = data.status && data.status.trigger;
      if (t) {
        const desc = t.description != null ? String(t.description) : "?";
        targetLine.textContent = `target: ${desc}${t.kind ? ` (${t.kind})` : ""}`;
        if (typeof t.dropped_writes === "number") droppedLine.textContent = `dropped writes: ${t.dropped_writes}`;
      } else {
        targetLine.textContent = "target: ?";
      }
    } else if (type === "stats") {
      if (data.dropped_writes != null) droppedLine.textContent = `dropped writes: ${data.dropped_writes}`;
    } else if (type === "telemetry") {
      // Keyed on the subscriber, not the runner: frames can come from a macvision the
      // dashboard did not start, and the runner's `exited` can share a batch with the
      // run's last frame.
      const connected = !!data.connected;
      note.hidden = connected;
      note.textContent = connected ? "" : "telemetry disconnected: nothing is driving the trigger";
      if (!connected) setHit(null);
    }
  },

  unmount() { clearInterval(timer); },
};
