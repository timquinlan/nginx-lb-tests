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
    read_upstream_hosts,
    NGINX_EXPERIMENT_PORT,
    BACKEND_PORT,
    RUNS_LOG_PATH,
    LOG_DIR,
    DYNAMIC_ALGO_NAMES,
    TICK_SECONDS,
    DEPLOY_MODE,
)
import config_writer
import sampler
import validate_backends

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

# Contention-cap presets (see EXPERIMENTS.md, "Backend contention / the
# many-LB problem"). Deliberately mild-to-heavy, not unstable -- the
# leastconn/aco/mc means are already close, so a little shared-backend
# pressure should be enough to separate them without risking an unstable
# queue (rho must stay < 1; see the math in EXPERIMENTS.md for why "off"
# is None, not just a very high number -- None means the backend's
# capacity gate stays fully unbounded, identical to today's behavior).
# "heavy" (rho=0.95) is deliberately still < 1, not a step towards the
# unstable/burst-pause territory EXPERIMENTS.md describes separately --
# queueing delay grows sharply as rho->1 even while staying stable, so
# this is already a meaningfully harder squeeze than "moderate" without
# risking the runaway-backlog failure mode.
CONTENTION_RHO_TARGETS = {
    "off": None,
    "mild": 0.8,
    "moderate": 0.9,
    "heavy": 0.95,
}
CAPACITY_PROBE_TIMEOUT_SECONDS = 5
# A single probe/backend was noisy enough to occasionally collide two
# different levels onto the same computed N -- found 2026-07-29: mild
# (rho=0.8) and moderate (rho=0.9) landed on the identical N=23 because
# moderate's one probe happened to catch a slower moment (424.7ms vs
# mild's 374.9ms), and the two effects canceled out. Averaging several
# probes/backend smooths out that per-request latency noise (each probe
# draws its own independent base-latency sample even within the same
# degradation-offset state) so the mild/moderate/heavy ladder reliably
# reflects its intended rho targets instead of occasionally coinciding.
CAPACITY_PROBE_ROUNDS = 3


def _post_capacity(host, limit):
    """Direct-to-backend admin call (bypasses NGINX, same as sampler.py's
    direct_probe) -- sets backend `host`'s concurrency cap. limit=None
    means unlimited. Best-effort: a single unreachable backend shouldn't
    abort the whole run, but it does mean that backend won't have the
    intended cap -- logged loudly so it isn't silently wrong."""
    body = json.dumps({"limit": limit}).encode("utf-8")
    try:
        conn = http.client.HTTPConnection(host, BACKEND_PORT, timeout=CAPACITY_PROBE_TIMEOUT_SECONDS)
        try:
            conn.request("POST", "/admin/capacity", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            if resp.status != 200:
                log("traffic_generator", f"WARNING: {host} rejected capacity={limit} (HTTP {resp.status})")
        finally:
            conn.close()
    except OSError as e:
        log("traffic_generator", f"WARNING: couldn't set capacity={limit} on {host}: {e}")


def _get_capacity_stats(host):
    """GET counterpart to _post_capacity -- fetches AdjustableLimiter.stats()
    from `host` (limit, total acquires, blocked acquires, blocked_pct) since
    that backend's cap was last set. Best-effort, same failure handling as
    _post_capacity: an unreachable backend just logs a warning and yields no
    stats for that host, rather than aborting record-writing for the whole
    run."""
    try:
        conn = http.client.HTTPConnection(host, BACKEND_PORT, timeout=CAPACITY_PROBE_TIMEOUT_SECONDS)
        try:
            conn.request("GET", "/admin/capacity")
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                log("traffic_generator", f"WARNING: {host} capacity-stats fetch returned HTTP {resp.status}")
                return None
            return json.loads(body)
        finally:
            conn.close()
    except OSError as e:
        log("traffic_generator", f"WARNING: couldn't fetch capacity stats from {host}: {e}")
        return None


def collect_contention_stats(hosts):
    """Aggregates per-backend limiter stats into an overall blocked-request
    percentage plus the raw per-backend breakdown, for the run record. Only
    meaningful in DEPLOY_MODE=full (the only mode with /admin/capacity --
    see apply_contention_level) -- callers should skip this entirely against
    external backends rather than generating a page of 404 warnings for a
    field that will just be null anyway."""
    by_backend = {}
    total_acquires = 0
    total_blocked = 0
    for host in hosts:
        stats = _get_capacity_stats(host)
        if stats is None:
            continue
        by_backend[host] = stats
        total_acquires += stats.get("total", 0)
        total_blocked += stats.get("blocked", 0)

    if not by_backend:
        return None, None

    overall_pct = round((total_blocked / total_acquires * 100.0) if total_acquires else 0.0, 2)
    return overall_pct, by_backend


def apply_contention_level(hosts, paths, rps, level):
    """Resets every backend to unlimited, then (unless level is "off")
    probes current per-backend latency directly, sizes a concurrency cap
    via Little's Law at the requested utilization target, and pushes it to
    every backend. Always resets to unlimited first, even for "off" --
    otherwise a prior run's cap would silently carry over into a run that
    never asked for one. Returns (limit, rho_target, probe_w_ms) for the
    run record; limit/probe_w_ms are None when level is "off".

    The /admin/capacity endpoint only exists on this project's own
    simulated backends (backend/server.py) -- DEPLOY_MODE=external points
    at real hosts that don't have it. Found 2026-07-29: without this
    check, a real backend would just 404 the POST (logged as a warning,
    not fatal) and the run would proceed anyway, writing a run record that
    claims contention was applied when nothing was -- misleading, not just
    silently inert. So: "off" against external is a no-op (skip the
    reset-push entirely, no point 404ing on every single run regardless of
    whether contention was ever requested); anything else against
    external refuses to start the run at all, same fail-loud philosophy as
    validate_backends.py rather than quietly downgrading to "off"."""
    if DEPLOY_MODE != "full":
        if level == "off":
            return None, None, None
        log(
            "traffic_generator",
            f"ERROR: --contention {level} requires DEPLOY_MODE=full (this project's own simulated "
            f"backends, which have the /admin/capacity endpoint) -- current DEPLOY_MODE={DEPLOY_MODE!r} "
            "points at real external hosts that don't support it. Refusing to start a run that would "
            "silently record contention settings that were never actually applied. Use --contention off "
            "(or omit the flag) against external backends.",
        )
        sys.exit(1)

    for host in hosts:
        _post_capacity(host, None)

    rho_target = CONTENTION_RHO_TARGETS[level]
    if rho_target is None:
        return None, None, None

    # CAPACITY_PROBE_ROUNDS direct probes per backend (not just one), right
    # after the unlimited reset above, so this reflects real current
    # backend conditions (base latency + whatever degradation offset is
    # active right now) unconstrained by any leftover cap from a previous
    # run -- same probe mechanism sampler.py's priming step uses, just
    # repeated and averaged for a more stable W estimate (see
    # CAPACITY_PROBE_ROUNDS' comment for why one probe wasn't enough).
    probe_latencies_ms = [
        sampler.direct_probe(host) for host in hosts for _ in range(CAPACITY_PROBE_ROUNDS)
    ]
    avg_w_ms = sum(probe_latencies_ms) / len(probe_latencies_ms)

    lambda_backend = (rps * len(paths)) / len(hosts)
    natural_concurrency_l = lambda_backend * (avg_w_ms / 1000.0)
    limit = max(1, math.ceil(natural_concurrency_l / rho_target))

    log(
        "traffic_generator",
        f"contention={level}: probed avg backend latency={avg_w_ms:.1f}ms "
        f"({CAPACITY_PROBE_ROUNDS} probes/backend x {len(hosts)} backends), "
        f"lambda_backend={lambda_backend:.1f}rps, natural concurrency L={natural_concurrency_l:.1f}, "
        f"rho_target={rho_target} -> capacity limit={limit}/backend",
    )
    for host in hosts:
        _post_capacity(host, limit)

    return limit, rho_target, round(avg_w_ms, 1)


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


def run(tick_seconds, rps, duration_minutes, contention):
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

    # Reuses validate_backends.py rather than re-implementing the check --
    # same module entrypoint.py and sampler.py already call at container
    # startup, just invoked again here since a backend can go unreachable
    # hours into a long-running container's life (a restart, a network
    # blip) and this run's own traffic/contention-probe phase deserves the
    # same loud, actionable failure a startup-time outage would get,
    # rather than silently generating a run's worth of broken data.
    hosts = read_upstream_hosts()
    unreachable = validate_backends.validate(hosts)
    if unreachable:
        validate_backends.print_failure_report(hosts, unreachable)
        sys.exit(1)

    contention_limit, contention_rho, contention_probe_w_ms = apply_contention_level(hosts, paths, rps, contention)

    run_index = next_run_index()
    total_duration_seconds = tick_seconds * duration_ticks
    start_ts_ms = now_ms()
    start_monotonic = time.monotonic()
    baseline_change_counts = current_change_counts()

    log("traffic_generator", f"run {run_index}: paths={paths} tick={tick_seconds}s rps={rps}/path "
        f"requested_duration={duration_minutes}min -> {duration_ticks} ticks ({total_duration_seconds}s actual) "
        f"contention={contention} (limit={contention_limit})")

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

    # Measures whether the contention cap actually bound anything this run,
    # rather than leaving that to be inferred from chart shapes -- see
    # EXPERIMENTS.md, "Backend contention / the many-LB problem". Only
    # DEPLOY_MODE=full backends expose /admin/capacity; skip entirely
    # against external backends (apply_contention_level already refused to
    # run there for any level other than "off", so there's nothing to
    # measure anyway).
    contention_blocked_pct = None
    contention_stats_by_backend = None
    if DEPLOY_MODE == "full":
        contention_blocked_pct, contention_stats_by_backend = collect_contention_stats(hosts)
        if contention_blocked_pct is not None:
            log("traffic_generator", f"run {run_index}: contention={contention} -- limiter blocked "
                f"{contention_blocked_pct}% of backend requests")

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
        "contention_level": contention,
        "contention_limit_per_backend": contention_limit,
        "contention_rho_target": contention_rho,
        "contention_probe_w_ms": contention_probe_w_ms,
        "contention_blocked_request_pct": contention_blocked_pct,
        "contention_stats_by_backend": contention_stats_by_backend,
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
    # requests/path -- comfortably under BOOTSTRAP_MAX_SAMPLE (50000) in
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
    parser.add_argument(
        "--contention",
        choices=sorted(CONTENTION_RHO_TARGETS.keys()),
        default="off",
        help=(
            "backend concurrency-cap level (default off, unlimited -- today's behavior, "
            "unchanged). 'mild'/'moderate'/'heavy' probe each backend's current latency directly, "
            "size a per-backend concurrency cap via Little's Law at a target utilization "
            "(rho=0.8/0.9/0.95) for this run's actual --rps, and push it to every backend before "
            "sending traffic -- see EXPERIMENTS.md, 'Backend contention / the many-LB problem'. "
            "FULL MODE (DEPLOY_MODE=full) ONLY -- the underlying /admin/capacity endpoint only "
            "exists on this project's own simulated backends; against DEPLOY_MODE=external (real "
            "hosts), anything other than 'off' refuses to start the run rather than silently "
            "recording contention settings that were never actually applied."
        ),
    )
    args = parser.parse_args()

    run(tick_seconds=args.tick, rps=args.rps, duration_minutes=args.duration, contention=args.contention)


if __name__ == "__main__":
    main()
