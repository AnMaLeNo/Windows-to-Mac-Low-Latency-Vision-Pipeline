# The dashboard

A web page, on the Mac, for the Mac. It starts and stops `macvision`, shows the debug
image, the latency numbers and the trigger state as they happen, and generates its
launch form from `macvision`'s own argument parser. It is a debug and comfort tool: the
pipeline is complete without it, and nothing the page does can slow the pipeline down.

Those are the two constraints the whole design serves, and they are worth stating as
rules because every later change will be tempted to break one of them:

1. **`macvision` never knows the dashboard exists.** It gains one optional output - a
   telemetry socket, off by default - and one introspection flag. No HTTP server, no
   command channel, no dashboard import anywhere in `macvision/`. Everything the page
   can do, the command line can do, because the page *is* the command line: it builds
   an argv and runs it.
2. **Nothing runs before the trigger byte.** The one line `macvision` executes for the
   dashboard sits after `action.update()` and after `mac_ms` is sampled, and it is a
   memcpy plus an event set. Drawing boxes, encoding JPEGs, serving browsers - all of
   that happens in another process, on the far side of a socket that cannot block.

```
 python3 -m macvision --telemetry tcp://127.0.0.1:50510          (or $MACVISION_TELEMETRY)
   frame loop ── trigger byte ── mac_ms sampled ── telemetry.frame()      <- 1 copy, 1 Event.set
                                                        │  publisher thread
                                                        │  (MVT1 stream, newest wins)
                                                        ▼
 python3 -m dashboard                                  subscriber thread
   runner    ── spawns/stops macvision as a subprocess, tails its stdout/stderr
   frames    ── newest raw frame -> JPEG (or PNG), at most --fps times a second
   server    ── static files, a small JSON API, one SSE stream to the browser
                                                        │
                                                        ▼
 browser: panels (video, latency, trigger, launcher, status, probes, log), each a file
```

Three contracts cross the process boundaries, and only these three. The page never
learns what `macvision` is; the server never learns what the page draws.

## Contract 1: the telemetry stream (`MVT1`)

`macvision` listens on a TCP socket (default `127.0.0.1:50510`) when `--telemetry` or
`$MACVISION_TELEMETRY` names one. Values follow the project idiom:

| value | meaning |
|---|---|
| unset, `""`, `none` | off. Not one instruction runs for it. The default. |
| `tcp://[host][:port]` | listen there. Host defaults to `127.0.0.1`, port to `50510`. IPv4 only. |

The publisher is `macvision/telemetry.py` (`TelemetryPublisher`). Stdlib only, listed
among the PURE modules in `tests/test_imports.py`. If the port is busy at startup
`macvision` prints a warning and runs without telemetry - like the debug window, a
failure here is not fatal.

**What the frame loop pays.** `telemetry.frame(...)` checks whether any client is
connected; with none it returns immediately - no copy, no allocation. With one, it
copies the pixels once (`frame.tobytes()`, ~270KB for a 300x300 ROI, tens of
microseconds), builds a small dict, stores both in a single "latest" slot and sets an
`Event`. Nothing else. The publisher thread does the JSON encoding and the `sendall`;
if a subscriber is slow the slot is simply overwritten with a newer frame - **newest
wins, exactly the rule both sources apply on the way in**. A blocked or dead subscriber
therefore costs the loop nothing beyond that one copy. `stats` and `hello` messages are
queued rather than coalesced, because losing one of those is losing information rather
than staleness.

**Where in the loop.** After `action.update()`, after `decide_ms`, after the display's
`annotate()` and the `mac_ms` sample, after `stats.observe()`. Before the `[stats]` print
and before `display.present()`. `tests/test_loop_order.py` asserts the order and that
neither `decide_ms` nor `mac_ms` includes the publish.

**Framing.** A byte stream of messages, each:

| offset | size | field |
|---|---|---|
| 0 | 4 | magic, ASCII `MVT1` |
| 4 | 4 | `H`, header length, uint32 little-endian |
| 8 | 4 | `P`, payload length, uint32 little-endian |
| 12 | H | header: one JSON object, UTF-8 |
| 12+H | P | payload: raw bytes, or empty |

A subscriber that does not find the magic where it expects it has lost sync and must
close and reconnect; there is no resync scan. Every header carries `"v": 1` and
`"type"`, plus `"t"`, the sender's `time.time()`.

`type: "hello"` - once per connection, first. Payload empty.

```json
{"type": "hello", "v": 1, "t": 1725270000.0, "pid": 4242, "roi": [300, 300],
 "argv": ["--source", "camera://0", "--no-display"],
 "status": {"source": {...}, "detector": {...}, "trigger": {...}, "display": null}}
```

`status` holds each block's own `status()` dict, verbatim, keyed by block name. The
dashboard renders it as key/value pairs and asserts nothing about the keys, so a block
may add fields freely.

`type: "frame"` - one per processed frame while a subscriber is connected. Payload: the
ROI's pixels, row-major, `w*h*c` bytes. `w` and `h` are the pixels' own dimensions (a
udp sender's header may claim otherwise; the payload is the truth), `fmt` is the channel
order (`gray8`, `bgr8`, `bgra8`) and `dtype` the element type, `uint8` for every source
that exists today.

```json
{"type": "frame", "v": 1, "t": 1725270000.1, "seq": 812, "n": 800,
 "w": 300, "h": 300, "c": 3, "fmt": "bgr8", "dtype": "uint8",
 "hit": true, "cx": 150, "cy": 150,
 "boxes": [[12.5, 40.0, 210.0, 190.0]],
 "timing": {"e2e_ms": 31.2, "decide_ms": 6.9, "mac_ms": 9.1,
            "upstream_ms": 4.0, "queue_ms": 0.3,
            "upstream_label": "win", "e2e_mark": "~"}}
```

`seq` is the source's sequence number and `n` the loop's processed-frame count (the
`[stats]` cadence). `boxes` are `[x1, y1, x2, y2]` in ROI pixels, the same list the
rule tested - no class, no score, by design: that would cost a second device sync per
frame on the torch path. `timing` uses `docs/PROTOCOL.md`'s vocabulary: `upstream_ms`
and `queue_ms` are `null` when the source cannot measure them, and `e2e_mark` is `~` or
`>` for the reason the overlay text explains.

`type: "stats"` - at the same cadence as the printed `[stats]` line. Payload empty.

```json
{"type": "stats", "v": 1, "t": 1725270010.0, "n": 900,
 "stats": {"n": 200, "window": 200, "e2e_median_ms": 30.1, "e2e_max_ms": 48.0,
           "decide_median_ms": 6.8, "offset_ms": 238.4},
 "stale_dropped": 3, "dropped_writes": 0,
 "summary": "the printed line, verbatim"}
```

Unknown `type` values must be ignored by subscribers. New fields may be added to any
message; `v` changes only when a field's meaning changes.

`tools/telemetry_tap.py` is the reference subscriber: it prints the message rate and
the per-frame timing fields, with no browser anywhere. It is also how the "costs
nothing" claim gets checked - run `macvision` with and without `--telemetry` (with a
tap connected) and compare `decide med` in the `[stats]` line.

## Contract 2: the argument description

```
python3 -m macvision --describe-args
```

prints the parser as JSON and exits 0, with nothing installed. The dashboard runs it once
at startup and builds the launch form from it; a flag added to `parse_args` appears in
the form with no dashboard change.

```json
{"v": 1, "prog": "macvision", "description": "...",
 "groups": [
   {"title": "trigger", "args": [
     {"dest": "trigger_target", "flag": "--trigger-target", "kind": "str",
      "default": "auto", "choices": null, "help": "...", "oneshot": false},
     ...]},
   {"title": "options", "args": [
     {"dest": "list_cameras", "flag": "--list-cameras", "kind": "bool",
      "default": false, "choices": null, "help": "...", "oneshot": true}]}]}
```

`kind` is one of `str`, `int`, `float`, `bool` (a `store_true` flag), `choice` (then
`choices` is the list). `default` is the parser's default *at the time of the call*,
which is what makes `$TRIGGER_TARGET` and friends show up: the dashboard runs
`--describe-args` in the same environment it will launch with. `oneshot` marks the flags
that make `macvision` print something and exit (`--list-ports`, `--list-cameras`); the
form never sends those with a launch, and the dashboard exposes them as probes instead.
`--help` and `--describe-args` itself are omitted.

**Values → argv** is one pure function, `dashboard.runner.argv_from_values(spec,
values)`, and it is the only place a form value becomes a flag. A value that is `null`
or `""` is omitted (so the parser's default applies); a `bool` adds the bare flag when
true; everything else adds `flag value`. A key that names no `dest` is an error, not a
silent drop.

## Contract 3: what the browser receives

The server is `dashboard/server.py`, stdlib `http.server`, on `127.0.0.1:50511` by
default. Routes:

| route | meaning |
|---|---|
| `GET /` `GET /static/...` | the page and its modules |
| `GET /manifest.webmanifest` `GET /icons/...` | the PWA manifest; install it for a standalone window, nothing more |
| `GET /api/args` | contract 2, verbatim |
| `GET /api/status` | `{"process": ..., "telemetry": ..., "hello": ...}` - the runner's state, the subscriber's counters, the last hello |
| `POST /api/preview` `{"values": {...}}` | `{"argv": [...], "command": "..."}` - what start would run, without running it |
| `POST /api/start` `{"values": {...}}` | spawns `python -m macvision <argv>`; 409 if one is already running, 400 on a bad value |
| `POST /api/stop` | SIGTERM, then SIGKILL after 5s; `{"exit_code": ...}` |
| `POST /api/oneshot` `{"flag": "--list-cameras"}` | runs a probe to completion; `{"exit_code", "stdout", "stderr"}`. Only flags marked `oneshot` are accepted, and 409 while `macvision` runs: probing opens every camera index, which stalls a running capture |
| `GET /api/log?n=200` | the last n lines of the child's output |
| `GET /events` | the SSE stream below |

A body over 1MB is a 413, a malformed or truncated one a 400, and an unexpected error
inside a route is a 500 with a JSON `error` - never a dropped connection. `HEAD` works
on the JSON and static routes.

The SSE stream carries these events (`event: <name>`, `data: <json>`):

| event | data | when |
|---|---|---|
| `hello` | contract 1's hello header | on connect (replayed if known), and whenever `macvision` reconnects |
| `frame` | contract 1's frame header, plus `"image": "data:image/jpeg;base64,..."` (`image/png` when opencv is absent) | at most `--fps` times a second, newest frame only, and only while a browser is connected |
| `stats` | contract 1's stats header | as they arrive |
| `process` | `{"state": "idle"\|"running"\|"exited", "pid", "argv", "command", "exit_code", "since"}` | on connect, and on every change |
| `telemetry` | `{"connected": bool, "messages", "frames", "bytes"}` | on connect and on every change |
| `log` | `{"stream": "stdout"\|"stderr", "line": "...", "t": ...}` | per line; the last lines are replayed on connect |
| `heartbeat` | `{"t": ...}` | every 5s, so a quiet stream stays open |

Image and boxes travel in the *same* event, so the overlay a panel draws always belongs to
the pixels under it - a separate MJPEG route would let them drift by a frame. Boxes and
the crosshair are drawn in the browser; the server never rasterises anything but the
image.

A browser that cannot keep up gets the newest frame, never a backlog: each SSE client
has a queue for the ordered events and a single slot for the frame.

The subscriber reconnects on its own, forever, with a pause between attempts - after a
refused connection and equally after a session that delivered nothing, so a port that
belongs to something else (an ssh tunnel with a dead far end, a stray HTTP server)
costs a retry every half second rather than a reconnect storm.

## The page

`dashboard/static/`, plain ES modules, no build step, no framework. `app.js` owns the
layout and a registry; **every panel is one file** under `panels/` with this shape:

```js
export default {
  id: "latency", title: "Latency", events: ["frame", "stats"],
  mount(el, ctx) {},          // ctx = { api, bus }
  update(type, data) {},      // called for each subscribed event
  unmount() {},
}
```

`bus.js` wraps the `EventSource` and dispatches by event name; `api.js` wraps the JSON
routes. Adding a panel is adding a file and one line in `panels/index.js`. Which panels
are visible, and the launch form's last values, persist in `localStorage` - per
browser, a convenience, nothing the server needs.

The manifest makes the page installable (Safari's *Add to Dock*, Chrome's install
button) for a standalone window. There is deliberately no service worker: the page is
meaningless offline, and a caching worker is the one component that would serve stale
files during development.

## Layout

| file | role | needs |
|---|---|---|
| `mac-app/macvision/telemetry.py` | contract 1, the publisher | - |
| `mac-app/macvision/__main__.py` | `--telemetry`, `--describe-args` (contract 2) | - |
| `mac-app/macvision/loop.py` | the one call, in the one place | - |
| `mac-app/tools/telemetry_tap.py` | the reference subscriber and the cost check | - |
| `mac-app/dashboard/runner.py` | the subprocess, `argv_from_values`, the log tail | - |
| `mac-app/dashboard/subscriber.py` | contract 1, the reader, with reconnect | - |
| `mac-app/dashboard/frames.py` | newest raw frame → JPEG or PNG, rate-limited | opencv, else stdlib PNG |
| `mac-app/dashboard/bus.py` | fan-out to SSE clients, frame coalescing | - |
| `mac-app/dashboard/server.py` | contract 3 | - |
| `mac-app/dashboard/__main__.py` | `python3 -m dashboard` | - |
| `mac-app/dashboard/static/` | the page | a browser |
| `mac-app/tests/test_telemetry.py` | the publisher and the framing | - |
| `mac-app/tests/test_dashboard.py` | the argv builder, the reader, the bus, the runner, PNG | - |

Everything marked `-` imports and tests with nothing installed, the same rule the rest
of `mac-app` lives by. The dashboard does not need the venv to *run* either; it needs it
only so that the `macvision` it launches has opencv and a detector.

## Running it

```bash
cd mac-app
source venv/bin/activate
python3 -m dashboard            # http://127.0.0.1:50511
```

The dashboard launches `macvision` with the interpreter it runs under and with
`MACVISION_TELEMETRY` set, so the frames come back on their own. Start it from a real
terminal: macOS grants the camera to the app that owns the process, and a `macvision`
spawned by the dashboard inherits whatever the terminal was granted.

## Things that will bite

- **A dashboard that dies without cleaning up orphans its `macvision`.** Ctrl-C, SIGTERM
  and a closed terminal all stop the child first. SIGKILL, a crash or an OOM kill do not,
  and nothing on macOS ties a child's life to its parent's without a watchdog that would
  sit on `macvision`'s hot path - so there is none. The orphan keeps the camera and the
  telemetry port, the next dashboard's child then prints `warning: telemetry disabled`,
  and the page shows the orphan's frames: the tell is a `hello.pid` that differs from
  `process.pid`. The dashboard prints a warning naming the pid when it sees a hello it
  did not launch; `kill <pid>` is the fix.
- **The page's form remembers values per browser.** A flag renamed in `parse_args` is
  pruned from the stored values when the form loads, so it cannot poison every launch;
  a value the parser now refuses still shows up as a 400 in the preview, which is the
  point of the preview.

## Out of scope, on purpose

- The Windows agent and the Pi. The dashboard controls one process on one machine.
- Changing a parameter while `macvision` runs. Every change is a restart, which is the
  only way the running configuration and the displayed one cannot disagree.
- Authentication. It binds to loopback; put it on an interface only on the lab link.
