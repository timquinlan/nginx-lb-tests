#!/usr/bin/env python3
"""Control script for the single NGINX instance in docker-compose.full.yml.
Runs on the host, not inside any container (it shells out to `docker
compose`/`docker exec` itself).

Replaces the manual "docker exec traffic_generator.py ... then separately
run analyze.py" workflow with one command:

    python3 run_experiment.py run --rps 40 --duration 10
    python3 run_experiment.py status
    python3 run_experiment.py analyze [--run N]
    python3 run_experiment.py purge
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

COMPOSE_FILE = "docker-compose.full.yml"
SERVICE = "controller"
LOGS_DIR = "./logs"


def resolve_container_id():
    result = subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "ps", "-q", SERVICE],
        capture_output=True, text=True,
    )
    container_id = result.stdout.strip()
    if not container_id:
        print(
            f"error: no running container for service {SERVICE!r} -- "
            f"is `docker compose -f {COMPOSE_FILE} up -d` running?",
            file=sys.stderr,
        )
        sys.exit(1)
    return container_id


def cmd_run(args):
    container_id = resolve_container_id()
    cmd = [
        "docker", "exec", container_id, "python3", "traffic_generator.py",
        "--rps", str(args.rps), "--duration", str(args.duration),
    ]
    if args.tick is not None:
        cmd += ["--tick", str(args.tick)]

    print(f"Starting run: rps={args.rps}, duration={args.duration}min\n", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in iter(proc.stdout.readline, ""):
        print(line.rstrip(), flush=True)
    proc.stdout.close()
    exit_code = proc.wait()

    if exit_code != 0:
        print(f"\nerror: traffic_generator.py exited {exit_code} -- not running analysis", file=sys.stderr)
        sys.exit(1)

    print("\nrun finished.", flush=True)


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
    container_id = resolve_container_id()
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

    latest = read_latest_run(LOGS_DIR)
    if latest is None:
        run_summary = "no completed runs yet"
    else:
        outcome = "interrupted" if latest.get("interrupted") else "clean"
        run_summary = f"last completed: run {latest['run_index']} ({outcome})"
    print(f"{SERVICE}: {state} -- {run_summary}")


def cmd_analyze(args):
    cmd = [sys.executable, os.path.join("analysis", "analyze.py"), "--logs-dir", LOGS_DIR]
    if args.run is not None:
        cmd += ["--run", str(args.run)]
    subprocess.run(cmd, check=True)


def cmd_purge(args):
    # Refuse if anything is actively running -- purging out from under a
    # live run would corrupt it (e.g. the in-progress run's own eventual
    # write_run_record() call landing in a runs.log that no longer has the
    # lines its own next_run_index() counted at startup).
    container_id = resolve_container_id()
    if find_active_traffic_generator(container_id) is not None:
        print(
            f"error: {SERVICE} is currently running a traffic_generator.py invocation -- "
            "wait for it to finish (or stop it) before purging logs out from under it.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("This will permanently delete everything inside:")
    print(f"  {LOGS_DIR}/")
    print("The next run starts over at run 1.")

    if not args.yes:
        answer = input("Type 'yes' to proceed: ").strip().lower()
        if answer != "yes":
            print("Aborted, nothing deleted.")
            return

    if os.path.isdir(LOGS_DIR):
        for name in os.listdir(LOGS_DIR):
            path = os.path.join(LOGS_DIR, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
    print("Purged.")


def main():
    parser = argparse.ArgumentParser(description="Control script for the single-instance NGINX topology (docker-compose.full.yml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="kick off a traffic_generator.py run, live-streamed, then analyze the results")
    p_run.add_argument("--rps", type=float, default=40, help="requests per second, per algorithm path (same default as traffic_generator.py)")
    p_run.add_argument("--duration", type=float, default=10, help="run duration in minutes, same meaning as traffic_generator.py's --duration (default 10)")
    p_run.add_argument("--tick", type=float, default=None, help="passed through to --tick if given; omitted means the container's own default")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="check whether the controller is currently mid-run, and its most recently completed run")
    p_status.set_defaults(func=cmd_status)

    p_analyze = sub.add_parser("analyze", help="analyze the most recent run, or a specific past run")
    p_analyze.add_argument("--run", type=int, default=None, help="specific run index to analyze instead of the latest")
    p_analyze.set_defaults(func=cmd_analyze)

    p_purge = sub.add_parser("purge", help="delete all logs, for a clean restart at run 1 -- refuses if currently running")
    p_purge.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    p_purge.set_defaults(func=cmd_purge)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
