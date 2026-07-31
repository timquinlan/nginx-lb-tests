#!/usr/bin/env python3
"""Control script for the multi-instance NGINX topology
(docker-compose.multi.yml) -- see EXPERIMENTS.md, "3x identical NGINX
instances / many-LB test". Runs on the host, not inside any container
(it shells out to `docker compose`/`docker exec` itself).

Replaces the manual "N backgrounded docker exec calls + wait + merge
analysis" workflow with one command:

    python3 run_experiment.py run --rps 13 --duration 10 --contention mild
    python3 run_experiment.py status
    python3 run_experiment.py analyze [--run N --instances N]
    python3 run_experiment.py purge

All 3 controller instances are always deployed together
(`docker compose -f docker-compose.multi.yml up --build -d`, unchanged) --
`run --instances {1,2,3}` chooses how many of them actually generate
traffic for a given experiment, always starting from controller-1 (the
CONTENTION_OWNER instance, so ownership needs no per-run bookkeeping: it's
included in every subset). `run` takes the same inputs as
traffic_generator.py itself (rps/tick/duration/contention), applied
identically to whichever instances are selected -- --rps is PER INSTANCE,
same meaning as traffic_generator.py's own --rps; the aggregate rps used
for --contention's Little's Law sizing is computed automatically
(rps * instance count) and passed only to the owner instance.

`analyze` (no args) finds "the same coordinated run" across instances by
matching start timestamps, not by assuming run_index counters stay in
lockstep -- they don't: each instance's run_index is just a count of its
own runs.log lines, with zero cross-instance coordination, so it drifts
apart the moment any instance is ever run standalone (confirmed live
2026-07-30). See find_coordinated_runs().
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading

COMPOSE_FILE = "docker-compose.multi.yml"
INSTANCES = ["controller-1", "controller-2", "controller-3"]
# Matches CONTENTION_OWNER=true in docker-compose.multi.yml -- only this
# instance is allowed to size/apply the backend concurrency cap; see
# traffic_generator.py's apply_contention_level docstring for why. Always
# included in INSTANCES[:N] for any N >= 1, so --instances needs no
# separate ownership bookkeeping.
OWNER_INSTANCE = "controller-1"
LOGS_DIRS = {f"controller-{i}": f"./logs-instance-{i}" for i in (1, 2, 3)}

# How close together two instances' run start times need to be to count as
# "the same coordinated run" -- generous enough to cover the owner
# instance's contention-probe startup delay (a few seconds at most) plus
# normal docker-exec launch jitter between threads, tight enough that two
# genuinely unrelated ad-hoc runs are very unlikely to land inside it by
# coincidence.
CLUSTER_TOLERANCE_S = 30


def resolve_container_id(service):
    result = subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "ps", "-q", service],
        capture_output=True, text=True,
    )
    container_id = result.stdout.strip()
    if not container_id:
        print(
            f"error: no running container for service {service!r} -- "
            f"is `docker compose -f {COMPOSE_FILE} up -d` running?",
            file=sys.stderr,
        )
        sys.exit(1)
    return container_id


def stream_prefixed(pipe, prefix, print_lock):
    """Reads `pipe` line by line and prints each one tagged with `prefix`,
    under `print_lock` so lines from concurrent threads don't interleave
    mid-line -- this is what makes it safe to just forward each instance's
    own docker exec stdout (validate_backends/tick-progress/run-finished
    lines, exactly what a single-instance invocation prints today) live to
    the terminal instead of building a separate progress mechanism."""
    for line in iter(pipe.readline, ""):
        with print_lock:
            print(f"[{prefix}] {line.rstrip()}", flush=True)
    pipe.close()


def run_instance(service, args, aggregate_rps, print_lock, results):
    container_id = resolve_container_id(service)
    cmd = [
        "docker", "exec", container_id, "python3", "traffic_generator.py",
        "--rps", str(args.rps), "--duration", str(args.duration),
    ]
    if args.tick is not None:
        cmd += ["--tick", str(args.tick)]
    if args.contention != "off":
        cmd += ["--contention", args.contention]
        if service == OWNER_INSTANCE:
            cmd += ["--contention-total-rps", str(aggregate_rps)]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    stream_prefixed(proc.stdout, service, print_lock)
    results[service] = proc.wait()


def run_analysis_for(logs_dir_runs):
    """logs_dir_runs: [(logs_dir, run_index_or_None), ...]. Dispatches to
    plain analyze.py for a single instance (nothing to merge) or
    merge_analyze.py for 2+, passing each instance's exact run index
    explicitly via --logs-dir-run rather than relying on merge_analyze.py's
    own latest-run default -- callers here always know exactly which run
    they mean (either just-finished, or matched by find_coordinated_runs),
    so there's no reason to leave room for drift between what this script
    resolved and what merge_analyze.py would resolve on its own."""
    if len(logs_dir_runs) == 1:
        logs_dir, run_index = logs_dir_runs[0]
        cmd = [sys.executable, os.path.join("analysis", "analyze.py"), "--logs-dir", logs_dir]
        if run_index is not None:
            cmd += ["--run", str(run_index)]
    else:
        cmd = [sys.executable, os.path.join("analysis", "merge_analyze.py")]
        for logs_dir, run_index in logs_dir_runs:
            if run_index is None:
                cmd += ["--logs-dir", logs_dir]
            else:
                cmd += ["--logs-dir-run", logs_dir, str(run_index)]
    subprocess.run(cmd, check=True)


def cmd_run(args):
    n = args.instances
    instances = INSTANCES[:n]
    aggregate_rps = args.rps * n
    print(f"Starting {n} instance(s) ({', '.join(instances)}): rps={args.rps}/instance "
          f"(aggregate={aggregate_rps}), duration={args.duration}min, contention={args.contention}\n", flush=True)

    print_lock = threading.Lock()
    results = {}
    threads = [
        threading.Thread(target=run_instance, args=(service, args, aggregate_rps, print_lock, results))
        for service in instances
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    failed = [s for s in instances if results.get(s) != 0]
    if failed:
        print(f"\nerror: {', '.join(failed)} exited non-zero -- not running analysis", file=sys.stderr)
        sys.exit(1)

    print(f"\nall {n} instance(s) finished.", flush=True)
    if not args.no_merge:
        run_analysis_for([(LOGS_DIRS[s], None) for s in instances])


def read_latest_run(logs_dir):
    path = os.path.join(logs_dir, "runs.log")
    if not os.path.exists(path):
        return None
    last = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                last = json.loads(line)
    return last


def find_active_traffic_generator(container_id):
    """Looks for a running traffic_generator.py process in `container_id`
    via `docker top ... -eo pid,etimes,args` -- etimes is ps's
    seconds-since-start format (cross-platform reliable, unlike parsing
    STIME's locale/format-dependent clock-or-date display), and args gives
    the full command line, including --duration, since that's never
    persisted anywhere a separate `status` invocation could otherwise read
    it from (the tick-by-tick progress line only ever goes to whoever
    invoked the original `docker exec`). Returns None if no such process is
    running."""
    result = subprocess.run(
        ["docker", "top", container_id, "-eo", "pid,etimes,args"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines()[1:]:  # skip the ps header row
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        _pid, etimes_str, command = parts
        if "traffic_generator.py" not in command:
            continue
        try:
            elapsed_s = int(etimes_str)
        except ValueError:
            continue
        return {"elapsed_s": elapsed_s, "duration_str": parse_cmd_flag(command, "--duration"), "command": command}
    return None


def parse_cmd_flag(command, flag):
    parts = command.split()
    if flag in parts:
        idx = parts.index(flag)
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def format_duration(seconds):
    seconds = max(0, int(round(seconds)))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def cmd_status(args):
    for service in INSTANCES:
        container_id = resolve_container_id(service)
        active = find_active_traffic_generator(container_id)

        if active is None:
            state = "idle"
        else:
            state = f"RUNNING (elapsed {format_duration(active['elapsed_s'])})"
            # --duration is minutes, wall-clock (same meaning as
            # traffic_generator.py's own flag) -- the actual planned run
            # rounds up to a whole number of ticks (see run()'s
            # duration_ticks math), so this estimate can be off by up to
            # one tick's worth of seconds, close enough for a progress
            # indicator.
            if active["duration_str"] is not None:
                try:
                    total_s = float(active["duration_str"]) * 60.0
                    remaining_s = total_s - active["elapsed_s"]
                    state += f", ~{format_duration(remaining_s)} remaining of {format_duration(total_s)} planned"
                except ValueError:
                    pass

        latest = read_latest_run(LOGS_DIRS[service])
        if latest is None:
            run_summary = "no completed runs yet"
        else:
            outcome = "interrupted" if latest.get("interrupted") else "clean"
            run_summary = (
                f"last completed: run {latest['run_index']} "
                f"(contention={latest.get('contention_level')}, {outcome})"
            )
        print(f"{service}: {state} -- {run_summary}")


def find_coordinated_runs():
    """For each of the 3 always-available instances, finds its own most
    recent run, then keeps only the ones whose start_ts_ms falls within
    CLUSTER_TOLERANCE_S of the single latest start_ts_ms among them.
    Instances NOT part of that most recent coordinated `run_experiment.py
    run` invocation -- e.g. excluded by a smaller --instances count, or
    never used at all -- have an older, unrelated latest run and are
    dropped automatically; no separate bookkeeping of which instances were
    used for which run is needed. Returns [(service, logs_dir, run_index), ...]
    for the qualifying instances, sorted by service name."""
    latest_by_instance = {}
    for service in INSTANCES:
        latest = read_latest_run(LOGS_DIRS[service])
        if latest is not None:
            latest_by_instance[service] = latest

    if not latest_by_instance:
        return []

    anchor_ts = max(run["start_ts_ms"] for run in latest_by_instance.values())
    qualifying = [
        (service, LOGS_DIRS[service], run["run_index"])
        for service, run in latest_by_instance.items()
        if abs(run["start_ts_ms"] - anchor_ts) <= CLUSTER_TOLERANCE_S * 1000
    ]
    qualifying.sort(key=lambda item: item[0])
    return qualifying


def cmd_analyze(args):
    if args.run is not None:
        n = args.instances or len(INSTANCES)
        instances = INSTANCES[:n]
        run_analysis_for([(LOGS_DIRS[s], args.run) for s in instances])
        return

    qualifying = find_coordinated_runs()
    if not qualifying:
        print(
            "error: no completed runs found on any instance -- run `run_experiment.py run` first, "
            "or pass --run (and --instances) to target a specific run explicitly.",
            file=sys.stderr,
        )
        sys.exit(1)

    qualified_services = {service for service, _, _ in qualifying}
    excluded = [s for s in INSTANCES if s not in qualified_services]
    if excluded:
        print(
            f"note: excluding {', '.join(excluded)} -- its latest run doesn't look like part of the same "
            f"coordinated run (started more than {CLUSTER_TOLERANCE_S}s apart from the others). Pass "
            "--run/--instances explicitly if that's wrong."
        )

    run_analysis_for([(logs_dir, run_index) for _, logs_dir, run_index in qualifying])


def cmd_purge(args):
    # Refuse if anything is actively running -- purging out from under a
    # live run would corrupt it (e.g. the in-progress run's own eventual
    # write_run_record() call landing in a runs.log that no longer has the
    # lines its own next_run_index() counted at startup).
    for service in INSTANCES:
        container_id = resolve_container_id(service)
        if find_active_traffic_generator(container_id) is not None:
            print(
                f"error: {service} is currently running a traffic_generator.py invocation -- "
                "wait for it to finish (or stop it) before purging logs out from under it.",
                file=sys.stderr,
            )
            sys.exit(1)

    targets = ["./logs"] + list(LOGS_DIRS.values())
    print("This will permanently delete everything inside:")
    for t in targets:
        print(f"  {t}/")
    print("The next run on any instance starts over at run 1.")

    if not args.yes:
        answer = input("Type 'yes' to proceed: ").strip().lower()
        if answer != "yes":
            print("Aborted, nothing deleted.")
            return

    for target in targets:
        if not os.path.isdir(target):
            continue
        for name in os.listdir(target):
            path = os.path.join(target, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
    print("Purged.")


def main():
    parser = argparse.ArgumentParser(description="Control script for the multi-instance NGINX topology (docker-compose.multi.yml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="kick off a coordinated traffic_generator.py run across the chosen number of instances, live-streamed, then analyze the results")
    p_run.add_argument("--rps", type=float, default=40, help="requests per second, per algorithm path, PER INSTANCE (same default as traffic_generator.py -- aggregate across all selected instances is --instances x this)")
    p_run.add_argument("--duration", type=float, default=10, help="run duration in minutes, same meaning as traffic_generator.py's --duration (default 10)")
    p_run.add_argument("--tick", type=float, default=None, help="passed through to every instance's --tick if given; omitted means each instance uses its own container default")
    p_run.add_argument(
        "--contention", choices=["off", "mild", "moderate", "heavy"], default="off",
        help="passed to every selected instance; the aggregate rps for Little's Law sizing (rps x instance count) is computed automatically and applied only on the owner instance (controller-1) -- see EXPERIMENTS.md",
    )
    p_run.add_argument("--instances", type=int, choices=[1, 2, 3], default=3, help="how many of the 3 always-deployed instances to actually run traffic through, starting from controller-1 (default 3)")
    p_run.add_argument("--no-merge", action="store_true", help="skip running the (merged) analysis automatically once the selected instances finish")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="check whether each of the 3 instances is currently mid-run, and its most recently completed run")
    p_status.set_defaults(func=cmd_status)

    p_analyze = sub.add_parser("analyze", help="analyze the most recent coordinated run (auto-detected by matching start timestamps across instances), or a specific past run")
    p_analyze.add_argument("--run", type=int, default=None, help="specific run index to analyze instead of auto-detecting the latest coordinated run -- applied to the first --instances instances (default all 3)")
    p_analyze.add_argument("--instances", type=int, choices=[1, 2, 3], default=None, help="how many instances --run applies to (only meaningful together with --run); defaults to all 3")
    p_analyze.set_defaults(func=cmd_analyze)

    p_purge = sub.add_parser("purge", help="delete all logs across every instance, for a clean restart at run 1 -- refuses if any instance is currently running")
    p_purge.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    p_purge.set_defaults(func=cmd_purge)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
