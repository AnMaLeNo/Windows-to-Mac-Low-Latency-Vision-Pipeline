"""Fan-out to the browsers: every SSE connection is a Client, and publish() is cheap.

docs/DASHBOARD.md, contract 3, last paragraph: "a browser that cannot keep up gets the
newest frame, never a backlog: each SSE client has a queue for the ordered events and a
single slot for the frame". That sentence is this module. Two rules fall out of it:

  - publish() never blocks. It appends to a bounded deque or overwrites one slot, under
    a lock that only ever guards those two operations, and sets an Event. A browser
    that stopped reading costs the publisher one dict assignment per event, and the
    subscriber thread - which is the one calling publish() with telemetry in hand -
    never waits on a socket it does not own.
  - Frames coalesce, everything else queues. A frame is a picture of "now" and an older
    one is worthless the moment a newer one exists; a log line, a stats message or a
    process transition is information, and losing one is losing history. So the frame
    slot is overwritten and the queue is appended to - and when even the queue fills
    (512 unread events is a browser that has gone away) the oldest are dropped and
    counted, so the number is visible rather than the memory growing.

The Bus also remembers what a fresh connection needs to catch up: the last hello,
process, telemetry and stats, and a ring of recent log lines. subscribe() preloads
those into the new client under the same lock publish() takes, so nothing is ever both
replayed and delivered live, and nothing published in the gap is lost.
"""

import threading
from collections import deque

# Events whose LAST value is state, and is replayed to a new connection.
REPLAYED = ("hello", "process", "telemetry", "stats")
QUEUE_MAXLEN = 512
LOG_RING = 500


class Client:
    """One SSE connection's view of the stream: a queue, a frame slot, a wake-up."""

    def __init__(self, maxlen=QUEUE_MAXLEN):
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._queue = deque(maxlen=maxlen)
        self._frame = None
        self.dropped = 0
        self.closed = False

    def push(self, event, data):
        """Called by Bus.publish(). Never blocks; never raises."""
        with self._lock:
            if self.closed:
                return
            if event == "frame":
                self._frame = (event, data)          # newest wins
            else:
                if len(self._queue) == self._queue.maxlen:
                    self.dropped += 1                # deque drops the oldest itself
                self._queue.append((event, data))
            # Set under the lock, so a wake means "something is here" and next() can
            # clear it under the same lock without a spurious or a lost wake-up.
            self._wake.set()

    def next(self, timeout=None):
        """Wait up to timeout for something; return the queued events in order, then
        the frame if one is pending. Empty list means the wait timed out (or the
        client was closed)."""
        self._wake.wait(timeout)
        with self._lock:
            out = list(self._queue)
            self._queue.clear()
            if self._frame is not None:
                out.append(self._frame)
                self._frame = None
            if not self.closed:
                self._wake.clear()          # closed stays woken so a loop can notice
        return out

    def close(self):
        """Wake the consumer for the last time. Anything pushed after this is dropped."""
        with self._lock:
            self.closed = True
            self._wake.set()


class Bus:
    def __init__(self, log_ring=LOG_RING):
        self._lock = threading.Lock()
        self._clients = set()
        self.last = {}                       # event -> data, for REPLAYED events
        self.log = deque(maxlen=log_ring)
        self.published = 0
        self.closed = False

    # --- clients --------------------------------------------------------------------
    def subscribe(self, replay=True):
        """A new Client, already registered and (by default) preloaded with replay()."""
        client = Client()
        with self._lock:
            if replay:
                for event, data in self._replay_locked():
                    client.push(event, data)
            if self.closed:
                client.close()
            self._clients.add(client)
        return client

    def unsubscribe(self, client):
        with self._lock:
            self._clients.discard(client)
        client.close()

    def client_count(self):
        return len(self._clients)

    def wants_frames(self):
        """frames.py asks this before encoding: with no browser, the encoder skips the
        frame and the whole JPEG/PNG cost is never paid."""
        return len(self._clients) > 0

    def close(self):
        """Wake every client for the last time, so the SSE loops end at shutdown
        instead of lingering until their next timeout."""
        with self._lock:
            self.closed = True
            clients = list(self._clients)
        for client in clients:
            client.close()

    # --- events ----------------------------------------------------------------------
    def publish(self, event, data):
        """Deliver (event, data) to every client. Never blocks on any of them."""
        with self._lock:
            if event in REPLAYED:
                self.last[event] = data
            elif event == "log":
                self.log.append(data)
            self.published += 1
            clients = list(self._clients)
            # The fan-out stays under the lock so that subscribe()'s preload and this
            # delivery cannot interleave: a client sees an event replayed or live,
            # never both, never neither. Each push is an append and an Event.set.
            for client in clients:
                client.push(event, data)

    def forget(self, event):
        """Drop a remembered event so no new connection is given it. The hello handler
        calls this for "stats": those numbers belong to the run that sent them, and a
        page connecting after a restart must not see the old run's summary replayed
        under the new run's hello."""
        with self._lock:
            self.last.pop(event, None)

    def replay(self):
        """The events a new connection receives first: hello, process, telemetry, the
        last stats, then the log ring - in that order, skipping what is not known."""
        with self._lock:
            return self._replay_locked()

    def _replay_locked(self):
        out = []
        for event in REPLAYED:
            if event in self.last:
                out.append((event, self.last[event]))
        for data in self.log:
            out.append(("log", data))
        return out
