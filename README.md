# upstream-rl

A load balancing science experiment: it compares multiple upstream-selection algorithms running **simultaneously** against the same pool of backends, under identical, programmatically-controlled, degrading latency conditions. The initial algorithms are **Ant Colony Optimization (ACO)** and a **Markov Chain**, measured against stock, unweighted NGINX round robin as the baseline control. The architecture is built so additional algorithms can be added without restructuring the project.

The primary contribution isn't the experiment result itself (which algorithm "wins") -- it's the experimentation *methodology*: a reproducible framework for measuring performance differences between upstream-selection algorithms, with analysis tooling specific enough to work out of the box against this project's log format but simple enough to adapt to a different algorithm or log schema. Longer-term framing: a paper/talk on ACO-inspired load balancing for edge devices that operate without centralized orchestration -- ant colony behavior is inherently decentralized and locally-informed, which is a natural fit for that setting. 

See `AGENT.md` for the full architecture writeup, every design tradeoff made along the way (and why), and bugs found/fixed during development.

## How it works

One controller container runs NGINX plus a set of Python scripts (backend validation, NGINX config generation, priming, per-algorithm sampling loops, the traffic generator), and in local/full mode, one lightweight container per backend. Three location blocks sit behind the same NGINX instance, all proxying to the *same* backend pool:

| Path | Mechanism | Who rewrites it |
|---|---|---|
| `/rr`  | Unweighted round robin | Nobody -- generated once, static for the life of the container. The baseline. |
| `/aco` | Weighted round robin | The ACO module, every sampling window |
| `/mc`  | Weighted round robin | The Markov module, every sampling window |

All three are the *same* underlying NGINX mechanism (weighted round robin) -- the only difference between `/aco` and `/mc` is who computes the weights and how. Every sampling window, each algorithm reads **its own** access log (a closed feedback loop: the traffic distribution the algorithm chose is exactly what feeds its next decision), computes each backend's mean response time (TTFB, via NGINX's `$upstream_header_time`), hands that to its algorithm module, gets back an integer weight (1-100) per backend, writes it to its own upstream conf, and reloads NGINX gracefully. If a backend gets zero real traffic in a window (common once an algorithm concentrates its weight elsewhere), the sampler falls back to a single direct probe of that backend so the algorithm always has at least one observation to work with.

Before any of this starts, the controller validates every backend is reachable (fail-fast, with retries -- so Docker start-up ordering doesn't cause a false failure) and runs one full priming pass so `/aco` and `/mc` start from algorithm-derived weights on their very first proxied request, not the equal-weight placeholder.

## The algorithms

- **Round robin** -- no learning, no module. The control everything else is measured against.
- **ACO** -- one pheromone value per backend. Every window: evaporate all of them by a configurable rate, then deposit an amount inversely proportional to that backend's latency (faster backend, bigger deposit). Weight is read off the pheromone table relative to its current max. This gives ACO *momentum*: it's slow to forget, so it stays stable but lags behind sudden latency shifts.
- **Markov Chain** -- a transition matrix rebuilt completely from scratch every window, purely from that window's latency observations (no self-transitions, so every window models moving to a different, faster backend). Weight is the matrix's stationary distribution (computed via power iteration), scaled directly by 100. This makes Markov genuinely *memoryless*: no state carries over between windows, so it reacts fully and immediately to whatever just happened, at the cost of being noisier.

In testing, this contrast shows up concretely: given the *identical* underlying latency data in the same window, ACO tends to lock onto a favorite backend and hold it (its leader often near weight 100, clearly separated from the rest), while Markov's weights stay much flatter and shift around more, since it never accumulates confidence the way ACO does. Neither algorithm "wins" outright -- the interesting question is under which conditions each one wins.

## Backend pool (default, full mode)

Defined in `upstream-hosts.txt` (the single source of truth -- nothing in the codebase hardcodes a backend count; scaling to 10 or 25 is just adding lines). The default 5 backends have deliberately overlapping latency ranges, so no single one is always best:

| Backend | Latency range | Character |
|---|---|---|
| `backend-1` | 10-20ms | fast, tight, reliable |
| `backend-2` | 5-40ms | fast floor, high variance |
| `backend-3` | 12-18ms | fast, very tight |
| `backend-4` | 15-60ms | medium baseline, moderate variance |
| `backend-5` | 30-80ms | slow baseline, high variance |

Every backend also runs an internal staircase degradation cycle -- baseline -> +100ms -> +250ms -> reset -- on its own timer, independent of the other backends, reported via an `X-Degradation-State: 0|1|2` response header that lands directly in the access logs.

## Timing model

Everything derives from one base unit, `--tick` (seconds) -- nothing is hardcoded:

| Parameter | Multiplier | Default (60s tick) | Smoke test (5s tick) |
|---|---|---|---|
| Sampling window | 1x | 60s | 5s |
| Degradation cycle -- fast backends | 2x | 120s | 10s |
| Degradation cycle -- medium backends | 4x | 240s | 20s |
| Degradation cycle -- slow backends | 6x | 360s | 30s |
| Staircase step duration | 1x | 60s | 5s |

## Data & where it lands

Everything is written to plain host directories (bind mounts, not opaque Docker volumes) -- `ls`/`cat` them directly, no `docker exec` needed:

- **`./logs/{rr,aco,mc}.access.log`** -- the experimental evidence. One line per proxied request: `timestamp(ms) | request_path | backend | response_time | header_time(TTFB) | http_status | degradation_state`.
- **`./logs/{aco,mc}.weights.csv`** -- the applied integer weight per backend, per sampling window, wide-form (`timestamp_ms, timestamp_iso, backend-1, backend-2, ...`) so a line chart (x=time, y=weight, one line per backend) is a direct plot away.
- **`./logs/runs.log`** -- one JSON line per traffic-generator run: start/end timestamps, tick, rps, planned vs. actual duration, and each algorithm's config-change count. Multiple runs append to the same log files; `runs.log`'s timestamps are how an individual run gets isolated later.
- **`./logs/analysis/`** -- Phase 4's output for each analyzed run: PNG charts (TTFB over time with a degradation overlay, per-backend selection frequency) and `runN_stats_report.txt`, a TTFB mean/median/p90/p95/p99 comparison of `aco`/`mc` against the `rr` control, including a Mann-Whitney U p-value and bootstrap confidence intervals -- not just deltas, an actual significance test. See `analysis/README.md`.
- **`./nginx-conf/`** -- the generated NGINX confs themselves, if you want to see exactly what's live at any moment.

## Project status

- **Phase 1** (scaffolding: validation, config generation, priming, traffic generator, plumbing) -- done.
- **Phase 2** (ACO) -- done.
- **Phase 3** (Markov Chain, all three paths running simultaneously) -- done.
- **Phase 4** (analysis tooling in `analysis/`) -- done.

## Quick start

```sh
docker compose -f docker-compose.full.yml up --build -d
```

This validates backends, generates NGINX config, primes `/aco` and `/mc` with equal weights, and starts the per-algorithm sampling loops automatically -- no further setup needed. It does **not** send any traffic on its own. Trigger an experiment run manually, against the already-running container:

```sh
docker exec -it $(docker compose -f docker-compose.full.yml ps -q controller) \
  python3 traffic_generator.py --tick 5 --rps 5 --duration 4   # smoke test: 5s tick, ~20s total
```

Run it again (with different `--tick`/`--rps`/`--duration`) as many times as you like against the same container -- each run appends its own record to `runs.log`. See `AGENT.md` for why setup and traffic generation are split this way.

Run these one at a time, not concurrently -- two overlapping `traffic_generator.py` invocations would collide on the same run index and mix their traffic together in the same access logs with no way to tell them apart afterward.

Each run automatically triggers analysis at the end -- a stdout summary table, PNG charts, and a text stats report (TTFB mean/median/p90/p95/p99 for `aco`/`mc` vs. the `rr` control, with a Mann-Whitney p-value and bootstrap confidence intervals -- see `analysis/README.md`), all under `./logs/analysis/`. To analyze a run again later, or a different run than the most recent one:

```sh
python3 analysis/analyze.py --run 3          # from the host, against ./logs
```

**On short smoke-test runs, expect the stats report to say "not statistically significant" everywhere** -- a few thousand requests over tens of seconds usually isn't enough to separate real algorithm differences from noise, especially against backends with deliberately overlapping latency ranges. That's the correct, expected answer at this scale, not a sign anything is broken -- see `analysis/README.md`.

Points at external backends instead of building local ones:

```sh
# edit upstream-hosts.txt to list reachable external hosts/IPs first
docker compose -f docker-compose.controller.yml up --build -d
```

Only run one of these two Compose files at a time from this directory -- they share a Docker Compose project name (and volumes) by design, so bring one down (`docker compose -f <file> down`) before starting the other.

## Choosing `--rps`

`--rps 5` (the default) is a fine smoke test but sends too little traffic per window to reliably exercise every backend -- expect frequent fallback-probe log lines at that rate. `--rps 500` is a good general-purpose starting point for a real run: on a MacBook Air M4 (24GB RAM) it kept the controller container around 75-79% of one core with no errors.

The real ceiling is hardware-dependent, so treat 500 as a starting point, not a hard number -- push `--rps` up and watch `docker stats` alongside the per-algorithm error logs (`./logs/{rr,aco,mc}.error.log`) if you want to find this machine's actual limit:

- **CPU headroom**: the controller container (NGINX + the traffic generator + the sampler loops) is deliberately single-core-bound (`worker_processes 1`, kept for reproducible round-robin measurement -- see `AGENT.md`), so it's the first thing to saturate, well before the backend containers break a sweat. On the M4 machine above, ~650rps/path held at 91-95% CPU with zero errors; 800rps/path pushed past 100% (spilling onto a second OS-scheduled core) and started tripping NGINX's `worker_connections` limit (`1024 worker_connections are not enough...` in the error logs) -- a real overload signal, not just "busy."
- **A `worker_connections` warning in any `*.error.log` means you've gone past this machine's ceiling** -- drop `--rps` back down. Slower machines will hit both limits earlier than 500; faster ones may comfortably exceed 650.
