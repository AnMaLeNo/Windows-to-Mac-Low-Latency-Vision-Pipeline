"""Contract 1, the reading end: connect to macvision's telemetry socket, keep connecting.

The framing (MAGIC, MessageReader) is imported from macvision.telemetry, not copied:
there is exactly one implementation of the wire layout, and this is a user of it.

The one design decision here is that the connection loop never gives up. macvision is
started and stopped by the dashboard's own runner, many times per session, and the
socket is only there while it runs. So "connection refused" is the normal state, not an
error: on loopback it costs a few microseconds and the subscriber simply tries again
half a second later. The dashboard survives every macvision restart with no state to
reset and nothing for the page to click.

The same pacing applies after a session ends, not only after a refused connect. A peer
that accepts and closes at once - a port held by something that is not macvision, or
one that speaks HTTP - would otherwise be reconnected to thousands of times a second,
and every attempt publishes two telemetry state changes to every browser. The one
exception: a session that delivered messages may reconnect at once, once, so a
macvision restart is picked up without the gap. Two immediate retries in a row is what
an accept-and-drop peer looks like, and the second waits.

Losing sync is the other exit. MessageReader raises ValueError when the magic is not
where a message boundary should be, and the only correct response is the one the
protocol prescribes - close and reconnect - because the publisher starts every
connection with a fresh hello at a message boundary.

The callbacks run on this thread. They must be quick (the dashboard's are: a dict
assignment and an Event.set), and they must not be able to kill the thread, so their
exceptions are caught and reported on stderr at the trigger module's cadence.
"""

import socket
import sys
import threading
import time

from macvision.telemetry import MessageReader

CONNECT_TIMEOUT_S = 1.0
RETRY_S = 0.5
RECV_BYTES = 65536
# A recv timeout so the loop can notice stop() while the far end is silent. It is not an
# error: a running macvision with a source that delivers nothing sends nothing.
RECV_TIMEOUT_S = 1.0


class TelemetrySubscriber:
    def __init__(self, host, port, on_message, on_state=None,
                 retry_s=RETRY_S, connect_timeout_s=CONNECT_TIMEOUT_S):
        self.host = host
        self.port = port
        self.on_message = on_message
        self.on_state = on_state
        self.retry_s = retry_s
        self.connect_timeout_s = connect_timeout_s

        self.connected = False
        self.messages = 0
        self.frames = 0
        self.bytes = 0
        self.reconnects = 0
        self.callback_errors = 0
        self.last_message_at = None
        self.last_error = None

        self._sock = None
        self._sock_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="telemetry-subscriber")

    # --- lifecycle -------------------------------------------------------------------
    def start(self):
        if not self._thread.is_alive() and not self._stop.is_set():
            self._thread.start()

    def stop(self, timeout=3.0):
        """Close the socket out from under the reader and join it. Bounded: every wait
        in the loop is a timed one."""
        self._stop.set()
        self._close()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def alive(self):
        return self._thread.is_alive()

    def status(self):
        return {
            "connected": self.connected,
            "target": f"tcp://{self.host}:{self.port}",
            "messages": self.messages,
            "frames": self.frames,
            "bytes": self.bytes,
            "reconnects": self.reconnects,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
        }

    # --- the loop --------------------------------------------------------------------
    def _loop(self):
        # Whether the last reconnect skipped the wait: one immediate retry after a
        # productive session, never two in a row (see the module docstring).
        hurried = False
        while not self._stop.is_set():
            sock = self._connect()
            if sock is None:
                # Refused, unreachable, or timed out: macvision is not running yet, or
                # not any more. Wait on the stop Event, never time.sleep(), so stop()
                # is immediate.
                hurried = False
                self._stop.wait(self.retry_s)
                continue
            delivered = self._serve(sock)
            if delivered and not hurried:
                hurried = True
                continue
            hurried = False
            self._stop.wait(self.retry_s)

    def _connect(self):
        try:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=self.connect_timeout_s)
        except OSError as exc:
            self.last_error = str(exc)
            return None
        sock.settimeout(RECV_TIMEOUT_S)
        with self._sock_lock:
            if self._stop.is_set():
                sock.close()
                return None
            self._sock = sock
        return sock

    def _serve(self, sock):
        """Read until the peer closes, sync is lost or stop() is called. Returns how
        many messages the session delivered: that is what earns an immediate retry."""
        reader = MessageReader()
        self.connected = True
        self._state(True)
        why = None
        delivered = 0
        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(RECV_BYTES)
                except socket.timeout:
                    continue
                if not data:
                    why = "closed by peer"
                    break
                self.bytes += len(data)
                for header, payload in reader.feed(data):
                    self.messages += 1
                    delivered += 1
                    if header.get("type") == "frame":
                        self.frames += 1
                    self.last_message_at = time.time()
                    self._deliver(header, payload)
        except ValueError as exc:          # MessageReader: lost sync
            why = str(exc)
        except OSError as exc:             # reset, or stop() closed the socket
            why = str(exc)
        finally:
            self._close()
            self.connected = False
            if why is not None:
                self.last_error = why
            self.reconnects += 1
            self._state(False)
        return delivered

    def _close(self):
        with self._sock_lock:
            sock, self._sock = self._sock, None
        if sock is None:
            return
        # shutdown() is what actually wakes a recv() blocked on another thread; a bare
        # close() leaves it waiting until the fd's last reference goes.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # --- callbacks: quick, and never fatal -------------------------------------------
    def _deliver(self, header, payload):
        try:
            self.on_message(header, payload)
        except Exception as exc:
            self._callback_failed("on_message", exc)

    def _state(self, connected):
        if self.on_state is None:
            return
        try:
            self.on_state(connected)
        except Exception as exc:
            self._callback_failed("on_state", exc)

    def _callback_failed(self, which, exc):
        self.callback_errors += 1
        n = self.callback_errors
        if n in (1, 10) or n % 500 == 0:
            print(f"[telemetry] {which} raised ({exc!r}); continuing [{n} so far]",
                  file=sys.stderr, flush=True)
