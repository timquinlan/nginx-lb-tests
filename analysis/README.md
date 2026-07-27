# analysis/ — Phase 4 data analysis tooling

Reads one experiment run's NGINX access logs (plus its weight-history
CSVs) and produces a summary table and PNG line charts. No database, no
extra infrastructure — everything is read directly from the plain-file
log directory (`./logs` on the host; see the root `AGENT.md`'s "Bind
mounts, not named volumes").

## Log schema this reads

**Access logs (`{algo}.access.log`, one per algorithm path)** — pipe-delimited,
one line per proxied request:

```
$msec | $uri | $upstream_addr | $upstream_response_time | $upstream_header_time | $status | $sent_http_x_degradation_offset_ms
```

- `$msec` and the two response-time fields are **seconds with a
  millisecond decimal** (e.g. `1721923200.123`, `0.014`) — not raw
  integer milliseconds, despite field names elsewhere using an `_ms`
  suffix for readability. `analysis/log_reader.py` converts these to
  integer milliseconds on read.
- The last field is a **signed float already in ms** (not
  seconds-with-decimal like the two above) — negative means that backend
  was currently faster than its own baseline at request time, positive
  means slower. Each backend drifts independently; there's no shared
  "state" across backends anymore — see `AGENT.md`, "backends share a
  baseline, drift independently."
- `$upstream_addr` is a **resolved `ip:port`, never the original
  hostname**. `ip_to_host.json` (written by `controller/sampler.py`,
  refreshed every sampling window) maps it back to the name from
  `upstream-hosts.txt`. If that file is missing, backends are labeled by
  raw `ip:port` instead — still correct, just less readable.
- A `-` in any field means NGINX had nothing to log there (e.g. the
  request never reached an upstream). These rows are parsed but excluded
  from any stat that needs the missing value.

**`runs.log`** — JSON Lines, one object per traffic-generator invocation:
`run_index`, `start_ts_ms`, `end_ts_ms`, `tick_seconds`, `rps_per_path`,
`planned_duration_ticks`, `actual_duration_s`, `paths`, `change_counts`,
`interrupted`. Multiple runs append to the same access log files —
**`start_ts_ms`/`end_ts_ms` are the only thing that separates one run's
requests from another's afterward.**

**`{algo}.weights.csv`** (dynamic algorithms only — `rr` has none, since
it's static/unweighted) — wide-form, one row per sampling window,
regardless of whether the weights actually changed: `timestamp_ms,
timestamp_iso, <one column per backend>`.

## Run isolation

Every output is scoped to exactly one run, selected by:

```sh
python3 analysis/analyze.py --run 3            # by runs.log's run_index
python3 analysis/analyze.py --start-ts 1721923200123   # by exact start_ts_ms
python3 analysis/analyze.py                    # defaults to the most recent run
```

Internally: `read_weights_csv`/`parse_access_log` both filter to
`[start_ts_ms, end_ts_ms]` from the selected `runs.log` record before
anything else happens.

## Running it

**Automatically** — `traffic_generator.py`'s end-of-run hook calls
`analyze.run_analysis()` in-process after every run and prints the
summary to stdout. Nothing to do here; this is wired into the container
image already (see root `AGENT.md`).

**Manually, from the host** (this is what the bind-mounted `./logs`
directory is for — see root `AGENT.md`):

```sh
pip install -r analysis/requirements.txt   # matplotlib; one-time, host-side only
python3 analysis/analyze.py --run 3
```

**Manually, against a running container:**

```sh
docker exec -it $(docker compose -f docker-compose.full.yml ps -q controller) \
  python3 analysis/analyze.py --run 3
```

Charts land in `<logs-dir>/analysis/` (i.e. `./logs/analysis/` from the
host either way) — a subdirectory of the existing bind mount, so no new
Compose volume was needed for this.

## Outputs

- **Stdout summary table** — per algorithm: request count, mean TTFB,
  p95 TTFB, best/worst sampling window (by mean TTFB) and when it
  happened, and config-change count (`N/M sampling windows`, or `static
  (n/a)` for `rr`).
- **`runN_stats_report.txt`** — the overall statistical comparison: TTFB
  mean/median/p90/p95/p99 for every discovered algorithm (point estimates,
  full data), then, for each non-control algorithm vs `rr` (the control —
  see `CONTROL_ALGO` in `analyze.py`):
  - a **Mann-Whitney U p-value** on the full TTFB samples — is the overall
    distribution different from the control at all, with no assumption
    that TTFB is normally distributed (it isn't — it's right-skewed).
  - a **bootstrap 95% confidence interval** on each stat's delta — is
    *this specific stat's* difference likely non-zero. A stat is flagged
    `significant` when its CI excludes 0.

  These are two different questions and are labeled as such — a
  significant Mann-Whitney result doesn't guarantee every individual
  percentile's CI excludes 0, and vice versa. Negative delta = faster
  than the control. If no `rr` log exists in a given run (e.g. an adapted
  setup with a different control), the point-estimate table is still
  written; only the significance section is skipped, with a note
  explaining why.

  **On short smoke-test runs, expect "not significant" as the normal
  result** — a few thousand requests over tens of seconds is rarely
  enough to distinguish real algorithm differences from noise, especially
  against backends with deliberately overlapping latency ranges. This
  isn't a bug; it's what the numbers are supposed to say until a real
  (hours-long, much higher request count) run gives the tests enough data
  to work with.

  **Performance at real-experiment scale:** point estimates and the
  Mann-Whitney test always run against the true full sample (each is a
  single sort, still well under a second at hundreds of thousands of
  samples). Only the bootstrap — which repeats that sort thousands of
  times — is capped, by default to 5000 observations/side
  (`--max-bootstrap-sample`), regardless of how much larger the real
  sample is. Measured: ~2s for a 2000-resample bootstrap capped at
  5000/side, even against a simulated 300k-sample-per-side dataset.
  `--bootstrap-resamples` and `--alpha` (which also sets the CI's coverage
  as `1 - alpha`, so the two can't drift out of sync) are also
  overridable. The bootstrap's random draws are seeded from the run's own
  `start_ts_ms`, so re-running the report against the same run reproduces
  the same CIs.
- **`runN_ttfb_over_time.png`** — one line per algorithm, mean TTFB per
  analysis window, with a translucent red area overlay showing the mean
  `X-Degradation-Offset-Ms` across *all* requests that window (pooled
  across algorithms — degradation is a property of the backend, not of
  whichever algorithm routed to it), signed: positive means the pool was
  net slower than baseline that window, negative means net faster.
- **`runN_{algo}_selection_frequency.png`** — one chart per algorithm,
  one line per backend, request count per analysis window. `rr`'s should
  look roughly flat/uniform — that's the sanity-check baseline the other
  two are meant to visibly diverge from.
- **Stderr warning** if an algorithm changed weights in less than 30% of
  its sampling windows during the run — signals early convergence (design
  doc: "user may need to increase backend variability").

## The "analysis window" assumption

Chart/table bucketing defaults to the run's own `tick_seconds` (from
`runs.log`) as the window size, since sampling windows are `1x tick` by
default. Override with `--window-seconds` if a run was started with a
`--tick` that was deliberately different from the container's real
`TICK_SECONDS` (see root `AGENT.md`, "`--tick` and `TICK_SECONDS` are not
the same channel") — in that case the true sampling window and this
run's own `--tick` describe different cadences, and only you know which
one you actually want reflected in the chart's x-axis.

## Adapting this for a different algorithm or log schema

- **New algorithm:** add its `{algo}.access.log` file (matching the
  pipe-delimited schema above) and it's picked up automatically —
  `discover_algos()` derives the algorithm list from which `*.access.log`
  files exist in the logs directory, nothing is hardcoded to
  `rr`/`aco`/`mc`. If it's a weighted algorithm, add a matching
  `{algo}.weights.csv` too (wide-form as described above) to get its
  config-change stats and warning; omitting it is treated as "static,
  not applicable" rather than "zero changes," same as `rr`.
- **Different log schema entirely:** everything schema-specific lives in
  `analysis/log_reader.py`'s `parse_access_log()` / `read_weights_csv()`
  — the rest of `analyze.py` (stats, bucketing, charts) works against the
  parsed-record shape those two functions return (`ts_ms`, `host`,
  `header_time_ms`, etc.), not the raw log text. Point-edit those two
  functions for a different delimiter/field order/timestamp unit and
  everything downstream keeps working.
- `analysis/log_reader.py` deliberately doesn't import anything from
  `controller/` (see its module docstring) — it only assumes the on-disk
  formats documented above, so it stays usable even outside this
  project's own container.
