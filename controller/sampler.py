#!/usr/bin/env python3
"""Runs backend validation, the priming sample, then one continuous
per-algorithm sampling loop per dynamic algorithm (aco, mc). Reads the
backend list from upstream-hosts.txt (same source NGINX config generation
reads) so the controller and NGINX always agree on the pool. Logs to
stderr only -- this is Stream 1 (operational), never analyzed by Phase 4.

Phase 1 wires every dynamic algorithm to the equal-weight stub so the
plumbing (priming, sampling cadence, sparse-observation fallback, config
writer, change counter) is exercised end-to-end before any real ACO/Markov
math exists. Phase 2/3 swap in real modules here without touching anything
else in this file.
"""
import http.client
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
from algorithms.stub import EqualWeightStub
import config_writer
import validate_backends

PROBE_TIMEOUT_SECONDS = 5

ALGORITHMS = {
    "aco": EqualWeightStub(),
    "mc": EqualWeightStub(),
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
    log("sampler", f"priming: probing {len(hosts)} backend(s) directly")
    observations = probe_all_backends(hosts)
    log("sampler", f"priming observations (ms): {observations}")
    for algo_name, algorithm in ALGORITHMS.items():
        weights = algorithm.update(observations)
        change_count, _ = config_writer.apply_weights(algo_name, hosts, weights)
        log("sampler", f"priming: {algo_name} weights={weights} change_count={change_count}")


def sampling_loop(algo_name, hosts):
    """Priming just computed and applied one full window's worth of
    weights. Sleeping a full tick before this loop's first iteration avoids
    immediately redoing that work against zero new log lines (which would
    just re-trigger a fallback probe for every backend for no reason)."""
    algorithm = ALGORITHMS[algo_name]
    time.sleep(TICK_SECONDS)
    while True:
        window_start = time.monotonic()
        ip_to_host = resolve_ip_to_host_map(hosts)
        lines = read_new_window_lines(algo_name)
        observations = parse_window_observations(lines, ip_to_host)
        observations = fill_sparse_observations(observations, hosts)
        weights = algorithm.update(observations)
        change_count, _ = config_writer.apply_weights(algo_name, hosts, weights)
        log("sampler", f"{algo_name}: window complete, {len(lines)} log line(s) seen, change_count={change_count}")
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
