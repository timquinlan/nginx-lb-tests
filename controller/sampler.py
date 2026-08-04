#!/usr/bin/env python3
"""Runs backend validation, the priming sample, then one continuous
per-algorithm sampling loop per dynamic algorithm (aco, mc). Reads the
backend list from upstream-hosts.txt (same source NGINX config generation
reads) so the controller and NGINX always agree on the pool. Logs to
stderr only -- this is Stream 1 (operational), never analyzed by Phase 4.

Phase 1 wired every dynamic algorithm to the equal-weight stub so the
plumbing (priming, sampling cadence, sparse-observation fallback, config
writer, change counter) could be exercised end-to-end before any real
ACO/Markov math existed. Phase 2 swapped in the real ACO module for /aco.
Phase 3 swaps in the real Markov module for /mc -- both dynamic algorithms
now run their real math simultaneously.
"""
import http.client
import json
import os
import sys
import threading
import time

from common import (
    read_upstream_hosts,
    log,
    LOG_DIR,
    TICK_SECONDS,
    BACKEND_PORT,
    DYNAMIC_ALGO_NAMES,
    resolve_ip_to_host_map,
)
from algorithms.aco import AntColonyOptimization
from algorithms.markov import MarkovChain
import config_writer
import validate_backends

PROBE_TIMEOUT_SECONDS = 5

# Phase 4 needs the same ip:port -> host attribution sampler.py already
# builds every window (see resolve_ip_to_host_map / AGENT.md's
# "$upstream_addr gotcha"), but analysis is meant to be runnable directly
# from the host machine against the bind-mounted ./logs directory, where
# Docker's embedded DNS (service names like "backend-1") isn't reachable.
# Rather than require analysis to have live DNS access -- which also
# wouldn't reflect the exact resolution NGINX actually used at request
# time -- the sampling loop persists its own resolved mapping to a small
# JSON snapshot on every window, so Phase 4 reads the *actual* mapping
# used instead of re-deriving (and potentially mismatching) it.
IP_TO_HOST_PATH = os.path.join(LOG_DIR, "ip_to_host.json")

ALGORITHMS = {
    "aco": AntColonyOptimization(),
    "mc": MarkovChain(),
    # Own instance, own pheromone state, fed by /combo's own traffic --
    # deliberately not a shared reference to the "aco" instance above, so
    # combo stands on its own the same way aco/mc each do (see AGENT.md,
    # "combo" for the tradeoff this implies). Both instances read the same
    # ACO_EVAPORATION_RATE/ACO_DEPOSIT_CONSTANT env vars (module-level in
    # aco.py), so combo always runs with whatever tuning aco itself is
    # currently using.
    "combo": AntColonyOptimization(),
}

_log_offsets = {}  # algo_name -> byte offset into its access log, in-memory
                    # for the life of this process (see AGENT.md)


def direct_probe(host, port=BACKEND_PORT, timeout=PROBE_TIMEOUT_SECONDS):
    """Bypass NGINX entirely: one direct HTTP GET to a backend, timed in
    Python. Used for priming (no traffic/logs exist yet) and as the
    sparse-observation fallback within a sampling window."""
    start = time.monotonic()
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
    finally:
        conn.close()
    return (time.monotonic() - start) * 1000.0


def probe_all_backends(hosts):
    return {host: direct_probe(host) for host in hosts}


def write_ip_to_host_snapshot(hosts):
    """Resolve and persist the ip:port -> host map (see IP_TO_HOST_PATH).
    Re-resolving and overwriting on every call keeps it fresh the same way
    resolve_ip_to_host_map's own callers already re-resolve every sampling
    window; the atomic replace matches config_writer._write_state's pattern
    so a reader never sees a partially-written file.

    Both dynamic algorithms' sampling_loop threads call this independently,
    every window (see AGENT.md) -- the tmp filename must be unique per
    caller, not just per target file, or two threads racing this in the
    same window can steal each other's tmp file: thread A writes tmp,
    thread B (same shared tmp path) overwrites/renames it away, then
    thread A's os.replace() raises FileNotFoundError on a tmp file that no
    longer exists. That exception is fatal to a daemon thread (uncaught ->
    thread dies silently, no further weight updates from it ever again) --
    caught live via a real dual-sampling-loop run during Phase 4
    verification, not theoretical."""
    mapping = resolve_ip_to_host_map(hosts)
    tmp_path = f"{IP_TO_HOST_PATH}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp_path, "w") as f:
        json.dump(mapping, f)
    os.replace(tmp_path, IP_TO_HOST_PATH)
    return mapping


def access_log_path(algo_name):
    return os.path.join(LOG_DIR, f"{algo_name}.access.log")


def read_new_window_lines(algo_name):
    path = access_log_path(algo_name)
    if not os.path.exists(path):
        return []
    offset = _log_offsets.get(algo_name, 0)
    with open(path, "r") as f:
        f.seek(offset)
        lines = f.readlines()
        _log_offsets[algo_name] = f.tell()
    return lines


def parse_window_observations(lines, ip_to_host):
    """Returns {host: mean_latency_ms} from $upstream_header_time (TTFB),
    the field this loop's weight decisions are grounded in. $upstream_addr
    is a resolved ip:port, not the original hostname -- see AGENT.md."""
    sums = {}
    counts = {}
    for line in lines:
        fields = [f.strip() for f in line.strip().split("|")]
        if len(fields) < 5:
            continue
        # Failover can log multiple comma-separated addresses; only the
        # first attempt is attributed here.
        upstream_addr = fields[2].split(",")[0].strip()
        header_time_str = fields[4]
        host = ip_to_host.get(upstream_addr)
        if host is None or header_time_str == "-":
            continue
        try:
            header_time_ms = float(header_time_str) * 1000.0
        except ValueError:
            continue
        sums[host] = sums.get(host, 0.0) + header_time_ms
        counts[host] = counts.get(host, 0) + 1
    return {host: sums[host] / counts[host] for host in sums}


def fill_sparse_observations(observations, hosts):
    """Sparse observation handling: any backend with zero observations this
    window gets a single direct fallback probe (bypassing NGINX), logged to
    stderr so the operator can see when it's happening. Not logged to the
    access log -- operational data only (Stream 1), not part of the
    experiment evidence (Stream 2)."""
    for host in hosts:
        if host not in observations:
            latency_ms = direct_probe(host)
            observations[host] = latency_ms
            log("sampler", f"fallback probe used for {host} (zero observations this window): {latency_ms:.1f}ms")
    return observations


def run_priming(hosts):
    write_ip_to_host_snapshot(hosts)
    log("sampler", f"priming: probing {len(hosts)} backend(s) directly")
    observations = probe_all_backends(hosts)
    log("sampler", f"priming observations (ms): {observations}")
    for algo_name, algorithm in ALGORITHMS.items():
        weights = algorithm.update(observations)
        change_count, _ = config_writer.apply_weights(algo_name, hosts, weights)
        log("sampler", f"priming: {algo_name} weights={weights} change_count={change_count}")


# Same retry-then-give-up-loudly shape as validate_backends.py's RETRIES/
# RETRY_DELAY, applied to the ongoing per-window loop instead of just the
# one-time startup gate -- validate_backends.py only ever runs once at
# container boot, so it has zero protection against a transient failure
# hitting this loop hours into a long-running container's life. Found
# 2026-07-29: a backend container restart (to deploy an unrelated code
# change) caused a brief DNS-resolution failure; aco's sampling thread hit
# it inside direct_probe(), the uncaught exception killed the thread
# outright, and its weights silently froze for the rest of the container's
# life -- multiple experiment runs looked like static weighted round robin
# with no visible error anywhere in their own output.
WINDOW_RETRY_ATTEMPTS = 3
WINDOW_RETRY_DELAY_SECONDS = 1.0


def sampling_loop(algo_name, hosts):
    """Priming just computed and applied one full window's worth of
    weights. Sleeping a full tick before this loop's first iteration avoids
    immediately redoing that work against zero new log lines (which would
    just re-trigger a fallback probe for every backend for no reason)."""
    algorithm = ALGORITHMS[algo_name]
    time.sleep(TICK_SECONDS)
    while True:
        window_start = time.monotonic()
        # Snapshot the log-read offset so a failed attempt can restore it
        # before retrying -- read_new_window_lines() advances _log_offsets
        # as a side effect, so retrying without resetting it first would
        # have attempt 2 silently miss whatever lines attempt 1 already
        # consumed before failing partway through.
        offset_before_window = _log_offsets.get(algo_name, 0)
        for attempt in range(1, WINDOW_RETRY_ATTEMPTS + 1):
            try:
                ip_to_host = write_ip_to_host_snapshot(hosts)
                lines = read_new_window_lines(algo_name)
                observations = parse_window_observations(lines, ip_to_host)
                observations = fill_sparse_observations(observations, hosts)
                weights = algorithm.update(observations)
                change_count, _ = config_writer.apply_weights(algo_name, hosts, weights)
                log("sampler", f"{algo_name}: window complete, {len(lines)} log line(s) seen, change_count={change_count}")
                break
            except Exception as e:
                _log_offsets[algo_name] = offset_before_window
                if attempt < WINDOW_RETRY_ATTEMPTS:
                    log("sampler", f"{algo_name}: window failed on attempt {attempt}/{WINDOW_RETRY_ATTEMPTS} ({e!r}), retrying in {WINDOW_RETRY_DELAY_SECONDS}s")
                    time.sleep(WINDOW_RETRY_DELAY_SECONDS)
                else:
                    # Fast stop: bounded, quick retries (~a few seconds total),
                    # not a long/exponential backoff or an infinite retry --
                    # give up on *this window* and let the next tick's fresh
                    # attempt take another shot, rather than blocking this
                    # thread indefinitely on a backend that may be genuinely,
                    # persistently down.
                    log("sampler", f"{algo_name}: window failed after {WINDOW_RETRY_ATTEMPTS} attempts ({e!r}) -- giving up on this window, will try fresh next tick")
        elapsed = time.monotonic() - window_start
        time.sleep(max(0.0, TICK_SECONDS - elapsed))


def start_sampling_loops(hosts):
    """One background thread per dynamic algorithm. Threads (not
    processes) are enough here -- each loop is I/O-bound (file reads, HTTP
    probes, the `nginx -s reload` subprocess call), not CPU-bound, so the
    GIL isn't a bottleneck."""
    threads = []
    for algo_name in DYNAMIC_ALGO_NAMES:
        t = threading.Thread(target=sampling_loop, args=(algo_name, hosts), daemon=True, name=f"sampler-{algo_name}")
        t.start()
        threads.append(t)
    return threads


def main():
    """Runs priming synchronously, starts the sampling loops in background
    threads, and returns without blocking -- the caller (entrypoint.py)
    still has to start/supervise NGINX. Running this file directly blocks
    forever instead, for standalone testing."""
    hosts = read_upstream_hosts()
    unreachable = validate_backends.validate(hosts)
    if unreachable:
        validate_backends.print_failure_report(hosts, unreachable)
        sys.exit(1)

    run_priming(hosts)
    return start_sampling_loops(hosts)


if __name__ == "__main__":
    main()
    while True:
        time.sleep(3600)
