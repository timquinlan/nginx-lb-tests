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
import math
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


def run(tick_seconds, rps, duration_minutes):
    paths = read_location_paths()
    if not paths:
        log("traffic_generator", "no location paths found in generated NGINX config -- nothing to send traffic to")
        sys.exit(1)

    # --duration is specified in minutes (the actual wall-clock length of
    # the run); ticks are just the sleep-loop's internal unit, so convert
    # here rather than making the caller do tick arithmetic. ceil, not
    # round, so the run always covers *at least* the requested duration --
    # a requested duration that isn't a whole multiple of tick_seconds
    # rounds the actual run slightly long, never short.
    duration_ticks = math.ceil((duration_minutes * 60.0) / tick_seconds)

    run_index = next_run_index()
    total_duration_seconds = tick_seconds * duration_ticks
    start_ts_ms = now_ms()
    start_monotonic = time.monotonic()
    baseline_change_counts = current_change_counts()

    log("traffic_generator", f"run {run_index}: paths={paths} tick={tick_seconds}s rps={rps}/path "
        f"requested_duration={duration_minutes}min -> {duration_ticks} ticks ({total_duration_seconds}s actual)")

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
            run_change_counts = {
                algo: current_change_counts()[algo] - baseline_change_counts[algo]
                for algo in baseline_change_counts
            }
            print(format_status_line(tick_index, duration_ticks, rps, run_change_counts), flush=True)
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
        "requested_duration_minutes": duration_minutes,
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
    parser = argparse.ArgumentParser(description="nginx-lb-tests traffic generator")
    parser.add_argument(
        "--tick",
        type=float,
        default=TICK_SECONDS,
        help=(
            "base tick unit in seconds -- defaults to this container's own TICK_SECONDS "
            f"env var (currently {TICK_SECONDS}s), which is also the sampling window the "
            "algorithms actually run on (backend degradation timing is a separate, "
            "independent config -- DEGRADATION_MEAN_DWELL_SECONDS -- since AGENT.md's "
            "'degradation timing decoupled and randomized'). Only override this if you "
            "deliberately want this run's status-line/duration accounting to use a "
            "different unit than the sampling cadence underneath -- see AGENT.md."
        ),
    )
    # 40rps/path (~240rps aggregate across all six paths), changed from 500
    # on 2026-07-29, deliberately conservative to keep backend contention
    # off the table as a confound -- the parallel-run comparison that day
    # (500rps vs 50rps/path) found no evidence that higher rps was
    # distorting results, but 40rps/path was picked as the new default
    # anyway to stay comfortably low rather than lean on that headroom.
    # Also the base of that day's same-total-volume series (40rps/60min,
    # 80rps/30min, 160rps/15min, 240rps/10min -- all 144k requests/path),
    # where p99-vs-rr significance held at every point on that series, so
    # nothing about correctness depends on running faster than this. See
    # README.md, "Choosing --rps" for the full history of this default.
    parser.add_argument("--rps", type=float, default=40, help="requests per second, per algorithm path (default 40)")
    # Default 10 minutes, changed from required-no-default on 2026-07-29.
    # At the current --rps default (40/path), 10 minutes gives 24,000
    # requests/path -- comfortably under BOOTSTRAP_MAX_SAMPLE (150000) in
    # analysis/analyze.py, so the stats report always uses full, uncapped
    # data at these defaults. Also long enough to avoid the "unlucky
    # degradation schedule" effect confirmed the same day: a 5-minute run
    # only samples a handful of each backend's independent degradation
    # transitions, so its realized average condition (and even the
    # aco-vs-mc ranking, normally a near-tie) can swing noticeably by
    # chance in a way a 10+ minute run doesn't -- see EXPERIMENTS.md.
    parser.add_argument(
        "--duration",
        type=float,
        default=10,
        help=(
            "run duration in minutes (wall-clock, not ticks) -- internally converted to "
            "ceil(duration*60/tick) ticks, so the actual run covers at least the requested "
            "duration and may run slightly longer if it doesn't divide evenly into whole ticks "
            "(default 10)"
        ),
    )
    args = parser.parse_args()

    run(tick_seconds=args.tick, rps=args.rps, duration_minutes=args.duration)


if __name__ == "__main__":
    main()
