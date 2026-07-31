#!/usr/bin/env python3
"""Merges TTFB samples for the same algorithm across several independent
NGINX instances' runs into one combined distribution -- the analysis half
of the 3x identical NGINX instance topology (see EXPERIMENTS.md, "3x
identical NGINX instances / many-LB test").

Each instance runs its own ordinary traffic_generator.py invocation
(docker-compose.multi.yml), completely unaware of the other instances, and
produces its own runs.log/access logs in its own logs directory. This
script is the only place that treats several instances' logs as one
combined experiment: for each algorithm, it concatenates that algorithm's
TTFB samples across every instance's own run window, then runs the exact
same stats machinery analyze.py uses for a single run (reused directly, not
reimplemented -- see analyze.algo_result_from_records/write_stats_report).

Usage:
    # each --logs-dir defaults to that instance's own latest run:
    python3 analysis/merge_analyze.py \\
        --logs-dir ./logs-instance-1 --logs-dir ./logs-instance-2 --logs-dir ./logs-instance-3

    # pin an EXACT run per instance instead -- required once instances'
    # run_index counters have drifted apart (each instance's counter is
    # just "how many lines are in MY OWN runs.log", with zero cross-
    # instance coordination, so this happens the moment any instance is
    # ever run standalone, outside a coordinated multi-instance
    # invocation -- confirmed live 2026-07-30). --logs-dir-run takes the
    # directory and the run index as two separate values:
    python3 analysis/merge_analyze.py \\
        --logs-dir-run ./logs-instance-1 6 --logs-dir-run ./logs-instance-2 5 --logs-dir-run ./logs-instance-3 5

    # the two forms mix freely -- only the instances that actually need a
    # specific (non-latest) run need --logs-dir-run:
    python3 analysis/merge_analyze.py \\
        --logs-dir-run ./logs-instance-1 6 --logs-dir ./logs-instance-2 --logs-dir ./logs-instance-3

Deliberately produces the merged stats report only -- no merged PNG charts.
Each instance's own end-of-run analysis already produces its own charts;
whether a merged chart is worth building can be decided once the merged
numbers themselves show something worth visualizing.
"""
import argparse
import os
import random
import sys

import analyze
import log_reader


def build_merged_header(instance_runs):
    lines = [
        f"=== Merged TTFB statistical comparison across {len(instance_runs)} instances (ms) ===",
        f"control: {analyze.CONTROL_ALGO}",
    ]
    for logs_dir, run in instance_runs:
        lines.append(
            f"  instance {logs_dir}: run {run['run_index']}  "
            f"window {analyze.ms_to_iso(run['start_ts_ms'])} -> {analyze.ms_to_iso(run['end_ts_ms'])}  "
            f"rps={run['rps_per_path']}/path  contention={analyze.contention_summary(run)}"
        )
    return lines


def print_merge_summary(instance_runs, per_algo):
    print(f"\n=== Merged summary across {len(instance_runs)} instances ===")
    for logs_dir, run in instance_runs:
        print(f"  {logs_dir}: run {run['run_index']}, contention={analyze.contention_summary(run)}")

    header = f"{'algo':<{analyze.ALGO_LABEL_WIDTH}} {'merged requests':>16} {'mean TTFB(ms)':>14} {'p95 TTFB(ms)':>13}"
    print(header)
    print("-" * len(header))
    for algo, data in per_algo.items():
        n = len(data["records"])
        stats = data["stats"]
        mean_s = f"{stats['mean']:.1f}" if stats else "n/a"
        p95_s = f"{stats['p95']:.1f}" if stats else "n/a"
        print(f"{algo:<{analyze.ALGO_LABEL_WIDTH}} {n:>16} {mean_s:>14} {p95_s:>13}")

    # Weight-change counts stay per-instance, not merged -- 3 independent
    # pheromone/transition-matrix states don't have one combined "change
    # count" that means anything (see docstring/EXPERIMENTS.md).
    for algo in analyze.ADAPTIVE_ALGO_NAMES:
        parts = []
        for logs_dir, run in instance_runs:
            weight_rows = log_reader.read_weights_csv(logs_dir, algo, run["start_ts_ms"], run["end_ts_ms"])
            change_count = log_reader.count_weight_changes(weight_rows)
            sampling_windows = len(weight_rows) if weight_rows is not None else None
            parts.append(f"{logs_dir}: {change_count}/{sampling_windows} windows")
        print(f"  {algo} weight changes (per instance, not merged): " + "; ".join(parts))


def merge_analysis(logs_dir_runs, out_dir=None, window_seconds=None,
                    alpha=analyze.SIGNIFICANCE_ALPHA, n_resamples=analyze.BOOTSTRAP_RESAMPLES,
                    max_bootstrap_sample=analyze.BOOTSTRAP_MAX_SAMPLE):
    """logs_dir_runs: [(logs_dir, run_index_or_None), ...] -- run_index=None
    means that instance's own latest run (log_reader.select_run's default).
    Deliberately per-instance rather than one shared run_index applied to
    every directory: each instance's run_index counter is independent
    (just a count of its own runs.log lines), so nothing guarantees they
    stay in lockstep across instances once any of them is ever run
    standalone -- see module docstring."""
    logs_dirs = [d for d, _ in logs_dir_runs]
    if len(logs_dirs) < 2:
        raise log_reader.AnalysisError(
            "merge_analyze.py needs at least 2 --logs-dir/--logs-dir-run values -- for a single instance, use analyze.py instead"
        )

    instance_runs = []  # [(logs_dir, run), ...]
    ip_to_host_by_dir = {}
    for logs_dir, run_index in logs_dir_runs:
        runs = log_reader.read_runs_log(logs_dir)
        run = log_reader.select_run(runs, run_index=run_index)
        instance_runs.append((logs_dir, run))
        ip_to_host, _ = log_reader.load_ip_to_host_map(logs_dir)
        ip_to_host_by_dir[logs_dir] = ip_to_host

    # Assumed identical across instances (that's the whole point of
    # "identical") -- taken from the first instance's own run the same way
    # analyze.py defaults window_seconds from a single run's tick_seconds.
    window_seconds = window_seconds or instance_runs[0][1]["tick_seconds"]

    # Canonical bucketing anchor for the merged window. Each instance's own
    # parse_access_log call below still only returns records inside THAT
    # instance's own [start_ts_ms, end_ts_ms] -- widening the anchor here to
    # the earliest start / latest end across instances is safe, it just
    # means edge buckets may be sparser if instances didn't start in
    # perfect lockstep (see EXPERIMENTS.md on the concurrent-start
    # approach -- `&`-backgrounded docker exec calls, no dedicated
    # warm-up mechanism).
    merged_start_ts_ms = min(run["start_ts_ms"] for _, run in instance_runs)
    merged_end_ts_ms = max(run["end_ts_ms"] for _, run in instance_runs)

    algos = sorted(set().union(*(set(log_reader.discover_algos(d)) for d in logs_dirs)))

    per_algo = {}
    for algo in algos:
        merged_records = []
        for logs_dir, run in instance_runs:
            access_log_path = os.path.join(logs_dir, f"{algo}{log_reader.ACCESS_LOG_SUFFIX}")
            merged_records.extend(
                log_reader.parse_access_log(access_log_path, run["start_ts_ms"], run["end_ts_ms"], ip_to_host_by_dir[logs_dir])
            )
        per_algo[algo] = analyze.algo_result_from_records(merged_records, window_seconds, merged_start_ts_ms, merged_end_ts_ms)

    out_dir = out_dir or os.path.join(logs_dirs[0], "analysis")
    os.makedirs(out_dir, exist_ok=True)

    print_merge_summary(instance_runs, per_algo)
    analyze.print_warnings(per_algo)

    # Seeded from the merged window's own start, same reproducibility
    # rationale as analyze.py's single-run report.
    rng = random.Random(merged_start_ts_ms)
    merge_id = "-".join(str(run["run_index"]) for _, run in instance_runs)
    stats_report_path = analyze.write_stats_report(
        build_merged_header(instance_runs), f"merged_run{merge_id}_stats_report.txt", per_algo, out_dir, rng,
        alpha=alpha, n_resamples=n_resamples, max_bootstrap_sample=max_bootstrap_sample,
    )

    print(f"\nmerged stats report written to {stats_report_path}")
    return {"instance_runs": instance_runs, "per_algo": per_algo, "stats_report_path": stats_report_path}


def main():
    parser = argparse.ArgumentParser(description="nginx-lb-tests merged multi-instance analysis")
    parser.add_argument(
        "--logs-dir",
        action="append",
        default=[],
        help="one instance's logs directory, using that instance's own latest run -- repeat for every "
             "instance that doesn't need a specific (non-latest) run. See --logs-dir-run for pinning one.",
    )
    parser.add_argument(
        "--logs-dir-run",
        nargs=2,
        metavar=("LOGS_DIR", "RUN_INDEX"),
        action="append",
        default=[],
        help="one instance's logs directory plus an exact run index to use, instead of its latest -- repeat "
             "per instance that needs pinning. Needed once instances' run_index counters have drifted apart "
             "(each is independent -- just a count of that instance's own runs.log lines -- so nothing keeps "
             "them in lockstep once any instance is ever run standalone). Mixes freely with --logs-dir.",
    )
    parser.add_argument("--out-dir", default=None, help="defaults to the first --logs-dir/--logs-dir-run's ./analysis subdirectory")
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=None,
        help="analysis bucket size in seconds; defaults to the first instance's own tick_seconds (assumed identical across instances)",
    )
    parser.add_argument("--alpha", type=float, default=analyze.SIGNIFICANCE_ALPHA, help=f"significance threshold; default {analyze.SIGNIFICANCE_ALPHA}")
    parser.add_argument("--bootstrap-resamples", type=int, default=analyze.BOOTSTRAP_RESAMPLES, help=f"default {analyze.BOOTSTRAP_RESAMPLES}")
    parser.add_argument("--max-bootstrap-sample", type=int, default=analyze.BOOTSTRAP_MAX_SAMPLE, help=f"default {analyze.BOOTSTRAP_MAX_SAMPLE}")
    args = parser.parse_args()

    logs_dir_runs = [(d, None) for d in args.logs_dir]
    for logs_dir, run_index_str in args.logs_dir_run:
        try:
            logs_dir_runs.append((logs_dir, int(run_index_str)))
        except ValueError:
            print(f"error: --logs-dir-run run index must be an integer, got {run_index_str!r}", file=sys.stderr)
            sys.exit(1)

    if not logs_dir_runs:
        print("error: at least one --logs-dir or --logs-dir-run is required", file=sys.stderr)
        sys.exit(1)

    try:
        merge_analysis(
            logs_dir_runs=logs_dir_runs,
            out_dir=args.out_dir,
            window_seconds=args.window_seconds,
            alpha=args.alpha,
            n_resamples=args.bootstrap_resamples,
            max_bootstrap_sample=args.max_bootstrap_sample,
        )
    except log_reader.AnalysisError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
