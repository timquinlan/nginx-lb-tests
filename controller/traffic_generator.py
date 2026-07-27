#!/usr/bin/env python3
"""Traffic generator -- invoked manually via `docker exec` for each
experiment run, against the already-running, already-primed container (see
AGENT.md "Entrypoint / Orchestration Model"). Hits NGINX at localhost.
Reads algorithm paths from the generated NGINX config dynamically (never
hardcodes /rr, /aco, /mc), so adding a new algorithm's location block is all
that's needed for it to be picked up here automatically.

Does not log individual requests -- NGINX access logs are the experimental
record (Stream 2). This process only prints run-level progress to stdout
and appends one run summary to runs.log.
"""
import argparse
import datetime
import http.client
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# analysis/ is copied to /app/analysis in the image (see controller/
# Dockerfile) but isn't on sys.path by default since this script's own
# directory (/app) is what Python adds automatically. analysis/ is kept
# import-independent from controller/ (see analysis/log_reader.py) so it
# also works standalone from the host -- this is the one place that
# bridges the two for the automatic end-of-run hook.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis"))
import analyze

from common import (
    log,
    now_ms,
    read_location_paths,
    NGINX_EXPERIMENT_PORT,
    RUNS_LOG_PATH,
    LOG_DIR,
    DYNAMIC_ALGO_NAMES,
    TICK_SECONDS,
)
import config_writer

REQUEST_TIMEOUT_SECONDS = 10
MIN_WORKERS_PER_PATH = 20
# Rough Little's Law sizing (concurrency ~= rps * latency): the slowest
# backend's max latency range plus the top degradation step is ~330ms with
# the default backend pool, so 0.5s/request gives comfortable headroom. A
# fixed small pool would otherwise silently throttle throughput at higher
# --rps targets with no error at all -- ThreadPoolExecutor.submit() just
# queues faster than it drains, so the achieved rate quietly falls short
# of what was requested instead of failing loudly.
WORKERS_PER_REQUESTED_RPS = 0.5


def send_request(path):
    try:
        conn = http.client.HTTPConnection("localhost", NGINX_EXPERIMENT_PORT, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
        finally:
            conn.close()
    except OSError:
        # Individual request failures aren't logged here -- NGINX's access
        # log (status code) and error log already capture this on the proxy
        # side. A generator-side exception just means the request never
        # made it to NGINX at all (e.g. transient connection refused).
        pass


def path_traffic_loop(path, rps, stop_event, executor):
    interval = 1.0 / rps
    next_time = time.monotonic()
    while not stop_event.is_set():
        executor.submit(send_request, path)
        next_time += interval
        sleep_for = next_time - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            next_time = time.monotonic()  # fell behind; resync instead of bursting to catch up


def current_change_counts():
    return {algo: config_writer.read_state(algo)["change_count"] for algo in DYNAMIC_ALGO_NAMES}


def format_status_line(tick_index, total_ticks, rps, change_counts):
    dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    counts_str = " | ".join(f"{algo} changes: {n}" for algo, n in change_counts.items())
    return f"[{dt}] tick: {tick_index}/{total_ticks} | rps: {rps} | {counts_str}"


def next_run_index():
    if not os.path.exists(RUNS_LOG_PATH):
        return 1
    with open(RUNS_LOG_PATH) as f:
        return sum(1 for _ in f) + 1


def write_run_record(record):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(RUNS_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def invoke_analysis(run_index):
    """End-of-run hook: analyzes the run that just finished and prints its
    summary/warnings/chart paths to stdout. Analysis failures are logged,
    not raised -- the traffic-generator run itself already succeeded and
    its record is already in runs.log by this point, so a broken chart
    shouldn't be reported as a failed run."""
    try:
        analyze.run_analysis(logs_dir=LOG_DIR, run_index=run_index)
    except Exception as e:
        log("traffic_generator", f"Phase 4 analysis failed for run {run_index}: {e}")


def run(tick_seconds, rps, duration_ticks):
    paths = read_location_paths()
    if not paths:
        log("traffic_generator", "no location paths found in generated NGINX config -- nothing to send traffic to")
        sys.exit(1)

    run_index = next_run_index()
    total_duration_seconds = tick_seconds * duration_ticks
    start_ts_ms = now_ms()
    start_monotonic = time.monotonic()

    log("traffic_generator", f"run {run_index}: paths={paths} tick={tick_seconds}s rps={rps}/path duration={duration_ticks} ticks ({total_duration_seconds}s)")

    workers_per_path = max(MIN_WORKERS_PER_PATH, int(rps * WORKERS_PER_REQUESTED_RPS))
    log("traffic_generator", f"sizing thread pool at {workers_per_path} workers/path for {rps}rps/path")

    stop_event = threading.Event()
    executors = {path: ThreadPoolExecutor(max_workers=workers_per_path) for path in paths}
    loop_threads = []
    for path in paths:
        t = threading.Thread(
            target=path_traffic_loop, args=(path, rps, stop_event, executors[path]), daemon=True
        )
        t.start()
        loop_threads.append(t)

    interrupted = False

    def handle_signal(signum, frame):
        nonlocal interrupted
        interrupted = True
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    tick_index = 0
    try:
        while tick_index < duration_ticks and not interrupted:
            time.sleep(tick_seconds)
            tick_index += 1
            print(format_status_line(tick_index, duration_ticks, rps, current_change_counts()), flush=True)
    finally:
        stop_event.set()
        for executor in executors.values():
            executor.shutdown(wait=True, cancel_futures=False)

    end_ts_ms = now_ms()
    actual_duration_s = time.monotonic() - start_monotonic

    record = {
        "run_index": run_index,
        "start_ts_ms": start_ts_ms,
        "end_ts_ms": end_ts_ms,
        "tick_seconds": tick_seconds,
        "rps_per_path": rps,
        "planned_duration_ticks": duration_ticks,
        "actual_duration_s": round(actual_duration_s, 3),
        "paths": paths,
        "change_counts": current_change_counts(),
        "interrupted": interrupted,
    }
    write_run_record(record)
    log("traffic_generator", f"run {run_index} finished, wrote record to {RUNS_LOG_PATH}")
    invoke_analysis(run_index)


def main():
    parser = argparse.ArgumentParser(description="upstream-rl traffic generator")
    parser.add_argument(
        "--tick",
        type=float,
        default=TICK_SECONDS,
        help=(
            "base tick unit in seconds -- defaults to this container's own TICK_SECONDS "
            f"env var (currently {TICK_SECONDS}s), which is also what the sampling loops "
            "and backend degradation cycles are actually running on. Only override this "
            "if you deliberately want this run's status-line/duration accounting to use a "
            "different unit than the sampling cadence underneath -- see AGENT.md."
        ),
    )
    parser.add_argument("--rps", type=float, default=5, help="requests per second, per algorithm path (default 5)")
    parser.add_argument("--duration", type=int, required=True, help="run duration, in ticks")
    args = parser.parse_args()

    run(tick_seconds=args.tick, rps=args.rps, duration_ticks=args.duration)


if __name__ == "__main__":
    main()
