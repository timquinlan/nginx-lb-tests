#!/usr/bin/env python3
"""Cross-run 'shape' charts: does the algorithm picture change as rps
changes, or is it flat? Groups every clean run in runs.log by rps_per_path,
recomputes each algorithm's mean TTFB per run straight from its access log
(same common.parse_access_log + common.mean path analyze.py itself uses,
not a re-parse of any run's text stats report -- so this stays correct even
if the report's formatting changes later), and produces two PNGs:

  1. shape_means_by_rps.png -- mean TTFB vs rps, one line per algorithm.
     Line value is the mean of that rps bucket's repeat runs; each repeat's
     own value is also plotted as a small, same-colored, semi-transparent
     dot, so a reader can see repeat-to-repeat noise alongside the trend
     line instead of it being silently averaged away.
  2. shape_deltas_by_rps.png -- two lines, aco_wrr-mc_wrr and aco_lc-mc_lc
     (mean TTFB delta, ms; negative = ACO faster), same mean-line +
     individual-repeat-dots treatment, with a zero reference line.

Bucketing is automatic (grouped by whatever rps_per_path values are
present, not hardcoded run indices or hardcoded rps values) so a
differently-sized or differently-valued future sweep still works -- see
AGENT.md, "Shape charts (analysis/shape_charts.py)" for how to point this
at a fresh sweep. Interrupted runs are always excluded; --run-min/--run-max
narrow the run_index range further if old, unrelated runs share the same
./logs directory.

Usage:
    python3 analysis/shape_charts.py [--logs-dir ./logs] [--out-dir ./logs/analysis]
    python3 analysis/shape_charts.py --run-min 1 --run-max 15
"""
import argparse
import os

import log_reader as common

DELTA_PAIRS = (("aco_wrr", "mc_wrr"), ("aco_lc", "mc_lc"))


def collect_run_means(logs_dir, run_min, run_max):
    """Returns {rps: {algo: [mean_ttfb_ms, ...]}} -- one value per clean,
    in-range run at that rps. ip_to_host is loaded once and reused across
    every run: all runs sharing one ./logs directory also share one
    backend pool/network, so the resolved ip:port -> host mapping doesn't
    change run to run (same assumption analyze.py's own single-run
    analysis already makes, just applied across runs here instead of
    within one)."""
    runs = common.read_runs_log(logs_dir)
    ip_to_host, _ = common.load_ip_to_host_map(logs_dir)
    algos = common.discover_algos(logs_dir)

    by_rps = {}
    for run in runs:
        if run["interrupted"]:
            continue
        if run_min is not None and run["run_index"] < run_min:
            continue
        if run_max is not None and run["run_index"] > run_max:
            continue
        rps = run["rps_per_path"]
        means = {}
        for algo in algos:
            path = os.path.join(logs_dir, f"{algo}{common.ACCESS_LOG_SUFFIX}")
            records = common.parse_access_log(path, run["start_ts_ms"], run["end_ts_ms"], ip_to_host)
            header_times = [r["header_time_ms"] for r in records if r["header_time_ms"] is not None]
            mean_ttfb = common.mean(header_times)
            if mean_ttfb is not None:
                means[algo] = mean_ttfb
        if means:
            bucket = by_rps.setdefault(rps, {algo: [] for algo in algos})
            for algo, val in means.items():
                bucket[algo].append(val)

    return by_rps


def plot_means_by_rps(by_rps, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rps_values = sorted(by_rps)
    algos = sorted({algo for bucket in by_rps.values() for algo in bucket})

    fig, ax = plt.subplots(figsize=(10, 6))
    for algo in algos:
        line_xs, line_ys = [], []
        dot_xs, dot_ys = [], []
        for rps in rps_values:
            vals = by_rps[rps].get(algo, [])
            if not vals:
                continue
            line_xs.append(rps)
            line_ys.append(sum(vals) / len(vals))
            dot_xs.extend([rps] * len(vals))
            dot_ys.extend(vals)
        line, = ax.plot(line_xs, line_ys, marker="o", label=algo)
        ax.scatter(dot_xs, dot_ys, color=line.get_color(), alpha=0.35, s=18, zorder=1)

    ax.set_xlabel("rps per path")
    ax.set_ylabel("mean TTFB (ms)")
    ax.set_title("Mean TTFB vs rps, per algorithm (line = mean of repeats, dots = individual repeats)")
    ax.legend(loc="best", fontsize="small")

    path = os.path.join(out_dir, "shape_means_by_rps.png")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def plot_deltas_by_rps(by_rps, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rps_values = sorted(by_rps)

    fig, ax = plt.subplots(figsize=(10, 6))
    for algo_a, algo_b in DELTA_PAIRS:
        line_xs, line_ys = [], []
        dot_xs, dot_ys = [], []
        for rps in rps_values:
            vals_a = by_rps[rps].get(algo_a, [])
            vals_b = by_rps[rps].get(algo_b, [])
            # Repeats are paired by position (same run_index order both
            # sides were appended in), not by value -- collect_run_means
            # appends in runs.log order for every algo alike, so index i
            # on each side always comes from the same run.
            deltas = [a - b for a, b in zip(vals_a, vals_b)]
            if not deltas:
                continue
            line_xs.append(rps)
            line_ys.append(sum(deltas) / len(deltas))
            dot_xs.extend([rps] * len(deltas))
            dot_ys.extend(deltas)
        line, = ax.plot(line_xs, line_ys, marker="o", label=f"{algo_a} - {algo_b}")
        ax.scatter(dot_xs, dot_ys, color=line.get_color(), alpha=0.35, s=18, zorder=1)

    ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
    ax.set_xlabel("rps per path")
    ax.set_ylabel("mean TTFB delta (ms; negative = first algorithm faster)")
    ax.set_title("ACO vs MC mean-TTFB delta vs rps (line = mean of repeats, dots = individual repeats)")
    ax.legend(loc="best", fontsize="small")

    path = os.path.join(out_dir, "shape_deltas_by_rps.png")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def main():
    parser = argparse.ArgumentParser(description="Cross-run 'shape across rps' charts")
    parser.add_argument("--logs-dir", default=None, help=f"defaults to {common.DEFAULT_LOGS_DIR!r} (env LOG_DIR, or ./logs)")
    parser.add_argument("--out-dir", default=None, help="defaults to <logs-dir>/analysis")
    parser.add_argument("--run-min", type=int, default=None, help="lowest run_index to include (default: no lower bound)")
    parser.add_argument("--run-max", type=int, default=None, help="highest run_index to include (default: no upper bound)")
    args = parser.parse_args()

    logs_dir = args.logs_dir or common.DEFAULT_LOGS_DIR
    out_dir = args.out_dir or os.path.join(logs_dir, "analysis")
    os.makedirs(out_dir, exist_ok=True)

    by_rps = collect_run_means(logs_dir, args.run_min, args.run_max)
    if not by_rps:
        raise common.AnalysisError("no clean runs found in the given range -- nothing to chart")

    means_path = plot_means_by_rps(by_rps, out_dir)
    deltas_path = plot_deltas_by_rps(by_rps, out_dir)

    rps_summary = ", ".join(f"{rps:g}rps({len(next(iter(by_rps[rps].values())))})" for rps in sorted(by_rps))
    print(f"rps buckets used: {rps_summary}")
    print(f"charts written to {out_dir}:")
    print(f"  {means_path}")
    print(f"  {deltas_path}")


if __name__ == "__main__":
    try:
        main()
    except common.AnalysisError as e:
        raise SystemExit(f"error: {e}")
