"""Shared parsing/aggregation helpers for the Phase 4 analysis tooling.

Deliberately self-contained (stdlib only, no import from controller/):
analysis/ is meant to be runnable two ways --

  1. Automatically, in-container, via traffic_generator.py's end-of-run
     hook (LOG_DIR is already set to /var/log/upstream-rl there).
  2. Manually, from the host machine, pointed straight at the bind-mounted
     ./logs directory (`python3 analysis/analyze.py`) -- see AGENT.md
     "Bind mounts, not named volumes" for why that property matters here.

Importing controller/common.py would work for case 1 but not case 2 (it
isn't on the host's import path, and some of its constants -- CONF_DIR,
HOSTS_FILE -- don't make sense outside the container). Keeping analysis/
independent also matches the project's "simple enough to adapt for a
different algorithm or log schema" goal: nothing here assumes anything
controller-specific beyond the on-disk log formats documented in
analysis/README.md.
"""
import csv
import glob
import json
import math
import os

# Matches controller/common.py's own default-from-env pattern (see its
# LOG_DIR / --tick precedent): inside the container LOG_DIR is already set
# to /var/log/upstream-rl, so the hook needs no extra wiring. Run from the
# host with no override, and this falls back to a path relative to the
# current working directory -- expected to be the repo root, where the
# bind-mounted ./logs directory lives.
DEFAULT_LOGS_DIR = os.environ.get("LOG_DIR", "./logs")

ACCESS_LOG_SUFFIX = ".access.log"
WEIGHTS_CSV_SUFFIX = ".weights.csv"
RUNS_LOG_FILENAME = "runs.log"
IP_TO_HOST_FILENAME = "ip_to_host.json"


class AnalysisError(Exception):
    """Raised for user-facing problems (bad run selector, missing files) --
    caught at the CLI boundary and printed without a traceback."""


# --- runs.log ----------------------------------------------------------------

def read_runs_log(logs_dir):
    path = os.path.join(logs_dir, RUNS_LOG_FILENAME)
    if not os.path.exists(path):
        raise AnalysisError(f"no {RUNS_LOG_FILENAME} found at {path} -- has a traffic_generator.py run happened yet?")
    runs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                runs.append(json.loads(line))
    if not runs:
        raise AnalysisError(f"{path} exists but contains no run records")
    return runs


def select_run(runs, run_index=None, start_ts_ms=None):
    """Run isolation by index or start timestamp (design doc requirement).
    Defaults to the most recent run when neither is given -- the common
    case right after a run finishes (the end-of-run hook calls this way)."""
    if run_index is not None and start_ts_ms is not None:
        raise AnalysisError("specify --run or --start-ts, not both")
    if run_index is not None:
        for run in runs:
            if run["run_index"] == run_index:
                return run
        available = sorted(r["run_index"] for r in runs)
        raise AnalysisError(f"no run with index {run_index} in runs.log -- available indices: {available}")
    if start_ts_ms is not None:
        for run in runs:
            if run["start_ts_ms"] == start_ts_ms:
                return run
        raise AnalysisError(f"no run with start_ts_ms {start_ts_ms} in runs.log")
    return max(runs, key=lambda r: r["run_index"])


# --- algorithm discovery ------------------------------------------------------

def discover_algos(logs_dir):
    """Algorithm names are derived from which *.access.log files exist,
    rather than a hardcoded ("rr", "aco", "mc") list -- adding a new
    algorithm's location block (which writes its own access log, per
    AGENT.md) makes it show up here automatically, no analysis code change
    needed."""
    pattern = os.path.join(logs_dir, f"*{ACCESS_LOG_SUFFIX}")
    algos = sorted(
        os.path.basename(p)[: -len(ACCESS_LOG_SUFFIX)] for p in glob.glob(pattern)
    )
    if not algos:
        raise AnalysisError(f"no *{ACCESS_LOG_SUFFIX} files found in {logs_dir}")
    return algos


# --- ip:port -> host attribution ----------------------------------------------

def load_ip_to_host_map(logs_dir):
    """Returns (mapping, found). See sampler.py's write_ip_to_host_snapshot
    for why this is read from a snapshot file rather than re-resolved here:
    Docker service-name DNS ("backend-1") is only reachable from inside the
    container's network, not from a host machine running this script
    directly, and the snapshot also reflects the exact resolution NGINX
    was actually using at the time, not a fresh (potentially different)
    lookup. When missing (e.g. hand-rolled logs from a different setup),
    callers fall back to labeling backends by their raw ip:port -- still
    correct, just less readable."""
    path = os.path.join(logs_dir, IP_TO_HOST_FILENAME)
    if not os.path.exists(path):
        return {}, False
    with open(path) as f:
        return json.load(f), True


# --- access log parsing --------------------------------------------------------

def parse_access_log(path, start_ts_ms, end_ts_ms, ip_to_host):
    """Parses the pipe-delimited log_format from generate_config.py:
    $msec | $uri | $upstream_addr | $upstream_response_time |
    $upstream_header_time | $status | $sent_http_x_degradation_state

    $msec and the two response-time fields are seconds-with-millisecond-
    decimal (e.g. "1721923200.123", "0.014"), never raw integer
    milliseconds, despite the field names elsewhere using an "_ms" suffix
    for readability -- see AGENT.md. Converted to integer ms here so
    everything downstream works in one consistent unit.

    Records outside [start_ts_ms, end_ts_ms] are dropped -- this is the
    run-isolation mechanism: multiple runs append to the same log files,
    and runs.log's start/end timestamps are the only thing that separates
    one run's requests from another's afterward.
    """
    records = []
    if not os.path.exists(path):
        return records
    with open(path) as f:
        for line in f:
            fields = [x.strip() for x in line.strip().split("|")]
            if len(fields) != 7:
                continue  # malformed/partial line (e.g. truncated by a concurrent write)
            msec_str, uri, upstream_addr, response_time_str, header_time_str, status, degradation_state = fields
            try:
                ts_ms = round(float(msec_str) * 1000)
            except ValueError:
                continue
            if ts_ms < start_ts_ms or ts_ms > end_ts_ms:
                continue
            first_addr = upstream_addr.split(",")[0].strip()
            host = ip_to_host.get(first_addr, first_addr)
            record = {
                "ts_ms": ts_ms,
                "uri": uri,
                "host": host,
                "status": status,
                "degradation_state": degradation_state if degradation_state != "-" else None,
                "response_time_ms": _parse_seconds_field(response_time_str),
                "header_time_ms": _parse_seconds_field(header_time_str),
            }
            records.append(record)
    records.sort(key=lambda r: r["ts_ms"])
    return records


def _parse_seconds_field(value):
    if value == "-":
        return None
    try:
        return float(value) * 1000.0
    except ValueError:
        return None


# --- weights.csv (Stream 1's one Phase-4-visible exception) ------------------

def read_weights_csv(logs_dir, algo, start_ts_ms, end_ts_ms):
    """Returns rows (list of {backend: int_weight}) within the run window,
    in file order. rr has no weights file (static, unweighted -- see
    AGENT.md); callers should treat that as "not applicable", not "zero
    changes", since rr was never eligible to change in the first place."""
    path = os.path.join(logs_dir, f"{algo}{WEIGHTS_CSV_SUFFIX}")
    if not os.path.exists(path):
        return None
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        backend_cols = [c for c in reader.fieldnames if c not in ("timestamp_ms", "timestamp_iso")]
        for row in reader:
            ts_ms = int(row["timestamp_ms"])
            if ts_ms < start_ts_ms or ts_ms > end_ts_ms:
                continue
            rows.append({b: int(row[b]) for b in backend_cols})
    return rows


def count_weight_changes(weight_rows):
    """Transitions between consecutive rows inside the run window. A change
    that lands exactly on a window boundary (the row just before the run
    started differs from the first row inside it) isn't counted here --
    only transitions strictly between two in-window rows are, since that's
    what the row-to-row diff can see without reaching outside the
    isolated run. Undercounts by at most one per algorithm per run;
    documented in analysis/README.md rather than engineered around."""
    if weight_rows is None:
        return None
    return sum(1 for a, b in zip(weight_rows, weight_rows[1:]) if a != b)


# --- stats ---------------------------------------------------------------------

STAT_NAMES = ("mean", "median", "p90", "p95", "p99")
_STAT_PERCENTILES = {"median": 50, "p90": 90, "p95": 95, "p99": 99}


def mean(values):
    return sum(values) / len(values) if values else None


def _percentile_from_sorted(sorted_values, pct):
    if not sorted_values:
        return None
    rank = max(1, round(pct / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


def percentile(values, pct):
    """Nearest-rank percentile, stdlib only (no numpy). Simple and adequate
    at this project's data volumes; swap for numpy.percentile if adapting
    this for much larger logs."""
    if not values:
        return None
    return _percentile_from_sorted(sorted(values), pct)


def _stat_from_sorted(sorted_values, name):
    """name in STAT_NAMES -- shared by ttfb_stats and the bootstrap below,
    so a resample only needs sorting once to read off all five stats."""
    if name == "mean":
        return mean(sorted_values)
    return _percentile_from_sorted(sorted_values, _STAT_PERCENTILES[name])


def ttfb_stats(values):
    """n/mean/median/p90/p95/p99 off one shared, sorted pass -- used by both
    the stdout summary table and the standalone stats-comparison report, so
    the two never quietly disagree on how a stat was computed. Median is
    just the p50 case of the same nearest-rank percentile() above, not a
    separately-defined statistic. Always runs on the full sample -- a
    single sort is cheap even at real-experiment scale (see
    bootstrap_delta_cis for the one place that actually needs capping)."""
    if not values:
        return None
    ordered = sorted(values)
    stats = {name: _stat_from_sorted(ordered, name) for name in STAT_NAMES}
    stats["n"] = len(ordered)
    return stats


def capped_sample(values, max_n, rng):
    """Returns values unchanged if already within max_n, else a
    reproducible random subsample of size max_n (without replacement).

    Only meant for use ahead of bootstrap_delta_cis. ttfb_stats and
    mann_whitney_u each do a single O(n log n) sort -- still sub-second
    even against a real experiment's full log (millions of lines), so
    neither needs this. The bootstrap below repeats that sort thousands of
    times per comparison, which *is* the part that would stop scaling to
    "hundreds of times longer than a smoke test" without a cap -- see
    AGENT.md."""
    if len(values) <= max_n:
        return list(values)
    return rng.sample(values, max_n)


def bootstrap_delta_cis(sample_a, sample_b, rng, n_resamples, ci):
    """Percentile-bootstrap confidence intervals for (stat(a) - stat(b)),
    independently for mean/median/p90/p95/p99. Each resample is drawn and
    sorted ONCE, then all five stats are read off that single sorted
    resample, rather than resampling separately per stat -- five stats for
    the cost of one sort.

    Pass already-capped samples (see capped_sample) if the true sample
    sizes might be large: cost here is
    O(n_resamples * (len(sample_a) + len(sample_b)) * log(...)).

    Returns {stat_name: (lo, hi)}. Deliberately does not return the
    delta's point value -- callers already have that from the full-data
    ttfb_stats, and should pair it with the CI here, which only describes
    the uncertainty around that point (estimated from a resample of
    the, possibly subsampled, same data)."""
    n_a, n_b = len(sample_a), len(sample_b)
    deltas = {name: [] for name in STAT_NAMES}
    for _ in range(n_resamples):
        resample_a = sorted(rng.choices(sample_a, k=n_a))
        resample_b = sorted(rng.choices(sample_b, k=n_b))
        for name in STAT_NAMES:
            deltas[name].append(_stat_from_sorted(resample_a, name) - _stat_from_sorted(resample_b, name))
    alpha = 1 - ci
    result = {}
    for name in STAT_NAMES:
        ordered = sorted(deltas[name])
        lo_idx = max(0, int(alpha / 2 * n_resamples))
        hi_idx = min(n_resamples - 1, int((1 - alpha / 2) * n_resamples) - 1)
        result[name] = (ordered[lo_idx], ordered[hi_idx])
    return result


def _rank_with_ties(values):
    """1-based ranks matching input order; tied values get the average
    rank of their tied block -- the standard tie-handling for a
    rank-based test like Mann-Whitney."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    n = len(order)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1  # 1-based average rank across tie block [i, j]
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _standard_normal_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def mann_whitney_u(sample_a, sample_b):
    """Two-sided Mann-Whitney U test -- normal approximation with tie
    correction, stdlib only (no scipy dependency). Tests whether one
    distribution tends to produce larger values than the other, without
    assuming a shape -- appropriate for TTFB, which is right-skewed, not
    normal (a t-test's assumptions don't hold well here).

    Runs on full samples, not capped: the cost here is one sort, cheap
    even at real-experiment scale, unlike the repeated resampling in
    bootstrap_delta_cis -- so this test gets the full statistical power
    of however much data a run actually produced.

    Returns (u_statistic, p_value), or (None, None) if either sample is
    empty. The normal approximation is standard once both groups exceed
    roughly 20 samples (comfortably true even for smoke-test runs); it is
    not a small-sample-exact test.
    """
    n1, n2 = len(sample_a), len(sample_b)
    if n1 == 0 or n2 == 0:
        return None, None
    combined = list(sample_a) + list(sample_b)
    ranks = _rank_with_ties(combined)
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2.0

    n = n1 + n2
    if n <= 1:
        return u1, 1.0

    # Tie correction for the variance -- standard formula (see e.g.
    # Hollander & Wolfe, "Nonparametric Statistical Methods").
    sorted_vals = sorted(combined)
    tie_term = 0.0
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        t = j - i + 1
        if t > 1:
            tie_term += t ** 3 - t
        i = j + 1

    mu_u = n1 * n2 / 2.0
    variance = n1 * n2 / 12.0 * ((n + 1) - tie_term / (n * (n - 1)))
    if variance <= 0:
        return u1, 1.0  # every value identical -- no evidence of a difference

    sigma_u = math.sqrt(variance)
    # Continuity correction: nudge U1 half a unit toward mu_u before
    # standardizing, standard practice when a discrete statistic is
    # approximated by a continuous (normal) distribution.
    continuity = 0.5 if u1 > mu_u else (-0.5 if u1 < mu_u else 0.0)
    z = (u1 - mu_u - continuity) / sigma_u
    p_value = 2 * (1 - _standard_normal_cdf(abs(z)))
    return u1, min(1.0, p_value)


def bucket_by_window(records, window_seconds, start_ts_ms):
    """Groups records into fixed-size time buckets of window_seconds,
    anchored to the run's own start timestamp -- this deliberately mirrors
    the sampling loop's own window boundaries (see sampler.py) so an
    analysis window lines up with the window the algorithm was actually
    reacting within, as long as --window-seconds matches the container's
    real TICK_SECONDS (see analyze.py and AGENT.md's "--tick and
    TICK_SECONDS are not the same channel" note for the one case this
    assumption can drift)."""
    window_ms = window_seconds * 1000
    buckets = {}
    for record in records:
        idx = int((record["ts_ms"] - start_ts_ms) // window_ms)
        buckets.setdefault(idx, []).append(record)
    return buckets
