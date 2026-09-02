"""The subprocess: how a form becomes an argv, and how that argv becomes a macvision.

docs/DASHBOARD.md rule 1: macvision never knows the dashboard exists. Everything the
page can do, the command line can do, because the page IS the command line - the
runner builds `python -m macvision <argv>` and spawns it, tails its two streams, and
sends it the same SIGTERM Ctrl-C would. There is no other channel.

argv_from_values() is contract 2's one rule, in one pure function: the only place a
form value becomes a flag. It is strict where the doc says to be strict - a key that
names no dest is a ValueError, because a typo that silently dropped a flag would run a
configuration other than the one on screen, which is the exact confusion the
dashboard exists to remove.

The description (`--describe-args`) is fetched with the same environment the launch
uses, MACVISION_TELEMETRY included, so the defaults the form shows are the defaults
the launch would get.
"""

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from collections import deque

DEFAULT_TELEMETRY = "tcp://127.0.0.1:50510"
DESCRIBE_TIMEOUT_S = 30
STOP_TIMEOUT_S = 5.0
ONESHOT_TIMEOUT_S = 60
LOG_RING = 500

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


class RunnerBusy(RuntimeError):
    """start() or oneshot() while a child is alive. Its own type so the API can say
    409, not 500."""


# --- contract 2: values -> argv ---------------------------------------------------------
def argv_from_values(spec, values):
    """Walk the description in its own order and emit flags for the values given.

    None or "" -> omitted (the parser's default applies). bool -> the bare flag when
    true. int/float -> coerced, and a bad one is a ValueError that names the dest.
    choice -> must be one of choices. str (and any kind this code does not know) ->
    `flag str(value)`. oneshot flags are never emitted - they are probes, not options.
    A key that names no dest is a ValueError.
    """
    if not isinstance(values, dict):
        raise ValueError("values must be an object of dest -> value")
    args = [arg for group in spec.get("groups", []) for arg in group.get("args", [])]
    dests = {arg["dest"] for arg in args}
    unknown = sorted(k for k in values if k not in dests)
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(unknown)}")

    argv = []
    for arg in args:
        dest = arg["dest"]
        if dest not in values or arg.get("oneshot"):
            continue
        value = values[dest]
        if value is None or value == "":
            continue
        flag = arg["flag"]
        kind = arg.get("kind", "str")
        if kind == "bool":
            if _as_bool(value, dest):
                argv.append(flag)
        elif kind == "int":
            argv += [flag, str(_as_number(int, value, dest))]
        elif kind == "float":
            argv += [flag, str(_as_number(float, value, dest))]
        elif kind == "choice":
            argv += [flag, _as_choice(value, arg.get("choices") or [], dest)]
        else:
            argv += [flag, str(value)]
    return argv


def _as_bool(value, dest):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"{dest}: expected true or false, got {value!r}")


def _as_number(kind, value, dest):
    if isinstance(value, bool):
        raise ValueError(f"{dest}: expected {kind.__name__}, got {value!r}")
    try:
        if kind is int and isinstance(value, float):
            if value != int(value):
                raise ValueError
            return int(value)
        return kind(value)
    except (TypeError, ValueError):
        raise ValueError(f"{dest}: expected {kind.__name__}, got {value!r}")


def _as_choice(value, choices, dest):
    for choice in choices:
        if value == choice or str(value) == str(choice):
            return str(choice)
    raise ValueError(f"{dest}: {value!r} is not one of {list(choices)}")


def command_string(python, module, argv):
    return shlex.join([python, "-m", module, *argv])


# --- the child --------------------------------------------------------------------------
class Runner:
    def __init__(self, python, cwd, module="macvision", telemetry_url=DEFAULT_TELEMETRY,
                 on_event=None):
        self.python = python
        self.cwd = cwd
        self.module = module
        self.telemetry_url = telemetry_url
        self.on_event = on_event

        self._spec = None
        self._describe_lock = threading.Lock()
        self._lock = threading.Lock()
        self._proc = None
        self._argv = []
        self._started_at = None
        self._exited_at = None
        self._log = deque(maxlen=LOG_RING)
        self._readers = []

    # --- environment ---------------------------------------------------------------
    def _env(self, values=None):
        env = dict(os.environ)
        # The launch flag wins over the environment in macvision, so when the form
        # sets --telemetry itself the variable is left alone - one source of truth.
        if not (values and values.get("telemetry") not in (None, "")):
            env["MACVISION_TELEMETRY"] = self.telemetry_url
        return env

    # --- contract 2 ----------------------------------------------------------------
    def describe(self, refresh=False):
        """`python -m module --describe-args`, parsed and cached. RuntimeError, with
        the child's stderr in the message, when it cannot be had."""
        with self._describe_lock:
            if self._spec is not None and not refresh:
                return self._spec
            cmd = [self.python, "-m", self.module, "--describe-args"]
            try:
                # errors="replace", as for the child itself: one non-UTF-8 byte in a
                # warning (a device name, a locale-encoded path) must not become a
                # UnicodeDecodeError here, which is a ValueError and would be reported
                # as a bad request - or, at startup, as a crash.
                proc = subprocess.run(cmd, cwd=self.cwd, env=self._env(),
                                      capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      timeout=DESCRIBE_TIMEOUT_S)
            except OSError as exc:
                raise RuntimeError(f"cannot run {shlex.join(cmd)} in {self.cwd}: {exc}")
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"{shlex.join(cmd)} did not finish within "
                                   f"{DESCRIBE_TIMEOUT_S}s")
            if proc.returncode != 0:
                raise RuntimeError(f"{shlex.join(cmd)} exited {proc.returncode}: "
                                   f"{(proc.stderr or proc.stdout).strip()}")
            try:
                spec = json.loads(proc.stdout)
            except ValueError as exc:
                raise RuntimeError(f"{shlex.join(cmd)} printed something that is not "
                                   f"JSON ({exc}): {proc.stdout[:200]!r}")
            if not isinstance(spec, dict) or not isinstance(spec.get("groups"), list):
                raise RuntimeError(f"{shlex.join(cmd)} printed JSON without \"groups\"")
            self._spec = spec
            return spec

    def oneshot_flags(self):
        return [arg["flag"] for group in self.describe().get("groups", [])
                for arg in group.get("args", []) if arg.get("oneshot")]

    def preview(self, values):
        argv = argv_from_values(self.describe(), values)
        return {"argv": argv, "command": command_string(self.python, self.module, argv)}

    # --- run -------------------------------------------------------------------------
    def start(self, values):
        """Spawn the child. RunnerBusy if one is alive; ValueError on a bad value."""
        argv = argv_from_values(self.describe(), values)
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RunnerBusy(f"{self.module} is already running (pid {self._proc.pid})")
            cmd = [self.python, "-u", "-m", self.module, *argv]
            try:
                proc = subprocess.Popen(cmd, cwd=self.cwd, env=self._env(values),
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace",
                                        bufsize=1)
            except OSError as exc:
                raise RuntimeError(f"cannot run {shlex.join(cmd)} in {self.cwd}: {exc}")
            self._proc = proc
            self._argv = list(argv)
            self._started_at = time.time()
            self._exited_at = None
            self._readers = [
                threading.Thread(target=self._tail, args=(proc.stdout, "stdout"),
                                 daemon=True, name=f"{self.module}-stdout"),
                threading.Thread(target=self._tail, args=(proc.stderr, "stderr"),
                                 daemon=True, name=f"{self.module}-stderr"),
            ]
            for t in self._readers:
                t.start()
            threading.Thread(target=self._wait, args=(proc, list(self._readers)),
                             daemon=True, name=f"{self.module}-wait").start()
        status = self.status()
        self._emit("process", status)
        return status

    def _tail(self, stream, name):
        try:
            for line in stream:
                event = {"stream": name, "line": line.rstrip("\r\n"), "t": time.time()}
                self._log.append(event)
                self._emit("log", event)
        except (OSError, ValueError):
            pass          # the pipe went away under us: the child is gone
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _wait(self, proc, readers):
        proc.wait()
        # Stamped now, not after the join below: a status() asked during those seconds
        # must say when the child died, not when it was asked.
        with self._lock:
            if self._proc is proc:
                self._note_exit_locked()
        # The last lines are still in flight on the reader threads; the "exited" event
        # should follow them, not race them. Bounded, because a grandchild holding the
        # pipe open must not hold the state up with it.
        for t in readers:
            t.join(timeout=2.0)
        self._emit("process", self.status())

    def _note_exit_locked(self):
        if self._exited_at is None:
            self._exited_at = time.time()

    def stop(self, timeout=STOP_TIMEOUT_S):
        """SIGTERM, then SIGKILL after timeout; returns the exit code. None when nothing
        was ever started; a finished child's code when it is already gone."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return None
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                proc.wait()
        with self._lock:
            if self._proc is proc:
                self._note_exit_locked()
        return proc.returncode

    def status(self):
        with self._lock:
            proc = self._proc
            command = (command_string(self.python, self.module, self._argv)
                       if proc is not None else "")
            if proc is None:
                state, pid, code, since = "idle", None, None, None
            elif proc.poll() is None:
                state, pid, code, since = "running", proc.pid, None, self._started_at
            else:
                state, pid, code = "exited", proc.pid, proc.returncode
                # poll() can see the exit before the waiter thread does. Whoever sees
                # it first stamps it, so two calls never report two different times.
                self._note_exit_locked()
                since = self._exited_at
            return {"state": state, "pid": pid, "argv": list(self._argv),
                    "command": command, "exit_code": code, "since": since}

    # --- probes ----------------------------------------------------------------------
    def oneshot(self, flag, timeout=ONESHOT_TIMEOUT_S):
        """Run one probe flag to completion. Only flags the description marks oneshot
        are accepted: this route must not become a second way to launch anything."""
        allowed = self.oneshot_flags()
        if flag not in allowed:
            raise ValueError(f"{flag!r} is not a oneshot flag (allowed: "
                             f"{', '.join(allowed) or 'none'})")
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                # --list-cameras opens every camera index in turn, and the capture the
                # running child holds stalls while it does. A probe waits for the run.
                raise RunnerBusy(f"{self.module} is running (pid {self._proc.pid}); "
                                 f"probes wait until it stops")
        cmd = [self.python, "-m", self.module, flag]
        try:
            proc = subprocess.run(cmd, cwd=self.cwd, env=self._env(), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout)
        except OSError as exc:
            raise RuntimeError(f"cannot run {shlex.join(cmd)} in {self.cwd}: {exc}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{shlex.join(cmd)} did not finish within {timeout}s")
        return {"flag": flag, "exit_code": proc.returncode,
                "stdout": proc.stdout, "stderr": proc.stderr}

    def log_tail(self, n=200):
        n = max(0, int(n))
        lines = list(self._log)
        return lines[-n:] if n else []

    # --- events ----------------------------------------------------------------------
    def _emit(self, event, data):
        if self.on_event is None:
            return
        try:
            self.on_event(event, data)
        except Exception as exc:
            # A reader thread must outlive any bug in the listener.
            print(f"[runner] on_event({event}) raised ({exc!r}); continuing",
                  file=sys.stderr, flush=True)
