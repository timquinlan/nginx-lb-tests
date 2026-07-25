#!/usr/bin/env python3
"""Homegrown backend HTTP server for upstream-rl.

Every request sleeps for a random duration (simulating latency) plus
whatever the current staircase degradation adds on top. No framework,
no persistence -- just enough to give NGINX something to load-balance
across with controllable, observable timing.
"""
import http.server
import os
import random
import socket
import time

LATENCY_MIN_MS = float(os.environ.get("LATENCY_MIN_MS", "10"))
LATENCY_MAX_MS = float(os.environ.get("LATENCY_MAX_MS", "20"))
DEGRADATION_MULTIPLIER = float(os.environ.get("DEGRADATION_MULTIPLIER", "2"))
TICK_SECONDS = float(os.environ.get("TICK_SECONDS", "60"))
PORT = int(os.environ.get("PORT", "8080"))

# Static file stub for a future throughput phase. Not exercised in Phase 1 --
# the response body stays a minimal static string -- but the plumbing to
# serve a larger file from a mounted volume is in place so a later phase can
# turn this on without restructuring the server.
STATIC_FILE_PATH = os.environ.get("STATIC_FILE_PATH", "")

# Staircase degradation cycle: baseline -> baseline+100ms -> baseline+250ms -> reset.
# Cycle length is DEGRADATION_MULTIPLIER * TICK_SECONDS, split into three equal
# segments, one per state. The timer is internal (process start time) -- no
# external orchestration needed.
DEGRADATION_STEPS_MS = (0, 100, 250)
CYCLE_SECONDS = DEGRADATION_MULTIPLIER * TICK_SECONDS
SEGMENT_SECONDS = CYCLE_SECONDS / len(DEGRADATION_STEPS_MS)

START_TIME = time.monotonic()
HOSTNAME = socket.gethostname()


def current_degradation_state() -> int:
    elapsed = time.monotonic() - START_TIME
    position = elapsed % CYCLE_SECONDS
    return min(int(position // SEGMENT_SECONDS), len(DEGRADATION_STEPS_MS) - 1)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "upstream-rl-backend/0.1"

    def log_message(self, fmt, *args):
        # NGINX access logs are the experimental record; keep backend stdout quiet.
        pass

    def do_GET(self):
        if self.path == "/static":
            self._serve_static()
            return
        self._serve_default()

    def _serve_default(self):
        state = current_degradation_state()
        base_latency_ms = random.uniform(LATENCY_MIN_MS, LATENCY_MAX_MS)
        total_latency_ms = base_latency_ms + DEGRADATION_STEPS_MS[state]
        time.sleep(total_latency_ms / 1000.0)

        body = f"{HOSTNAME}\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Degradation-State", str(state))
        self.end_headers()
        self.wfile.write(body)

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

        state = current_degradation_state()
        with open(STATIC_FILE_PATH, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Degradation-State", str(state))
        self.end_headers()
        self.wfile.write(data)


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
