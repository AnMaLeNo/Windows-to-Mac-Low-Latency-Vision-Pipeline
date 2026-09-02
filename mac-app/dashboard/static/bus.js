// The SSE client. One EventSource, one listener per event name, JSON-decoded once and
// fanned out to whoever subscribed. EventSource reconnects on its own; we only report.
// emit() puts page-local events through the same fan-out.
export const EVENTS = ["hello", "frame", "stats", "process", "telemetry", "log", "heartbeat"];

export function createBus(url = "/events") {
  const subs = new Map();
  const stateSubs = new Set();
  let state = "connecting";
  const es = new EventSource(url);

  const setState = (s) => {
    state = s;
    for (const fn of stateSubs) {
      try { fn(s); } catch (e) { console.error("[bus] state handler", e); }
    }
  };
  es.addEventListener("open", () => setState("open"));
  es.addEventListener("error", () => setState("error"));

  // One handler's throw is logged; the next handler still runs.
  const dispatch = (type, set, data) => {
    for (const fn of set) {
      try { fn(data); } catch (e) { console.error(`[bus] ${type} handler`, e); }
    }
  };

  // Listeners are registered lazily so an event name unknown to this file still works;
  // EVENTS just pre-registers the contract's names.
  const listen = (type) => {
    if (subs.has(type)) return subs.get(type);
    const set = new Set();
    subs.set(type, set);
    es.addEventListener(type, (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (e) { console.warn(`[bus] unparseable ${type}`, e); return; }
      dispatch(type, set, data);
    });
    return set;
  };
  for (const type of EVENTS) listen(type);

  return {
    on(type, fn) {
      const set = listen(type);
      set.add(fn);
      return () => set.delete(fn);
    },
    // Page-local events: one panel telling the others something the server does not
    // know (a probe is running, say). Same listeners as on(), the EventSource is not
    // involved; use a name the server never sends, i.e. not one of EVENTS.
    emit(type, data) { dispatch(type, listen(type), data); },
    onState(fn) {
      stateSubs.add(fn);
      fn(state);  // so a late subscriber does not wait for the next transition
      return () => stateSubs.delete(fn);
    },
    get state() { return state; },
    close() { es.close(); },
  };
}
