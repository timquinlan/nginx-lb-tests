# upstream-rl

A load balancing science experiment of sorts: compares multiple upstream-selection algorithms running **simultaneously** against the same pool of backends, under continuously degrading and improving latency conditions. The experimental algorithms are **Ant Colony Optimization (ACO)** and a **Markov Chain**, measured against standard NGINX algorithms (round robin, random, random two, least_conn). Rround robin as the baseline control. The architecture is built so additional algorithms can be added without restructuring the project.

The primary contribution isn't the experiment result itself (which algorithm "wins") -- it's the experimentation *methodology*: a reproducible framework for measuring performance differences between upstream-selection algorithms, with analysis tooling specific enough to work out of the box against this project's log format but simple enough to adapt to a different algorithm or log schema. Longer-term framing: a paper/talk on ACO-inspired load balancing for edge devices that operate without centralized orchestration -- ant colony behavior is inherently decentralized and locally-informed, which is a natural fit for that setting. 

See `FINDINGS.md` for the results/conclusions writeup -- what the experiment actually showed, consolidated across every run and every axis varied. See `AGENT.md` for the full architecture writeup, every design tradeoff made along the way (and why), and bugs found/fixed during development.

## Quick start

```sh
docker compose -f docker-compose.full.yml up --build -d
# the time of the test is tick * duration, for a 10 minute test use --tick 10 --duration 60
docker exec -it $(docker compose -f docker-compose.full.yml ps -q controller) \
  python3 traffic_generator.py --tick 10 --rps 100 --duration 6   # one minute smoke test
```

That builds and starts the controller plus the local backend pool (validates backends, generates NGINX config, primes `/aco`/`/mc` with equal weights), then runs a short traffic burst against all six paths. Results (stdout summary, PNG charts, text stats report) land in `./logs/analysis/`. See "Running an experiment" below for real (non-smoke-test) defaults, re-analyzing a past run, and pointing at external backends instead of the local pool.

## How it works

One controller container runs NGINX plus a set of Python scripts (backend validation, NGINX config generation, priming, per-algorithm sampling loops, the traffic generator), and in local/full mode, one lightweight container per backend. Six location blocks sit behind the same NGINX instance, all proxying to the *same* backend pool:

| Path | Mechanism | Who rewrites it |
|---|---|---|
| `/rr`  | Unweighted round robin. Used as the baseline control. | Nobody -- generated once, static for the life of the container. The baseline. |
| `/random` | NGINX `random` -- a weighted dice roll, every request. With the amount of traffic the test pushes, this quickly converges to an even distribution.  It is included as a check against the round robin control, e.g. if there is a significant divergence between /rr and /random that run's results should be discarded. | Nobody -- static, same as `/rr` |
| `/random2` | NGINX `random two` -- pick 2 at random, then route to whichever of those 2 has fewer active connections (`least_conn` is the built-in default method for `two` in open-source NGINX) | Nobody -- static, same as `/rr` |
| `/leastconn` | NGINX `least_conn` -- always route to whichever backend has the fewest active connections, no randomness | Nobody -- static, same as `/rr` |
| `/aco` | Weighted round robin | The ACO module, every sampling window |
| `/mc`  | Weighted round robin | The Markov module, every sampling window |

`/aco` and `/mc` share the same underlying NGINX mechanism (weighted round robin) -- the only difference between them is who computes the weights and how. Every sampling window, each reads **its own** access log (a closed feedback loop: the traffic distribution the algorithm chose is exactly what feeds its next decision), computes each backend's mean response time (TTFB, via NGINX's `$upstream_header_time`), hands that to its algorithm module, gets back an integer weight (1-100) per backend, writes it to its own upstream conf, and reloads NGINX gracefully. If a backend gets zero real traffic in a window (common once an algorithm concentrates its weight elsewhere), the sampler falls back to a single direct probe of that backend so the algorithm always has at least one observation to work with.

`/rr`, `/random`, `/random2`, and `/leastconn` configurations are all **static**, the same way `/rr` always was: NGINX's own upstream-block method directive (`random`, `random two`, `least_conn` -- see `controller/nginx/upstream_conf.py`) does the selection internally, every request, with no Python-side weight computation, no sampling loop, no `weights.csv`, and no config-change counter. They exist to compare this project's adaptive algorithms against the load-balancing methods NGINX (and most L7 reverse proxies) already ship out of the box -- see "The algorithms" below for why `random two least_conn` specifically isn't a fourth path.

Before any of this starts, the controller validates every backend is reachable (fail-fast, with retries -- so Docker start-up ordering doesn't cause a false failure) and runs one full priming pass so `/aco` and `/mc` start from algorithm-derived weights on their very first proxied request, not the equal-weight placeholder.

## The algorithms

- **Round robin** (`/rr`) -- no learning, no module. The control everything else is measured against.
- **Random** (`/random`) -- NGINX's `random` upstream directive: a fresh weighted dice roll per request, no memory of past selections at all. **Not really a comparison algorithm in its own right** -- at scale, an unweighted dice roll and unweighted round robin converge to the same distribution (law of large numbers), so `/random` vs. `/rr` is expected to come back statistically indistinguishable every time, and has in every run so far. Kept deliberately anyway, as a cheap validity check: it's a mechanistically independent NGINX code path (a probabilistic selector, not the round-robin state machine `/rr` uses) that's expected to agree with `/rr`'s result. If it ever *didn't* agree, that would flag a problem with the measurement harness itself (uneven backend health, stale DNS, a skewed traffic split) rather than a real algorithmic difference -- two independently-implemented baselines agreeing is stronger evidence the setup is trustworthy than either alone.
- **Random two** (`/random2`) -- NGINX's `random two` directive: pick 2 backends at random, then route to whichever of those 2 currently has fewer active connections. This is NGINX's own "power of two choices" method -- some live, instantaneous state (connection count) feeds the decision, unlike plain `/random`.
- **Least connections** (`/leastconn`) -- NGINX's `least_conn` directive: always route to whichever backend has the fewest active connections, pool-wide, no randomness anywhere in the selection.
- **ACO** (`/aco`) -- one pheromone value per backend. Every window: evaporate all of them by a configurable rate, then deposit an amount inversely proportional to that backend's latency (faster backend, bigger deposit). Weight is read off the pheromone table relative to its current max. This gives ACO *momentum*: it's slow to forget, so it stays stable but lags behind sudden latency shifts.
- **Markov Chain** (`/mc`) -- a transition matrix rebuilt completely from scratch every window, purely from that window's latency observations (no self-transitions, so every window models moving to a different, faster backend). Weight is the matrix's stationary distribution (computed via power iteration), scaled directly by 100. This makes Markov genuinely *memoryless*: no state carries over between windows, so it reacts fully and immediately to whatever just happened, at the cost of being noisier.

These six sit on a rough information/memory-horizon ladder: `/rr` and `/random` use no live or historical information at all; `/random2` and `/leastconn` use NGINX's own live, instantaneous state (current connection counts) with no memory of anything before this instant; `/mc` uses historical information (last window's latency observations) with no memory across windows; `/aco` uses historical information *with* persistent, decaying memory across windows. Where each one's unpredictability (if any) enters also differs: `/random` and `/random2` inject randomness at the selection mechanism itself (a dice roll, given a fixed state); `/aco`/`/mc` inject it one layer up instead. Which *weights* get chosen each window is effectively non-deterministic -- it's a function of the environment's own randomly-generated backend latency, fed through a deterministic update rule (same inputs would reproduce the same weights, but the inputs themselves aren't fixed). What NGINX actually runs on any given request, though, is a plain, deterministic weighted round-robin config -- whatever integer weight vector got written down that window is held fixed and cycled through predictably until the next window's reload overwrites it. So the non-determinism lives entirely in *which* WRR config gets written each tick, not in how that config gets executed once it's live. Avoid describing this split as a flat "deterministic vs. non-deterministic" binary -- both halves of that distinction matter and answer different questions (see `AGENT.md`).

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

**Default is 10s.** The ratio between `--tick` (the sampling window) and `DEGRADATION_MEAN_DWELL_SECONDS` (how fast the environment actually changes) -- not the absolute tick value -- determines whether `aco`/`mc` get a clean signal at all; aim for at least ~4x the sampling window on the fastest backend class as a starting point if you change either. See `FINDINGS.md` for the dose-response results (and a 60-minute-scale reconfirmation) behind this default, and `AGENT.md` for the underlying mechanism.

## Data & where it lands

Everything is written to plain host directories (bind mounts, not opaque Docker volumes) -- `ls`/`cat` them directly, no `docker exec` needed:

- **`./logs/{rr,random,random2,leastconn,aco,mc}.access.log`** -- the experimental evidence. One line per proxied request: `timestamp(ms) | request_path | backend | response_time | header_time(TTFB) | http_status | degradation_state`.
- **`./logs/{aco,mc}.weights.csv`** -- the applied integer weight per backend, per sampling window, wide-form (`timestamp_ms, timestamp_iso, backend-1, backend-2, ...`) so a line chart (x=time, y=weight, one line per backend) is a direct plot away. `rr`/`random`/`random2`/`leastconn` have no weights file -- all four are static, so there's nothing to log.
- **`./logs/runs.log`** -- one JSON line per traffic-generator run: start/end timestamps, tick, rps, planned vs. actual duration, and each algorithm's config-change count. Multiple runs append to the same log files; `runs.log`'s timestamps are how an individual run gets isolated later.
- **`./logs/analysis/`** -- Phase 4/5's output for each analyzed run: PNG charts (TTFB over time with a degradation overlay, per-backend selection frequency) and `runN_stats_report.txt` -- TTFB mean/median/p90/p95/p99 for every discovered algorithm, all six ranked best-to-worst, each non-control algorithm's comparison against the `rr` control (Mann-Whitney U p-value plus bootstrap confidence intervals -- not just deltas, an actual significance test), and an incremental head-to-head between the best-performing static/built-in algorithm and the best-performing adaptive one (`aco`/`mc`). See `analysis/README.md`.
- **`./nginx-conf/`** -- the generated NGINX confs themselves, if you want to see exactly what's live at any moment.


## Running an experiment

```sh
docker compose -f docker-compose.full.yml up --build -d
```

This validates backends, generates NGINX config, primes `/aco` and `/mc` with equal weights, and starts the per-algorithm sampling loops automatically -- no further setup needed. It does **not** send any traffic on its own. Trigger an experiment run manually, against the already-running container:

```sh
docker exec -it $(docker compose -f docker-compose.full.yml ps -q controller) \
  python3 traffic_generator.py --tick 5 --rps 5 --duration 4   # smoke test: 5s tick, ~20s total, --rps explicit (default is now 500, see below)
```

Run it again (with different `--tick`/`--rps`/`--duration`) as many times as you like against the same container -- each run appends its own record to `runs.log`. See `AGENT.md` for why setup and traffic generation are split this way.

Run these one at a time, not concurrently -- two overlapping `traffic_generator.py` invocations would collide on the same run index and mix their traffic together in the same access logs with no way to tell them apart afterward.

Each run automatically triggers analysis at the end -- a stdout summary table, PNG charts, and a text stats report (TTFB mean/median/p90/p95/p99 for every discovered algorithm, all ranked best-to-worst, each non-control algorithm vs. the `rr` control with a Mann-Whitney p-value and bootstrap confidence intervals, plus an incremental best-built-in-vs-best-adaptive comparison -- see `analysis/README.md`), all under `./logs/analysis/`. To analyze a run again later, or a different run than the most recent one:

```sh
python3 analysis/analyze.py --run 3          # from the host, against ./logs
```

**On short smoke-test runs, expect the stats report to say "not statistically significant" everywhere** -- a few thousand requests over tens of seconds usually isn't enough to separate real algorithm differences from noise, especially against backends with deliberately overlapping latency ranges. That's the correct, expected answer at this scale, not a sign anything is broken -- see `analysis/README.md`.

## Controller-only mode (real backends instead of the simulation)

`docker-compose.full.yml` builds and starts its own backend containers with synthetic, controllable latency (the "Backend pool" table above) -- that's what every result in `FINDINGS.md` is measured against, and it's the right choice for reproducing this project's own experiments. `docker-compose.controller.yml` is the other half: it starts **only** the controller (NGINX + the Python scripts), pointed at whatever real hosts you list, with no backend containers built at all. Use it to run the same six-algorithm comparison against actual infrastructure -- real servers, cloud VMs, another team's staging fleet -- instead of the simulation, e.g. to sanity-check whether the simulated results hold up against real-world latency behavior, or to just use this project as a genuine load-balancing algorithm comparison tool for a backend pool you actually run.

**Configuring `upstream-hosts.txt` for this mode:** one hostname or IP per line, `#` for comments, blank lines ignored (`controller/common.py`'s `read_upstream_hosts()`) -- the same file `docker-compose.full.yml` uses, just with different meaning: there, each line is a Docker Compose service name resolved by Docker's embedded DNS; here, it's any host reachable from the controller container over the network. Nothing hardcodes a backend count -- scaling from 5 to 25 real hosts is purely adding lines.

**Every listed host is assumed to be listening on the same port** -- `BACKEND_PORT` (`controller/common.py`, default `8080`), applied uniformly to every line in the file, not settable per-host, and not currently exposed as an environment override in `docker-compose.controller.yml`. If your real backends listen on a different port, add a `BACKEND_PORT: "${BACKEND_PORT:-8080}"` line to that Compose file's controller `environment:` block yourself (same pattern already used for `TICK_SECONDS`) -- there's no `host:port` syntax inside `upstream-hosts.txt` itself.

```sh
# edit upstream-hosts.txt to list reachable external hosts/IPs first
docker compose -f docker-compose.controller.yml up --build -d
```

Startup still runs the same fail-fast backend validation as `docker-compose.full.yml` (see "How it works" above) -- if a listed host isn't reachable on `BACKEND_PORT`, the container exits with an actionable error naming which host failed, rather than starting half-broken. `DEPLOY_MODE=external` also switches NGINX's `resolver` directive to a public DNS (`8.8.8.8`) instead of Docker's embedded one (`127.0.0.11`), since there's no Docker-internal DNS to resolve real external hostnames.

Only run one of these two Compose files at a time from this directory -- they share a Docker Compose project name (and volumes) by design, so bring one down (`docker compose -f <file> down`) before starting the other.

## Choosing `--rps`

**Default is 500** (per algorithm path, ~3000 aggregate across all six paths) -- approximates a sustained ~1B-hits/month production load, validated with zero `worker_connections` warnings or errors in any `*.error.log`. See `AGENT.md`, "Phase 6," for the CPU/throughput measurements and the journey to this default.

For a quick plumbing smoke test where realism doesn't matter, pass a low `--rps` explicitly (e.g. `--rps 5`).

The real ceiling is hardware-dependent -- push `--rps` up and watch `docker stats` alongside the per-algorithm error logs (`./logs/{rr,random,random2,leastconn,aco,mc}.error.log`) if you want to find yours. **A `worker_connections` warning in any `*.error.log` means you've gone past the ceiling** -- drop `--rps` back down.
