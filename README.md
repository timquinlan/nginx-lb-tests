# nginx-lb-tests

A load balancing experiment: compares multiple upstream-selection algorithms running **simultaneously** against the same pool of backends, under continuously degrading and improving latency conditions. Experimental adaptive algorithms (**Ant Colony Optimization (ACO)** and **Markov Chain**) are measured both on their own (plain weighted round robin) and paired with NGINX's `least_conn`, against standard NGINX algorithms (round robin, least_conn). Round robin is the baseline control. The architecture is built so additional algorithms can be added without restructuring the project.

The primary contribution isn't the experiment result itself (which algorithm "wins") -- it's the experimentation *methodology*: a reproducible framework for measuring performance differences between upstream-selection algorithms, with analysis tooling specific enough to work out of the box against this project's log format but simple enough to adapt to a different algorithm or log schema. Longer-term framing: a paper/talk on ACO-inspired load balancing more generally -- ant colony behavior is inherently decentralized and locally-informed, an interesting property to explore in its own right, independent of any particular deployment target.

See `ALGORITHMS.md` for how ACO and Markov Chain each work and why they were picked. See `FINDINGS.md` for the results/conclusions writeup -- what the experiment actually showed, consolidated across every run and every axis varied. See `EXPERIMENTS.md` for what's worth trying next -- knobs to push, ideas not yet built, and the math behind each. See `AGENT.md` for the full architecture writeup, every design tradeoff made along the way (and why), and bugs found/fixed during development.

## Quick start

```sh
docker compose -f docker-compose.full.yml up --build -d
# --duration is in minutes (wall-clock), not ticks
docker exec -it $(docker compose -f docker-compose.full.yml ps -q controller) \
  python3 traffic_generator.py --rps 100 --duration 1   # one minute smoke test
```

That builds and starts the controller plus the local backend pool (validates backends, generates NGINX config, primes `/aco_wrr`/`/mc_wrr`/`/aco_lc`/`/mc_lc` with equal weights), then runs a short traffic burst against all six paths. Results (stdout summary, PNG charts, text stats report) land in `./logs/analysis/`. See "Running an experiment" below for real (non-smoke-test) defaults, re-analyzing a past run, and pointing at external backends instead of the local pool.

To run an experiment against existing upstreams, see the **Controller-only mode (real backends instead of the simulation)** section of this document.

## How it works

One controller container runs NGINX plus a set of Python scripts (backend validation, NGINX config generation, priming, per-algorithm sampling loops, the traffic generator), and in local/full mode, one lightweight container per backend. Six location blocks sit behind the same NGINX instance, all proxying to the *same* backend pool:

| Path | Mechanism | Who rewrites it |
|---|---|---|
| `/rr_control` | Unweighted round robin. Used as the baseline control. | Nobody -- generated once, static for the life of the container. The baseline. |
| `/leastconn` | NGINX `least_conn` -- always route to whichever backend has the fewest active connections, no randomness | Nobody -- static, same as `/rr_control` |
| `/aco_wrr` | Weighted round robin | The ACO module, every sampling window |
| `/mc_wrr`  | Weighted round robin | The Markov module, every sampling window |
| `/aco_lc` | `least_conn` + `weight=` (weighted `least_conn`) | The ACO module, every sampling window |
| `/mc_lc`  | `least_conn` + `weight=` (weighted `least_conn`) | The Markov module, every sampling window |

`/aco_wrr` and `/mc_wrr` share the same underlying NGINX mechanism (weighted round robin) -- the only difference between them is who computes the weights and how. `/aco_lc` and `/mc_lc` pair those same two algorithms with NGINX's `least_conn` method instead, each via its own separate algorithm instance (not shared with its `_wrr` sibling). Every sampling window, each dynamic algorithm reads **its own** access log (a closed feedback loop: the traffic distribution the algorithm chose is exactly what feeds its next decision), computes each backend's mean response time (TTFB, via NGINX's `$upstream_header_time`), hands that to its algorithm module, gets back an integer weight (1-100) per backend, writes it to its own upstream conf, and reloads NGINX gracefully. If a backend gets zero real traffic in a window (common once an algorithm concentrates its weight elsewhere), the sampler falls back to a single direct probe of that backend so the algorithm always has at least one observation to work with.

`/rr_control` and `/leastconn` configurations are both **static**: NGINX's own upstream-block method directive (`least_conn` -- see `controller/nginx/upstream_conf.py`) does the selection internally, every request, with no Python-side weight computation, no sampling loop, no `weights.csv`, and no config-change counter. They exist to compare this project's adaptive algorithms against the load-balancing methods NGINX (and most L7 reverse proxies) already ship out of the box.

Before any of this starts, the controller validates every backend is reachable (fail-fast, with retries -- so Docker start-up ordering doesn't cause a false failure) and runs one full priming pass so `/aco_wrr`, `/mc_wrr`, `/aco_lc`, and `/mc_lc` start from algorithm-derived weights on their very first proxied request, not the equal-weight placeholder.

## The algorithms

- **Round robin** (`/rr_control`) -- no learning, no module. The control everything else is measured against.
- **Least connections** (`/leastconn`) -- NGINX's `least_conn` directive: always route to whichever backend has the fewest active connections, pool-wide, no randomness anywhere in the selection.
- **ACO, weighted round robin** (`/aco_wrr`) -- one pheromone value per backend. Every window: evaporate all of them by a configurable rate, then deposit an amount inversely proportional to that backend's latency (faster backend, bigger deposit). Weight is read off the pheromone table relative to its current max. This gives ACO *momentum*: it's slow to forget, so it stays stable but lags behind sudden latency shifts.
- **Markov Chain, weighted round robin** (`/mc_wrr`) -- a transition matrix rebuilt completely from scratch every window, purely from that window's latency observations (no self-transitions, so every window models moving to a different, faster backend). Weight is the matrix's stationary distribution (computed via power iteration), scaled directly by 100. This makes Markov genuinely *memoryless*: no state carries over between windows, so it reacts fully and immediately to whatever just happened, at the cost of being noisier.
- **ACO, weighted least_conn** (`/aco_lc`) -- NGINX's `least_conn` directive *plus* integer `weight=` on every server, the weights rewritten every window by a second, independent ACO instance (own pheromone state, fed by `/aco_lc`'s own traffic, same tuning as `/aco_wrr`). `least_conn` picks the backend with the lowest `active_connections / weight`, so ACO's learned weights bias which backend wins a tie instead of replacing `least_conn`'s live signal with a lagged one -- combining a live, instantaneous signal with historical, decaying memory at the same time. See `FINDINGS.md` for how this compares against plain `/leastconn`.
- **Markov Chain, weighted least_conn** (`/mc_lc`) -- same idea as `/aco_lc`, pairing `least_conn` with a second, independent Markov Chain instance's weights instead of ACO's.

These six sit on a rough information/memory-horizon ladder: `/rr_control` uses no live or historical information at all; `/leastconn` uses NGINX's own live, instantaneous state (current connection counts) with no memory of anything before this instant; `/mc_wrr` uses historical information (last window's latency observations) with no memory across windows; `/aco_wrr` uses historical information *with* persistent, decaying memory across windows; `/aco_lc`/`/mc_lc` use both at once -- their algorithm's historical memory *and* `least_conn`'s live instantaneous state, layered together rather than one replacing the other. Which *weights* the dynamic algorithms choose each window is effectively non-deterministic -- it's a function of the environment's own randomly-generated backend latency, fed through a deterministic update rule (same inputs would reproduce the same weights, but the inputs themselves aren't fixed). What NGINX actually runs on any given request, though, is a plain, deterministic weighted round-robin config (or, for `/aco_lc`/`/mc_lc`, weighted `least_conn`) -- whatever integer weight vector got written down that window is held fixed and cycled through predictably until the next window's reload overwrites it. So the non-determinism lives entirely in *which* config gets written each tick, not in how that config gets executed once it's live. Avoid describing this split as a flat "deterministic vs. non-deterministic" binary -- both halves of that distinction matter and answer different questions (see `AGENT.md`).

In testing, ACO vs. Markov contrast shows up concretely: given the *identical* underlying latency data in the same window, ACO tends to lock onto a favorite backend and hold it (its leader often near weight 100, clearly separated from the rest), while Markov's weights stay much flatter and shift around more, since it never accumulates confidence the way ACO does. Neither algorithm "wins" outright -- the interesting question is under which conditions each one wins, and (now) how either compares against what NGINX already ships for free.

## Backend pool (default, full mode)

Defined in `upstream-hosts.txt` (the single source of truth -- nothing in the codebase hardcodes a backend count; scaling to 10 or 25 is just adding lines). Each backend has a fixed **personality** (its own `LATENCY_MIN_MS`/`MAX_MS` range) representing a heterogeneous hardware fleet -- some backends permanently weaker (e.g. older CPUs kept as low-weight backups), which is deliberate. Distinct personalities represent exactly the kind of asymmetry that static/instantaneous baseline can't see (see `AGENT.md`, "backends share a baseline, drift independently," for the fuller reasoning and the shared-baseline variant this was tried against):

| Backend | Latency range (personality) | Reshuffle cadence (mean dwell) | Character |
|---|---|---|---|
| `backend-1` | 150-200ms | 40s | fast, tight, reliable |
| `backend-2` | 150-350ms | 80s | fast floor, high variance |
| `backend-3` | 160-190ms | 40s | fast, very tight |
| `backend-4` | 220-450ms | 80s | medium baseline, moderate variance |
| `backend-5` | 350-600ms | 120s | slow baseline, high variance |


Every backend also independently drifts on top of its own personality range: at every reshuffle (a randomized dwell, not a fixed clock -- `DEGRADATION_MEAN_DWELL_SECONDS`, decoupled from `--tick`, see below), it draws a fresh **signed offset** (`DEGRADATION_OFFSET_MIN_MS`/`MAX_MS`, shared range across all backends, default -10ms to +250ms -- more room to get worse than to get better) and holds it until the next reshuffle. . This is fully decentralized -- no backend knows about any other's current offset, no shared clock, no coordination. Reported via `X-Degradation-Offset-Ms` on every response and logged as ground truth to `./logs/degradation-{backend}.log` (`timestamp_ms,offset_ms,dwell_seconds`), since -- with both dwell time and the offset itself randomized -- the schedule can't be computed in advance the way a fixed clock could.

**To "level out" the personalities for an identical-node run** (e.g. to reproduce the shared-baseline methodology check in `AGENT.md`), edit the 5 `LATENCY_MIN_MS`/`MAX_MS` values in `docker-compose.full.yml` directly for that run -- same workflow already used for every other backend-config experiment in this project (halved/doubled latency, wide-range offset). `DEGRADATION_OFFSET_MIN_MS`/`MAX_MS` remain single shared overrides (same `${VAR:-default}` pattern as `TICK_SECONDS`) regardless.

## Timing model

Everything algorithm/traffic-generator-side derives from one base unit, `--tick` (seconds) -- nothing there is hardcoded:

| Parameter | Multiplier | Default (10s tick) |
|---|---|---|
| Sampling window | 1x | 10s |

**Degradation timing is decoupled from `--tick`, on purpose.** It used to also derive from `--tick` (`DEGRADATION_MULTIPLIER x TICK_SECONDS`), which meant the environment's own dynamics -- not just the algorithms' reaction cadence -- silently rescaled with whatever `--tick` a given run happened to use, and (since the sampling window is also `1x tick`) the two were permanently phase-related for the life of a container. A longer real run wouldn't have diluted that coupling -- it would have just re-measured the same fixed relationship more precisely. Fixed by giving degradation its own timescale (`DEGRADATION_MEAN_DWELL_SECONDS`, per backend, in the table above, overridable as a single shared value the same way `TICK_SECONDS` is) and randomizing each visit's dwell time within it -- see `AGENT.md`, "Backend degradation timing decoupled and randomized," for the full rationale.

## Choosing `--tick`

**Default is 10s.** The ratio between `--tick` (the sampling window) and `DEGRADATION_MEAN_DWELL_SECONDS` (how fast the environment actually changes) -- not the absolute tick value -- determines whether `aco_wrr`/`mc_wrr`/`aco_lc`/`mc_lc` get a clean signal at all; aim for at least ~4x the sampling window on the fastest backend class as a starting point if you change either. See `AGENT.md` for the underlying mechanism.

## Data & where it lands

Everything is written to plain host directories (bind mounts, not opaque Docker volumes) -- `ls`/`cat` them directly, no `docker exec` needed:

- **`./logs/{rr_control,leastconn,aco_wrr,mc_wrr,aco_lc,mc_lc}.access.log`** -- the experimental evidence. One line per proxied request: `timestamp(ms) | request_path | backend | response_time | header_time(TTFB) | http_status | degradation_state`.
- **`./logs/{aco_wrr,mc_wrr,aco_lc,mc_lc}.weights.csv`** -- the applied integer weight per backend, per sampling window, wide-form (`timestamp_ms, timestamp_iso, backend-1, backend-2, ...`) so a line chart (x=time, y=weight, one line per backend) is a direct plot away. `rr_control`/`leastconn` have no weights file -- both are static, so there's nothing to log.
- **`./logs/runs.log`** -- one JSON line per traffic-generator run: start/end timestamps, tick, rps, planned vs. actual duration, and each algorithm's config-change count. Multiple runs append to the same log files; `runs.log`'s timestamps are how an individual run gets isolated later.
- **`./logs/analysis/`** -- Phase 4/5's output for each analyzed run: PNG charts (TTFB over time with a degradation overlay, per-backend selection frequency) and `runN_stats_report.txt` -- TTFB mean/median/p90/p95/p99 for every discovered algorithm, all six ranked best-to-worst, each non-control algorithm's comparison against the `rr_control` control (Mann-Whitney U p-value plus bootstrap confidence intervals -- not just deltas, an actual significance test), and an incremental head-to-head between the best-performing static/built-in algorithm and the best-performing adaptive one (`aco_wrr`/`mc_wrr`/`aco_lc`/`mc_lc`). See `analysis/README.md`.
- **`./nginx-conf/`** -- the generated NGINX confs themselves, if you want to see exactly what's live at any moment.


## Running an experiment

```sh
docker compose -f docker-compose.full.yml up --build -d
```

This validates backends, generates NGINX config, primes `/aco_wrr`, `/mc_wrr`, `/aco_lc`, and `/mc_lc` with equal weights, and starts the per-algorithm sampling loops automatically -- no further setup needed. It does **not** send any traffic on its own. Trigger an experiment run manually, against the already-running container:

```sh
docker exec -it $(docker compose -f docker-compose.full.yml ps -q controller) \
  python3 traffic_generator.py --rps 5 --duration 0.5   # smoke test: ~30s total, --rps explicit (default is now 500, see below)
```

Run it again (with different `--tick`/`--rps`/`--duration`) as many times as you like against the same container -- each run appends its own record to `runs.log`. See `AGENT.md` for why setup and traffic generation are split this way.

Run these one at a time, not concurrently -- two overlapping `traffic_generator.py` invocations would collide on the same run index and mix their traffic together in the same access logs with no way to tell them apart afterward.

Each run automatically triggers analysis at the end -- a stdout summary table, PNG charts, and a text stats report (TTFB mean/median/p90/p95/p99 for every discovered algorithm, all ranked best-to-worst, each non-control algorithm vs. the `rr_control` control with a Mann-Whitney p-value and bootstrap confidence intervals, plus an incremental best-built-in-vs-best-adaptive comparison -- see `analysis/README.md`), all under `./logs/analysis/`. To analyze a run again later, or a different run than the most recent one:

```sh
python3 analysis/analyze.py --run 3          # from the host, against ./logs
```

**On short smoke-test runs, expect the stats report to say "not statistically significant" everywhere** -- a few thousand requests over tens of seconds usually isn't enough to separate real algorithm differences from noise, especially against backends with deliberately overlapping latency ranges. That's the correct, expected answer at this scale, not a sign anything is broken -- see `analysis/README.md`.

`traffic_generator.py --help` is the authoritative, always-current reference for every flag (argparse) if you're invoking it directly against `docker-compose.full.yml`'s controller -- see "Choosing `--tick`"/"Choosing `--rps`" below for the reasoning behind the current defaults, and "Using `run_experiment.py`" below for a scriptable wrapper around the same workflow.

## Controller-only mode (real backends instead of the simulation)

`docker-compose.full.yml` builds and starts its own backend containers with synthetic, controllable latency (the "Backend pool" table above) -- that's what every result in `FINDINGS.md` is measured against, and it's the right choice for reproducing this project's own experiments. `docker-compose.controller.yml` is the other half: it starts **only** the controller (NGINX + the Python scripts), pointed at whatever real hosts you list, with no backend containers built at all. Use it to run the same seven-algorithm comparison against actual infrastructure -- real servers, cloud VMs, another team's staging fleet -- instead of the simulation, e.g. to sanity-check whether the simulated results hold up against real-world latency behavior, or to just use this project as a genuine load-balancing algorithm comparison tool for a backend pool you actually run.

**Configuring `upstream-hosts.txt` for this mode:** one hostname or IP per line, `#` for comments, blank lines ignored (`controller/common.py`'s `read_upstream_hosts()`) -- the same file `docker-compose.full.yml` uses, just with different meaning: there, each line is a Docker Compose service name resolved by Docker's embedded DNS; here, it's any host reachable from the controller container over the network. Nothing hardcodes a backend count -- scaling from 5 to 25 real hosts is purely adding lines.

**Every listed host is assumed to be listening on the same port** -- `BACKEND_PORT` (`controller/common.py`, default `8080`), applied uniformly to every line in the file, not settable per-host, and not currently exposed as an environment override in `docker-compose.controller.yml`. If your real backends listen on a different port, add a `BACKEND_PORT: "${BACKEND_PORT:-8080}"` line to that Compose file's controller `environment:` block yourself (same pattern already used for `TICK_SECONDS`) -- there's no `host:port` syntax inside `upstream-hosts.txt` itself.

```sh
# edit upstream-hosts.txt to list reachable external hosts/IPs first
docker compose -f docker-compose.controller.yml up --build -d
```

Startup still runs the same fail-fast backend validation as `docker-compose.full.yml` (see "How it works" above) -- if a listed host isn't reachable on `BACKEND_PORT`, the container exits with an actionable error naming which host failed, rather than starting half-broken. `DEPLOY_MODE=external` also switches NGINX's `resolver` directive to a public DNS (`8.8.8.8`) instead of Docker's embedded one (`127.0.0.11`), since there's no Docker-internal DNS to resolve real external hostnames.

**Backend latency/degradation simulation doesn't apply here.** `LATENCY_MIN_MS`/`DEGRADATION_*` are env vars on this project's own simulated backend containers (`backend/server.py`, `docker-compose.full.yml`) -- your real hosts just behave however they actually behave, nothing to configure on this project's side.

Only run one Compose file at a time from this directory -- `docker-compose.full.yml` and `docker-compose.controller.yml` share a Docker Compose project name (and volumes) by design, so bring one down (`docker compose -f <file> down`) before starting another.

## Using `run_experiment.py`

A host-side convenience wrapper around the manual `docker exec ... traffic_generator.py` + `analyze.py` workflow above -- same defaults and flags, just scriptable:

```sh
python3 run_experiment.py run --rps 40 --duration 10
```

This launches `traffic_generator.py` inside `docker-compose.full.yml`'s controller, live-streams its output, then automatically runs analysis once it finishes.

| Command | What it does |
|---|---|
| `run_experiment.py run` | Starts a run and analyzes the result. `--rps`/`--duration`/`--tick` mirror `traffic_generator.py`'s own flags; `--no-analyze` skips the automatic analysis step. |
| `run_experiment.py status` | Shows whether the controller is currently mid-run (with elapsed/estimated-remaining time), and its most recently completed run. |
| `run_experiment.py analyze [--run N]` | Re-runs analysis without starting new traffic -- defaults to the most recent run. |
| `run_experiment.py purge` | Deletes everything in `./logs` for a clean restart at run 1. Refuses if a run is currently in progress. |

## Choosing `--rps`

**Default is 40** (per algorithm path, ~240 aggregate across all six paths), changed from 500 on 2026-07-29 -- deliberately conservative to keep backend contention off the table as a confound, even though a same-day contention check (500rps vs. 50rps parallel) found no evidence that higher rps was distorting results against the adaptive algorithms. 40rps/path is also the base of that day's same-total-volume series (40rps/60min, 80rps/30min, 160rps/15min, 240rps/10min -- all 144k requests/path), where p99-vs-`rr_control` significance held at every point on the series, so nothing about correctness depends on running faster than this default. The older 500rps default was chosen to approximate a sustained ~1B-hits/month production load and was validated with zero `worker_connections` warnings or errors in any `*.error.log` -- still true at 500, just no longer the default. See `AGENT.md`, "NGINX process model: 4 workers, shared zones," for the underlying CPU/throughput mechanics.

For a quick plumbing smoke test where realism doesn't matter, pass a low `--rps` explicitly (e.g. `--rps 5`).

The real ceiling is hardware-dependent -- push `--rps` up and watch `docker stats` alongside the per-algorithm error logs (`./logs/{rr_control,leastconn,aco_wrr,mc_wrr,aco_lc,mc_lc}.error.log`) if you want to find yours. **A `worker_connections` warning in any `*.error.log` means you've gone past the ceiling** -- drop `--rps` back down.

On a Mac M4, `--rps 320` is the highest value confirmed to actually deliver what it asks for: runs at 40/80/160/320 all landed within ~0.2% of their target request count, but a 640 run only achieved ~551rps/path and a 1280 run landed at essentially the same ~548rps/path -- doubling the target didn't move the achieved rate, the signature of a hard ceiling (this generator sizes `0.5 threads/rps/path`, so 640rps across 7 paths means ~2,240 OS threads in one process -- GIL/OS-scheduling contention, not backend or NGINX behavior). No `worker_connections` warning fires when this happens -- it fails silently, so **check actual request counts (`n` in the stats report) against `rps x duration` for anything above ~320rps/path on comparable hardware** rather than trusting the requested rate was what actually ran. See `FINDINGS.md`'s caveats section for the full data.
