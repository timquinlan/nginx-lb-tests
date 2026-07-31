#!/usr/bin/env python3
"""Phase 4 analysis: reads one experiment run's NGINX access logs (plus its
weight-history CSVs) and produces the deliverables described in the design
doc -- a per-algorithm summary table, a text TTFB comparison report against
the round-robin control, and PNG line charts for TTFB over time and
per-backend selection frequency, with a degradation overlay.

Runnable two ways -- see analysis/README.md for details:

  1. Automatically, in-container, via traffic_generator.py's end-of-run
     hook (no args needed -- analyzes the run that just finished).
  2. Manually, from the host or a container, against any run:
       python3 analysis/analyze.py --run 3
       python3 analysis/analyze.py --start-ts 1721923200123
       python3 analysis/analyze.py                      # most recent run
"""
import argparse
import datetime
import os
import random
import sys

# Named log_reader.py, not common.py: this module is imported in-process
# by controller/traffic_generator.py's end-of-run hook, which already has
# its own controller/common.py loaded under the bare name "common" --
# Python's import cache is keyed by module name regardless of sys.path
# order, so a second same-named module would silently resolve to whichever
# one was imported first instead of raising. See AGENT.md.
import log_reader as common

LOW_CHANGE_RATIO = 0.3  # see print_warnings()

# Round robin is the design doc's stated control -- static, unweighted,
# never touched by any algorithm module (see AGENT.md). The stats report
# compares every other discovered algorithm against it. Not derived from
# discover_algos() since "which one is the control" is a fact about the
# experiment design, not something inferable from which log files exist.
CONTROL_ALGO = "rr"

# Algorithms that adaptively compute weights from observed latency every
# sampling window (see AGENT.md). Everything else discover_algos() finds
# is a static, built-in NGINX selection mechanism -- rr, plus NGINX's own
# random/random-two/least_conn upstream-block directives (see
# controller/common.py's STATIC_ALGO_METHODS). Hardcoded here, same
# reasoning as CONTROL_ALGO above: which group an algorithm belongs to is
# a fact about the experiment design, not derivable from which log files
# happen to exist. Used by write_stats_report's "best built-in vs best
# adaptive" incremental comparison below.
ADAPTIVE_ALGO_NAMES = ("aco", "mc")

# Shared minimum width for every algo-name column in printed tables --
# must be >= the longest algo name + 1 space of padding ("leastconn" is 9
# chars today) or that row's label overflows its field with no compensating
# padding, silently shifting every subsequent column right relative to
# shorter-named rows in the same table (found live: leastconn/random2 rows
# misaligned against aco/mc/rr/random in the point-estimates table).
ALGO_LABEL_WIDTH = 10

# One knob, not two: SIGNIFICANCE_ALPHA is both the Mann-Whitney verdict
# threshold and (as 1 - alpha) the bootstrap CI's coverage level. Exposing
# separate --alpha/--ci flags would let them drift out of sync (e.g. a
# 95%-CI "excludes zero" verdict alongside a 0.10-alpha Mann-Whitney
# verdict, answering two subtly different questions at two different
# confidence levels with no indication that's what happened).
SIGNIFICANCE_ALPHA = 0.05

# Bootstrap resampling repeats an O(n log n) sort BOOTSTRAP_RESAMPLES times
# per comparison -- the one part of this report that doesn't stay cheap on
# its own at real-experiment scale (a real run is meant to be "hundreds of
# times longer" than a smoke test, i.e. potentially hundreds of times more
# log lines). BOOTSTRAP_MAX_SAMPLE caps how much data actually goes into
# that resampling, independent of the point estimates and Mann-Whitney
# test above (both of which run on the true full sample regardless of
# size -- see log_reader.capped_sample).
#
# Raised from 5000 to 150000 (2026-07-29) after directly comparing both on
# the same real-run data: at 5000 obs/side, p99 CIs were 37-45ms wide and
# one comparison (mc vs rr) missed significance outright; uncapped at
# ~144k obs/side (the full sample for a 144k-request run), the same
# comparisons came back 6-9ms wide with the identical point estimates and
# that missed case now significant. The 5000 cap wasn't "good enough,
# just faster" -- it was materially widening the reported uncertainty.
#
# Brought back down to 50000 (2026-07-30) as a deliberate middle ground,
# not a re-run of the 5000-vs-150000 comparison above -- 50000 obs/side is
# still 10x the sample size that produced 6-9ms CIs at 150000/uncapped, so
# CI width isn't expected to meaningfully widen from this change; it's
# purely a CPU-cost tradeoff (fewer than 150000 x 2000 resamples per
# comparison) for the common case of a 24000-request/path 10-minute run,
# where the full sample is already under 50000 and this cap doesn't even
# engage.
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_MAX_SAMPLE = 50000


def ms_to_iso(ts_ms):
    return datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def contention_summary(run):
    """Formats the backend-contention-cap fields traffic_generator.py's
    --contention flag writes into a run record (see EXPERIMENTS.md,
    "Backend contention / the many-LB problem"). .get()-based throughout --
    runs recorded before this flag existed have none of these keys, and
    should just read as "off" rather than raising."""
    level = run.get("contention_level", "off")
    limit = run.get("contention_limit_per_backend")
    if limit is None:
        return level
    rho = run.get("contention_rho_target")
    w_ms = run.get("contention_probe_w_ms")
    return f"{level} (limit={limit}/backend, rho_target={rho}, probed_w={w_ms}ms)"


def algo_result_from_records(records, window_seconds, start_ts_ms, end_ts_ms, change_count=None, sampling_windows=None):
    """The stats-computation half of what used to be one analyze_algo() --
    split out (see merge_analyze.py) so a multi-instance merged run can
    build the same per-algorithm result shape from records concatenated
    across several instances' logs, without duplicating this logic.
    change_count/sampling_windows are weights.csv-derived and stay
    per-instance in the merge case (see merge_analyze.py for why) -- callers
    with nothing meaningful to report there just leave them None."""
    header_times = [r["header_time_ms"] for r in records if r["header_time_ms"] is not None]

    buckets = common.bucket_by_window(records, window_seconds, start_ts_ms, end_ts_ms)
    window_means = {}
    for idx, recs in buckets.items():
        times = [r["header_time_ms"] for r in recs if r["header_time_ms"] is not None]
        if times:
            window_means[idx] = common.mean(times)

    best_window = min(window_means, key=window_means.get) if window_means else None
    worst_window = max(window_means, key=window_means.get) if window_means else None

    return {
        "records": records,
        "buckets": buckets,
        "window_means": window_means,
        "header_times": header_times,
        "stats": common.ttfb_stats(header_times),
        "best_window": best_window,
        "worst_window": worst_window,
        "change_count": change_count,
        "sampling_windows": sampling_windows,
    }


def analyze_algo(logs_dir, algo, start_ts_ms, end_ts_ms, window_seconds, ip_to_host):
    access_log_path = os.path.join(logs_dir, f"{algo}{common.ACCESS_LOG_SUFFIX}")
    records = common.parse_access_log(access_log_path, start_ts_ms, end_ts_ms, ip_to_host)
    weight_rows = common.read_weights_csv(logs_dir, algo, start_ts_ms, end_ts_ms)
    change_count = common.count_weight_changes(weight_rows)
    sampling_windows = len(weight_rows) if weight_rows is not None else None
    return algo_result_from_records(records, window_seconds, start_ts_ms, end_ts_ms, change_count, sampling_windows)


def degradation_offset_by_window(per_algo):
    """Pools records across every algorithm -- degradation is a property of
    the backend, not of whichever algorithm happened to route to it, so
    pooling gives one algorithm-agnostic view of the pool's overall
    condition at any point in the run, regardless of which (if any) access
    log has an rr-style baseline to read it from.

    Returns mean X-Degradation-Offset-Ms per window (signed: positive =
    pool net slower than baseline, negative = net faster) -- not a
    percentage. Under the old fixed 0/1/2 staircase, "% of requests in a
    non-zero state" was a meaningful summary; now that each backend
    independently drifts along a continuous signed scale (see AGENT.md,
    "backends share a baseline, drift independently"), the mean offset is
    the analogous continuous-valued summary."""
    combined = {}
    for data in per_algo.values():
        for idx, recs in data["buckets"].items():
            combined.setdefault(idx, []).extend(recs)
    means = {}
    for idx, recs in combined.items():
        known = [r["degradation_offset_ms"] for r in recs if r["degradation_offset_ms"] is not None]
        if known:
            means[idx] = sum(known) / len(known)
    return means


def format_window(idx, window_means, window_seconds):
    if idx is None:
        return "n/a"
    return f"+{idx * window_seconds:.0f}s ({window_means[idx]:.1f}ms)"


def print_summary(run, per_algo, window_seconds, window_seconds_overridden, ip_to_host_found, logs_dir):
    print(f"\n=== Run {run['run_index']} summary ===")
    print(f"window:  {ms_to_iso(run['start_ts_ms'])}  ->  {ms_to_iso(run['end_ts_ms'])}")
    print(
        f"config:  tick={run['tick_seconds']}s  rps={run['rps_per_path']}/path  "
        f"planned={run['planned_duration_ticks']} ticks  actual={run['actual_duration_s']}s  "
        f"contention={contention_summary(run)}  "
        f"interrupted={run.get('interrupted', False)}"
    )
    source = "--window-seconds override" if window_seconds_overridden else "this run's tick_seconds (see AGENT.md: assumes --tick matched the container's TICK_SECONDS)"
    print(f"analysis window size: {window_seconds}s ({source})")
    if not ip_to_host_found:
        print(
            f"NOTE: {common.IP_TO_HOST_FILENAME} not found in {logs_dir} -- "
            "backends below are labeled by raw ip:port instead of hostname.",
            file=sys.stderr,
        )

    header = f"{'algo':<{ALGO_LABEL_WIDTH}} {'requests':>9} {'mean TTFB(ms)':>14} {'p95 TTFB(ms)':>13} {'best window':>20} {'worst window':>20} {'config changes':>18}"
    print(header)
    print("-" * len(header))
    for algo, data in per_algo.items():
        n = len(data["records"])
        stats = data["stats"]
        mean_s = f"{stats['mean']:.1f}" if stats else "n/a"
        p95_s = f"{stats['p95']:.1f}" if stats else "n/a"
        best_s = format_window(data["best_window"], data["window_means"], window_seconds)
        worst_s = format_window(data["worst_window"], data["window_means"], window_seconds)
        changes_s = (
            "static (n/a)"
            if data["change_count"] is None
            else f"{data['change_count']}/{data['sampling_windows']} windows"
        )
        print(f"{algo:<{ALGO_LABEL_WIDTH}} {n:>9} {mean_s:>14} {p95_s:>13} {best_s:>20} {worst_s:>20} {changes_s:>18}")


def print_warnings(per_algo):
    """Design doc: 'Low count relative to total sampling windows signals
    early convergence -- user may need to increase backend variability.'"""
    for algo, data in per_algo.items():
        if data["change_count"] is None or not data["sampling_windows"]:
            continue
        ratio = data["change_count"] / data["sampling_windows"]
        if ratio < LOW_CHANGE_RATIO:
            print(
                f"WARNING: {algo} only changed weights in {data['change_count']}/{data['sampling_windows']} "
                f"sampling windows this run ({ratio:.0%}) -- possible early convergence. "
                "Consider more backend latency variability if this is unexpected.",
                file=sys.stderr,
            )


def format_stats_row(label, stats):
    if stats is None:
        return f"{label:<{ALGO_LABEL_WIDTH}} {'n/a':>8}" + "".join(f"{'n/a':>12}" for _ in common.STAT_NAMES)
    cells = "".join(f"{stats[col]:>12.1f}" for col in common.STAT_NAMES)
    return f"{label:<{ALGO_LABEL_WIDTH}} {stats['n']:>8}" + cells


def format_delta_line(stat_name, point_delta, control_value, ci_lo, ci_hi, alpha):
    """One stat's delta vs control: point estimate (full data), percent,
    the bootstrap CI, and a significance verdict from whether that CI
    excludes zero. Negative delta = faster than control (lower TTFB is
    better) -- called out in the report header rather than left for the
    reader to infer."""
    pct = (point_delta / control_value * 100) if control_value else float("nan")
    significant = not (ci_lo <= 0 <= ci_hi)
    verdict = f"significant (CI excludes 0)" if significant else f"not significant (CI includes 0)"
    return (
        f"  {stat_name:<8} {point_delta:>+9.1f}ms ({pct:>+6.1f}%)   "
        f"{1 - alpha:.0%} CI [{ci_lo:>+8.1f}, {ci_hi:>+8.1f}]   {verdict}"
    )


def rank_algos(per_algo, stat_name="mean"):
    """Every algorithm with TTFB data this run, best (lowest -- lower TTFB
    is better) to worst, by the given stat. Algorithms with no data
    (empty sample) are omitted entirely rather than ranked last with a
    placeholder -- there's nothing to rank."""
    ranked = [(algo, data) for algo, data in per_algo.items() if data["stats"] is not None]
    ranked.sort(key=lambda item: item[1]["stats"][stat_name])
    return ranked


def default_header_lines(run):
    """The single-run header write_stats_report used to build inline --
    pulled out so a multi-instance merged report (see merge_analyze.py) can
    supply its own header (listing all the source runs it merged) while
    reusing the rest of the report unchanged."""
    return [
        f"=== Run {run['run_index']} TTFB statistical comparison (ms) ===",
        f"control: {CONTROL_ALGO}",
        f"window:  {ms_to_iso(run['start_ts_ms'])}  ->  {ms_to_iso(run['end_ts_ms'])}",
        f"config:  tick={run['tick_seconds']}s  rps={run['rps_per_path']}/path  "
        f"planned={run['planned_duration_ticks']} ticks  actual={run['actual_duration_s']}s  "
        f"contention={contention_summary(run)}",
    ]


def write_stats_report(header_lines, out_filename, per_algo, out_dir, rng, alpha=SIGNIFICANCE_ALPHA,
                        n_resamples=BOOTSTRAP_RESAMPLES, max_bootstrap_sample=BOOTSTRAP_MAX_SAMPLE):
    """The overall statistical comparison report: TTFB mean/median/p90/p95/p99
    for every discovered algorithm (full data), then, for each non-control
    algorithm vs the round-robin control (CONTROL_ALGO):

      - a Mann-Whitney U p-value on the full TTFB samples -- "is the
        overall distribution different at all"
      - a bootstrap confidence interval on each stat's delta, capped to
        max_bootstrap_sample observations/side for tractable compute time
        on a real (much longer) experiment -- "is *this specific stat's*
        difference likely non-zero"

    These answer related but distinct questions; both are labeled as such
    rather than collapsed into one "significant"/"not" verdict. Text, not
    a chart -- meant to be the one artifact that states a run's headline
    numbers plainly, without eyeballing a plot or running your own stats.

    header_lines/out_filename are caller-supplied (see default_header_lines)
    rather than derived from a single `run` dict here, so this same report
    body works for both a single run and a merge across several instances'
    runs (see merge_analyze.py) -- everything below the header only ever
    touches per_algo, never run-record fields directly.
    """
    lines = list(header_lines)
    lines.append("")

    lines.append("--- point estimates (full data) ---")
    header = f"{'algo':<{ALGO_LABEL_WIDTH}} {'n':>8}" + "".join(f"{col:>12}" for col in common.STAT_NAMES)
    lines.append(header)
    lines.append("-" * len(header))
    for algo, data in per_algo.items():
        lines.append(format_stats_row(algo, data["stats"]))
    lines.append("")

    ranked = rank_algos(per_algo, "mean")
    lines.append("--- ranked best to worst (by mean TTFB, lower is better) ---")
    if not ranked:
        lines.append("n/a -- no algorithm produced any TTFB data this run")
    else:
        for i, (algo, data) in enumerate(ranked, start=1):
            stats = data["stats"]
            tag = " [control]" if algo == CONTROL_ALGO else (" [adaptive]" if algo in ADAPTIVE_ALGO_NAMES else " [built-in]")
            lines.append(
                f"  {i}. {algo:<{ALGO_LABEL_WIDTH}} mean={stats['mean']:>8.1f}ms  median={stats['median']:>8.1f}ms  "
                f"p95={stats['p95']:>8.1f}ms{tag}"
            )
    lines.append("")

    # --- incremental: best built-in NGINX mechanism vs best adaptive algorithm ---
    # Once there are multiple static/built-in candidates (rr, random,
    # random2, leastconn) and multiple adaptive candidates (aco, mc), the
    # fixed rr-vs-everyone comparison below doesn't say which of each
    # GROUP actually won -- it only ever measures each algorithm against
    # one specific static baseline. This compares the strongest of each
    # group head-to-head instead. Independent of whether rr itself has
    # data this run (see the control-missing branch below), since rr is
    # just one of several possible "best built-in" candidates here, not a
    # prerequisite for this comparison.
    lines.append("--- best built-in NGINX mechanism vs best adaptive algorithm ---")
    builtin_ranked = [item for item in ranked if item[0] not in ADAPTIVE_ALGO_NAMES]
    adaptive_ranked = [item for item in ranked if item[0] in ADAPTIVE_ALGO_NAMES]
    if not builtin_ranked or not adaptive_ranked:
        lines.append("n/a -- need at least one built-in and one adaptive algorithm with TTFB data this run")
    else:
        best_builtin_algo, best_builtin_data = builtin_ranked[0]
        best_adaptive_algo, best_adaptive_data = adaptive_ranked[0]
        lines.append(f"best built-in: {best_builtin_algo}  (mean={best_builtin_data['stats']['mean']:.1f}ms)")
        lines.append(f"best adaptive: {best_adaptive_algo}  (mean={best_adaptive_data['stats']['mean']:.1f}ms)")

        builtin_sample = best_builtin_data["header_times"]
        adaptive_sample = best_adaptive_data["header_times"]
        u_stat, p_value = common.mann_whitney_u(adaptive_sample, builtin_sample)
        if p_value is None:
            lines.append("Mann-Whitney U: n/a (empty sample)")
        else:
            verdict = "statistically significant" if p_value < alpha else "not statistically significant"
            lines.append(
                f"Mann-Whitney U p-value: {p_value:.4g}  -> {verdict} at alpha={alpha} "
                f"(whole TTFB distribution, {best_adaptive_algo} vs {best_builtin_algo})"
            )

        builtin_capped = common.capped_sample(builtin_sample, max_bootstrap_sample, rng)
        adaptive_capped = common.capped_sample(adaptive_sample, max_bootstrap_sample, rng)
        if len(builtin_capped) < len(builtin_sample) or len(adaptive_capped) < len(adaptive_sample):
            lines.append(f"  (bootstrap below uses a random {max_bootstrap_sample}-observation subsample per side; point deltas are still full-data)")
        cis = common.bootstrap_delta_cis(adaptive_capped, builtin_capped, rng, n_resamples=n_resamples, ci=1 - alpha)
        for stat_name in common.STAT_NAMES:
            point_delta = best_adaptive_data["stats"][stat_name] - best_builtin_data["stats"][stat_name]
            ci_lo, ci_hi = cis[stat_name]
            lines.append(format_delta_line(stat_name, point_delta, best_builtin_data["stats"][stat_name], ci_lo, ci_hi, alpha))
    lines.append("")

    control_data = per_algo.get(CONTROL_ALGO)
    if control_data is None or control_data["stats"] is None:
        lines.append(f"NOTE: no {CONTROL_ALGO!r} TTFB data found in this run -- skipping rr-control significance comparison.")
        path = os.path.join(out_dir, out_filename)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    control_sample = control_data["header_times"]
    control_capped = common.capped_sample(control_sample, max_bootstrap_sample, rng)
    lines.append(
        f"method: Mann-Whitney U on full samples for the overall-distribution p-value "
        f"(alpha={alpha}); {n_resamples}-resample bootstrap {1 - alpha:.0%} CI, capped at "
        f"{max_bootstrap_sample} obs/side, for each stat's delta (seeded from this run's "
        f"start_ts_ms, so re-running this report against the same run reproduces the same CIs)."
    )

    for algo, data in per_algo.items():
        if algo == CONTROL_ALGO or data["stats"] is None:
            continue
        sample = data["header_times"]
        lines.append("")
        lines.append(f"--- {algo} vs {CONTROL_ALGO} (control) ---  n: {algo}={len(sample)}, {CONTROL_ALGO}={len(control_sample)}")

        u_stat, p_value = common.mann_whitney_u(sample, control_sample)
        if p_value is None:
            lines.append("Mann-Whitney U: n/a (empty sample)")
        else:
            verdict = "statistically significant" if p_value < alpha else "not statistically significant"
            lines.append(f"Mann-Whitney U p-value: {p_value:.4g}  -> {verdict} at alpha={alpha} (whole TTFB distribution vs control)")

        algo_capped = common.capped_sample(sample, max_bootstrap_sample, rng)
        if len(algo_capped) < len(sample) or len(control_capped) < len(control_sample):
            lines.append(f"  (bootstrap below uses a random {max_bootstrap_sample}-observation subsample per side; point deltas are still full-data)")
        cis = common.bootstrap_delta_cis(algo_capped, control_capped, rng, n_resamples=n_resamples, ci=1 - alpha)

        for stat_name in common.STAT_NAMES:
            point_delta = data["stats"][stat_name] - control_data["stats"][stat_name]
            ci_lo, ci_hi = cis[stat_name]
            lines.append(format_delta_line(stat_name, point_delta, control_data["stats"][stat_name], ci_lo, ci_hi, alpha))

    path = os.path.join(out_dir, out_filename)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def plot_ttfb_over_time(per_algo, run, window_seconds, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax1 = plt.subplots(figsize=(10, 5))
    for algo, data in per_algo.items():
        if not data["window_means"]:
            continue
        idxs = sorted(data["window_means"])
        xs = [idx * window_seconds for idx in idxs]
        ys = [data["window_means"][idx] for idx in idxs]
        ax1.plot(xs, ys, label=algo)
    ax1.set_xlabel(f"seconds since run start (window={window_seconds:.0f}s)")
    ax1.set_ylabel("mean TTFB (ms)")
    ax1.set_title(f"Run {run['run_index']}: TTFB over time")
    ax1.legend(loc="upper left")

    degradation = degradation_offset_by_window(per_algo)
    if degradation:
        ax2 = ax1.twinx()
        idxs = sorted(degradation)
        xs = [idx * window_seconds for idx in idxs]
        ys = [degradation[idx] for idx in idxs]
        ax2.fill_between(xs, ys, 0, step="mid", alpha=0.15, color="red")
        ax2.axhline(0, color="red", linewidth=0.5, alpha=0.4)
        ax2.set_ylabel("mean degradation offset, pool-wide (ms; + slower, - faster)")

    path = os.path.join(out_dir, f"run{run['run_index']}_ttfb_over_time.png")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def max_selection_frequency(per_algo):
    """Highest per-window, per-backend request count across every
    algorithm in the run -- used so all of a run's selection-frequency
    charts share one y-axis (see plot_selection_frequency). Without this,
    rr's chart (tightly clustered around its equal share, e.g. 495-515)
    auto-scales to its own narrow range and reads as far noisier/more
    volatile than aco/mc's charts (which legitimately span a much wider
    range, e.g. 0-600) purely because of independent axis scaling -- an
    apples-to-oranges visual comparison, not a real difference."""
    peak = 0
    for data in per_algo.values():
        for recs in data["buckets"].values():
            counts = {}
            for r in recs:
                counts[r["host"]] = counts.get(r["host"], 0) + 1
            if counts:
                peak = max(peak, max(counts.values()))
    return peak


def plot_selection_frequency(algo, data, run, window_seconds, out_dir, y_max):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    buckets = data["buckets"]
    hosts = sorted({r["host"] for recs in buckets.values() for r in recs})
    idxs = sorted(buckets)

    fig, ax = plt.subplots(figsize=(10, 5))
    for host in hosts:
        xs = [idx * window_seconds for idx in idxs]
        ys = [sum(1 for r in buckets[idx] if r["host"] == host) for idx in idxs]
        ax.plot(xs, ys, label=host)
    ax.set_xlabel(f"seconds since run start (window={window_seconds:.0f}s)")
    ax.set_ylabel("requests routed to backend, per window")
    ax.set_title(f"Run {run['run_index']}: {algo} backend selection frequency")
    ax.set_ylim(0, y_max * 1.05 if y_max else None)  # 5% headroom so a peak isn't clipped by the axis edge
    ax.legend(loc="upper left", fontsize="small")

    path = os.path.join(out_dir, f"run{run['run_index']}_{algo}_selection_frequency.png")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def run_analysis(logs_dir=None, out_dir=None, run_index=None, start_ts_ms=None, window_seconds=None,
                  alpha=SIGNIFICANCE_ALPHA, n_resamples=BOOTSTRAP_RESAMPLES, max_bootstrap_sample=BOOTSTRAP_MAX_SAMPLE):
    """The single entry point used both by analyze.py's own CLI and by
    traffic_generator.py's end-of-run hook (in-process import -- see
    AGENT.md for why this isn't a subprocess call)."""
    logs_dir = logs_dir or common.DEFAULT_LOGS_DIR
    runs = common.read_runs_log(logs_dir)
    run = common.select_run(runs, run_index=run_index, start_ts_ms=start_ts_ms)

    window_seconds_overridden = window_seconds is not None
    window_seconds = window_seconds or run["tick_seconds"]

    out_dir = out_dir or os.path.join(logs_dir, "analysis")
    os.makedirs(out_dir, exist_ok=True)

    ip_to_host, ip_to_host_found = common.load_ip_to_host_map(logs_dir)
    algos = common.discover_algos(logs_dir)

    per_algo = {
        algo: analyze_algo(logs_dir, algo, run["start_ts_ms"], run["end_ts_ms"], window_seconds, ip_to_host)
        for algo in algos
    }

    print_summary(run, per_algo, window_seconds, window_seconds_overridden, ip_to_host_found, logs_dir)
    print_warnings(per_algo)

    # Seeded from the run's own start_ts_ms, not left to the default clock
    # seed -- so bootstrap CIs reported for a given run are reproducible if
    # you regenerate the report later, rather than jittering run to run for
    # no reason a reader could tell apart from a real change in the data.
    rng = random.Random(run["start_ts_ms"])
    stats_report_path = write_stats_report(
        default_header_lines(run), f"run{run['run_index']}_stats_report.txt", per_algo, out_dir, rng,
        alpha=alpha, n_resamples=n_resamples, max_bootstrap_sample=max_bootstrap_sample
    )

    chart_paths = [plot_ttfb_over_time(per_algo, run, window_seconds, out_dir)]
    selection_freq_y_max = max_selection_frequency(per_algo)
    for algo in algos:
        chart_paths.append(plot_selection_frequency(algo, per_algo[algo], run, window_seconds, out_dir, selection_freq_y_max))

    print(f"\nstats report written to {stats_report_path}")
    print(f"charts written to {out_dir}:")
    for path in chart_paths:
        print(f"  {path}")

    return {"run": run, "per_algo": per_algo, "stats_report_path": stats_report_path, "chart_paths": chart_paths}


def main():
    parser = argparse.ArgumentParser(description="nginx-lb-tests Phase 4 analysis")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--run", type=int, help="run index to analyze (see runs.log). Default: most recent run")
    selector.add_argument("--start-ts", type=int, help="analyze the run with this exact start_ts_ms instead of --run")
    parser.add_argument("--logs-dir", default=None, help=f"defaults to {common.DEFAULT_LOGS_DIR!r} (env LOG_DIR, or ./logs)")
    parser.add_argument("--out-dir", default=None, help="defaults to <logs-dir>/analysis")
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=None,
        help="analysis bucket size in seconds; defaults to the run's own --tick value (assumed to match the sampling window -- see AGENT.md)",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=SIGNIFICANCE_ALPHA,
        help=f"significance threshold for the Mann-Whitney verdict, and (as 1-alpha) the bootstrap CI's coverage; default {SIGNIFICANCE_ALPHA}",
    )
    parser.add_argument(
        "--bootstrap-resamples",
        type=int,
        default=BOOTSTRAP_RESAMPLES,
        help=f"number of bootstrap resamples per algorithm-vs-control comparison; default {BOOTSTRAP_RESAMPLES}",
    )
    parser.add_argument(
        "--max-bootstrap-sample",
        type=int,
        default=BOOTSTRAP_MAX_SAMPLE,
        help=(
            f"cap on observations/side used for bootstrap resampling only (point estimates and the "
            f"Mann-Whitney test always use the full sample); default {BOOTSTRAP_MAX_SAMPLE}. A 5000-obs "
            "cap measurably widened CIs on a real run (37-45ms-wide p99 CIs, one missed-significance "
            "case) versus 150000/uncapped (6-9ms-wide, identical point estimates, that case significant) "
            "-- this default is a middle ground still 10x the sample size that produced the tight CIs, "
            "traded for less CPU per report than 150000 costs (~10-11 min for a full report at that "
            "cap on a ~144k-request run). Lower this further for an even faster/looser report."
        ),
    )
    args = parser.parse_args()

    try:
        run_analysis(
            logs_dir=args.logs_dir,
            out_dir=args.out_dir,
            run_index=args.run,
            start_ts_ms=args.start_ts,
            window_seconds=args.window_seconds,
            alpha=args.alpha,
            n_resamples=args.bootstrap_resamples,
            max_bootstrap_sample=args.max_bootstrap_sample,
        )
    except common.AnalysisError as e:
        print(f"analyze.py: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
