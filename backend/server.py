#!/usr/bin/env python3
"""Homegrown backend HTTP server for nginx-lb-tests.

Every request sleeps for a random duration (simulating latency) plus
whatever this backend's current, independently-drifting offset adds on
top. No framework, no persistence -- just enough to give NGINX something
to load-balance across with controllable, observable timing.
"""
import http.server
import json
import os
import random
import socket
import threading
import time

LATENCY_MIN_MS = float(os.environ.get("LATENCY_MIN_MS", "10"))
LATENCY_MAX_MS = float(os.environ.get("LATENCY_MAX_MS", "20"))
PORT = int(os.environ.get("PORT", "8080"))

# Static file stub for a future throughput phase. Not exercised in Phase 1 --
# the response body stays a minimal static string -- but the plumbing to
# serve a larger file from a mounted volume is in place so a later phase can
# turn this on without restructuring the server.
STATIC_FILE_PATH = os.environ.get("STATIC_FILE_PATH", "")

# Fully decentralized relative-drift model (see AGENT.md, "backends share a
# baseline, drift independently"). All backends are meant to be configured
# with the SAME LATENCY_MIN_MS/MAX_MS -- no fixed per-backend "personality"
# -- so the only thing that ever differentiates one backend from another at
# a given moment is this independently, randomly redrawn offset. Replaces
# the earlier fixed 0/100/250ms staircase: that gave every backend the same
# shape of degradation, just phase-shifted, which meant an algorithm could
# partly "win" simply by finding and sticking with whichever backend
# happened to have the best fixed personality, rather than by genuinely
# tracking change. With a shared baseline, there is no stable ranking to
# fall back on -- any measured advantage can only come from real-time
# tracking.
#
# Offsets can be negative (currently faster than baseline) or positive
# (currently slower) -- asymmetric range on purpose: there's more physical
# room for a backend to get worse (unbounded contention, GC pauses, noisy
# neighbors) than to get better (a floor set by the network/OS stack), so
# DEGRADATION_OFFSET_MAX_MS is deliberately much larger in magnitude than
# |DEGRADATION_OFFSET_MIN_MS|.
DEGRADATION_OFFSET_MIN_MS = float(os.environ.get("DEGRADATION_OFFSET_MIN_MS", "-10"))
DEGRADATION_OFFSET_MAX_MS = float(os.environ.get("DEGRADATION_OFFSET_MAX_MS", "250"))

# Each backend keeps its own independent randomized-dwell timer (unchanged
# from the earlier decoupling work -- see "degradation timing decoupled and
# randomized" in AGENT.md), so the 5 backends' offsets reshuffle at
# uncorrelated moments rather than in lockstep. This is what makes "two
# backends land on the exact same offset" a non-issue without needing any
# cross-backend coordination: two independent continuous random draws
# landing on the same float is a ~10^-15 event (see AGENT.md) -- and even
# if it weren't, the user's call was that an occasional tie is fine; the
# fully decentralized model (no shared clock, no coordination) is more
# representative of real, independent backends than an approach that
# guarantees distinctness via a synchronized reshuffle would be.
DEGRADATION_MEAN_DWELL_SECONDS = float(os.environ.get("DEGRADATION_MEAN_DWELL_SECONDS", "60"))
# Each visit's actual dwell is drawn uniformly from [0.5, 1.5] x the mean:
# bounded (unlike an exponential's long tail, which would occasionally
# produce implausibly long or near-instant states), simple to reason
# about, and enough to break fixed periodicity -- an algorithm (or a longer
# real run that just re-measures the same clockwork pattern many times)
# can no longer rely on offset changes landing at predictable moments.
DEGRADATION_DWELL_JITTER = (0.5, 1.5)

# Ground-truth log of every realized offset change (timestamp, new offset
# in ms, dwell drawn). With both dwell time AND the offset itself
# randomized, the schedule can no longer be computed in advance from
# elapsed time alone -- this is what preserves this project's
# "reproducible, inspectable" property: reproducible via the logged
# realized timeline, not via predictability. Optional -- empty path (the
# default) just disables it, e.g. for a deployment where LOG_DIR isn't
# bind-mounted into the backend.
DEGRADATION_LOG_PATH = os.environ.get("DEGRADATION_LOG_PATH", "")

START_TIME = time.monotonic()
HOSTNAME = socket.gethostname()

_degradation_lock = threading.Lock()


def _draw_dwell_seconds() -> float:
    return random.uniform(*DEGRADATION_DWELL_JITTER) * DEGRADATION_MEAN_DWELL_SECONDS


def _draw_offset_ms() -> float:
    return random.uniform(DEGRADATION_OFFSET_MIN_MS, DEGRADATION_OFFSET_MAX_MS)


def _log_transition(offset_ms: float, dwell_seconds: float) -> None:
    if not DEGRADATION_LOG_PATH:
        return
    line = f"{int(time.time() * 1000)},{offset_ms:.3f},{dwell_seconds:.3f}\n"
    try:
        with open(DEGRADATION_LOG_PATH, "a") as f:
            f.write(line)
    except OSError:
        pass  # best-effort -- a logging hiccup shouldn't break request handling


_current_offset_ms = _draw_offset_ms()
_offset_started_at = START_TIME
_offset_dwell_seconds = _draw_dwell_seconds()
_log_transition(_current_offset_ms, _offset_dwell_seconds)


def current_degradation_offset_ms() -> float:
    """Advances this backend's independent randomized-dwell offset as
    needed, then returns the current offset in ms (signed -- negative is
    currently faster than baseline, positive is currently slower).

    Locked, not lock-free: ThreadingHTTPServer calls this from a new
    thread per request, so concurrent calls are the norm, not an edge
    case. Contention is a non-issue here -- the critical section is a few
    float comparisons, versus offset changes happening at most every
    several seconds.

    The while loop (not a single if) matters for correctness during a
    quiet period: if this backend gets no traffic for longer than several
    dwell periods (e.g. an algorithm has deprioritized it), the next call
    needs to walk through however many offset changes it *would* have
    passed through by now, redrawing each one along the way -- not just
    advance one step and silently leave the offset stale.
    """
    global _current_offset_ms, _offset_started_at, _offset_dwell_seconds
    with _degradation_lock:
        now = time.monotonic()
        while now - _offset_started_at >= _offset_dwell_seconds:
            _offset_started_at += _offset_dwell_seconds
            _current_offset_ms = _draw_offset_ms()
            _offset_dwell_seconds = _draw_dwell_seconds()
            _log_transition(_current_offset_ms, _offset_dwell_seconds)
        return _current_offset_ms


# Contention-cap knob (see EXPERIMENTS.md, "Backend contention / the
# many-LB problem"). Off (limit=None, unlimited) by default -- matches the
# original unbounded-thread-per-connection behavior exactly, so nothing
# changes for any existing run unless something explicitly sets a limit.
#
# threading.Semaphore/BoundedSemaphore can't be resized in place, so this
# is a small hand-rolled counter + condition variable instead -- needed
# because the whole point is to change capacity *live*, between traffic
# generator runs, without restarting the container (see the /admin/capacity
# endpoint below and traffic_generator.py's --contention flag).
class AdjustableLimiter:
    def __init__(self):
        self._cond = threading.Condition()
        self._limit = None  # None = unlimited
        self._in_use = 0
        self._total_acquires = 0
        self._blocked_acquires = 0

    def set_limit(self, limit):
        with self._cond:
            self._limit = limit
            # Reset counters here, not just at construction -- set_limit is
            # called at the start of every traffic_generator run (see
            # apply_contention_level's reset-then-apply pair), so this is
            # what gives each run's /admin/capacity GET a clean "since this
            # run's cap was set" count instead of bleeding in whatever
            # blocked/unblocked mix the previous run left behind.
            self._total_acquires = 0
            self._blocked_acquires = 0
            self._cond.notify_all()  # wake waiters -- the cap may have just been raised or removed

    def acquire(self):
        with self._cond:
            self._total_acquires += 1
            if self._limit is not None and self._in_use >= self._limit:
                self._blocked_acquires += 1
            while self._limit is not None and self._in_use >= self._limit:
                self._cond.wait()
            self._in_use += 1

    def release(self):
        with self._cond:
            self._in_use -= 1
            self._cond.notify()

    def stats(self):
        with self._cond:
            total = self._total_acquires
            blocked = self._blocked_acquires
            pct = (blocked / total * 100.0) if total else 0.0
            return {
                "limit": self._limit,
                "total": total,
                "blocked": blocked,
                "blocked_pct": round(pct, 2),
            }


_capacity_limiter = AdjustableLimiter()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "nginx-lb-tests-backend/0.1"

    def log_message(self, fmt, *args):
        # NGINX access logs are the experimental record; keep backend stdout quiet.
        pass

    def do_GET(self):
        if self.path == "/static":
            self._serve_static()
            return
        if self.path == "/admin/capacity":
            self._handle_get_capacity()
            return
        self._serve_default()

    def do_POST(self):
        if self.path == "/admin/capacity":
            self._handle_set_capacity()
            return
        self.send_response(404)
        self.end_headers()

    def _handle_get_capacity(self):
        # Direct-to-backend admin call, same as _handle_set_capacity --
        # lets traffic_generator.py ask "how often did this backend's cap
        # actually make a request wait?" at the end of a run, instead of
        # inferring it indirectly from chart shapes.
        body = json.dumps(_capacity_limiter.stats()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_set_capacity(self):
        # Direct-to-backend admin call, bypassing NGINX entirely -- same
        # pattern as sampler.py's direct_probe(). Not part of the
        # experiment's request path (no degradation offset, no latency
        # sleep), just a control-plane call to resize _capacity_limiter.
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
            limit = payload.get("limit")
            if limit is not None:
                limit = int(limit)
                if limit < 1:
                    raise ValueError("limit must be >= 1 (or null for unlimited)")
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as e:
            body = f"invalid request: {e}\n".encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        _capacity_limiter.set_limit(limit)
        body = f"capacity set to {limit if limit is not None else 'unlimited'}\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_default(self):
        _capacity_limiter.acquire()
        try:
            offset_ms = current_degradation_offset_ms()
            base_latency_ms = random.uniform(LATENCY_MIN_MS, LATENCY_MAX_MS)
            total_latency_ms = max(0.0, base_latency_ms + offset_ms)
            time.sleep(total_latency_ms / 1000.0)

            body = f"{HOSTNAME}\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Degradation-Offset-Ms", f"{offset_ms:.1f}")
            self.end_headers()
            self.wfile.write(body)
        finally:
            _capacity_limiter.release()

    def _serve_static(self):
        # Stub for the future throughput phase. Serves STATIC_FILE_PATH if
        # configured and present; otherwise reports the feature is not
        # enabled yet rather than pretending to serve real content.
        if not STATIC_FILE_PATH or not os.path.isfile(STATIC_FILE_PATH):
            self.send_response(501)
            body = b"static file serving not configured for this phase\n"
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        _capacity_limiter.acquire()
        try:
            offset_ms = current_degradation_offset_ms()
            with open(STATIC_FILE_PATH, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Degradation-Offset-Ms", f"{offset_ms:.1f}")
            self.end_headers()
            self.wfile.write(data)
        finally:
            _capacity_limiter.release()


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
