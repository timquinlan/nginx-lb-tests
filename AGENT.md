⚠️ This file is committed to a public git repo. Never store secrets, credentials, API keys, environment-specific paths, or sensitive data here. Those belong in `.env` files excluded from git.

# upstream-rl — Architecture & Design Notes

Phase 1 (scaffolding and plumbing) only. See `design-docs/upstream-rl.md` (not part of this repo) for the full project prompt. This file tracks the decisions actually made while building it, and the tradeoffs behind them.

## What's running, and where

Everything runs in Docker Compose. There are two Compose files:

- `docker-compose.full.yml` — builds and starts 5 backend containers plus the controller. Local dev / self-contained experiments.
- `docker-compose.controller.yml` — starts only the controller, pointed at external hosts listed in `upstream-hosts.txt`.

The **controller container runs NGINX itself** — there is no separate NGINX container. The controller's `entrypoint.py` validates backends, generates NGINX config, starts NGINX, primes algorithm state, and starts the per-algorithm sampling loops, all automatically on `docker compose up`. It does **not** run the traffic generator — that's a separate, manually-triggered step (see below).

## Language: Python

Controller and backend containers are both Python (stdlib-heavy, no frameworks). Chosen for fastest iteration on the ACO/Markov math and to share idioms with the Phase 4 analysis tooling (also Python). Tradeoff accepted: larger process footprint than a Go binary, which matters more once this moves toward an actual edge-device deployment — revisit then, not now.

## Entrypoint / orchestration model

`controller/entrypoint.py` runs, in order:

1. `validate_backends.py` logic — ping every host in `upstream-hosts.txt` with retries; exit non-zero on failure so `docker compose up` reports the container as failed.
2. `nginx/generate_config.py` — write `rr.upstream.conf` (static, unweighted), `aco.upstream.conf`/`mc.upstream.conf` (equal-weight placeholders), and the main server conf.
3. Start NGINX as a background child process (`nginx -g "daemon off;"`).
4. `sampler.py`'s priming run: probe every backend directly (bypassing NGINX — there's no traffic yet), compute initial weights, write them via `config_writer.py`, which triggers `nginx -s reload`.
5. Start the per-algorithm sampling loops (background threads, one per dynamic algorithm) — these run continuously for the life of the container, rewriting weights and reloading NGINX every sampling window.
6. The entrypoint process then **supervises** NGINX: it waits on NGINX's child process and forwards `SIGTERM`/`SIGINT` to it, exiting with NGINX's exit code when it stops.

**NGINX is supervised, not `exec`'d.** An earlier version of this design said the entrypoint would `exec` into NGINX as the final step (making it literally PID 1). That doesn't work: NGINX has to already be running by step 3 so step 4 can `nginx -s reload` it. A second `exec nginx` at the end would either fail (port already bound) or start a conflicting second instance. Instead the wrapper stays alive as long as NGINX does and mirrors its exit code — what actually matters for Docker's health/lifecycle model, without the literal PID-1 mechanics.

**The traffic generator is invoked manually, per run:**

```sh
docker exec <controller-container> python3 traffic_generator.py --tick 60 --rps 5 --duration 10
```

This is why `runs.log` isolates multiple runs by start/end timestamp — the setup above happens once per container lifecycle; the operator can invoke additional traffic-generator runs against the same primed, continuously-adapting NGINX without restarting anything.

## NGINX layout

- Experiment traffic (`/rr`, `/aco`, `/mc`) is served on a **separate port from NGINX's stock default server** (`NGINX_EXPERIMENT_PORT`, default `8080`). The base image ships `conf.d/default.conf` — a `server { listen 80 default_server; }` block. Adding our own `listen 80` server would either conflict (duplicate default server) or silently never be reached (the shipped default wins). A separate port sidesteps this and leaves port 80's default location as a free, undisturbed liveness check.
- The controller's own generated confs (`rr`/`aco`/`mc` upstream blocks + main server block) do **not** live directly in `/etc/nginx/conf.d`. A volume mounted straight onto `conf.d` would overlay — and hide — whatever the base image put there (`default.conf` included) the first time the mount is used. Instead they live in `CONF_DIR` (default `/var/lib/upstream-rl/nginx-conf.d`, bind-mounted from `./nginx-conf` on the host), and `generate_config.py` drops one small file *outside* that bind mount, directly into the real (image-backed) `conf.d`, that just does `include /var/lib/upstream-rl/nginx-conf.d/*.conf;`. Cheap to regenerate identically every startup; never touches `default.conf`.
- DNS resolver directive is chosen by `DEPLOY_MODE`: `127.0.0.11 ipv6=off` (Docker embedded DNS) for `full`, `8.8.8.8` (public resolver) for `external`. Note this directive only actually governs re-resolution for *variable*-based `proxy_pass` targets; our upstream blocks use static `server host:port;` entries, which NGINX resolves once at config-load/reload time via the normal system resolver regardless of the `resolver` directive. In practice this doesn't matter for `aco`/`mc` (reloaded every sampling window, so DNS stays fresh as a side effect) but does mean `rr.upstream.conf` — generated once, never reloaded — would keep routing to a stale IP if a full-mode backend container were recreated (not just restarted) mid-run. Not a concern under the default flow; worth knowing if that assumption changes.

## Log format and the `$upstream_addr` gotcha

Log format: `$msec | $uri | $upstream_addr | $upstream_response_time | $upstream_header_time | $status | $sent_http_x_degradation_state`.

- `$msec` is NGINX's only source of millisecond-precision timestamps — it's seconds-since-epoch with a millisecond decimal (e.g. `1721923200.123`), not a raw integer millisecond count. `$upstream_response_time` / `$upstream_header_time` are the same shape (e.g. `0.014`), despite this doc's field names using an `_ms` suffix for readability elsewhere. Multiply by 1000 if an integer ms value is wanted.
- **`$upstream_addr` is a resolved `ip:port`, never the original hostname.** `sampler.py` has to independently resolve each host in `upstream-hosts.txt` and build an `ip:port -> host` map (`common.resolve_ip_to_host_map`) before it can attribute access-log lines back to a named backend. This is re-derived at the start of every sampling window.

## Two data streams (refined)

- **Stream 1 (operational, mostly not analyzed):** algorithm internal state (raw pheromone/transition-matrix values), fallback-probe notices — stderr only, not a deliverable. **One exception, added after Phase 3**: the *applied integer weight* per backend per window is also written to `{algo}.weights.csv` (see "Per-window weight history" below) specifically so Phase 4 can use it. Everything else in Stream 1 stays stderr-only.
- **Stream 2 (the experimental evidence):** NGINX access logs + `runs.log`, on the `./logs` host directory (bind-mounted, not a Docker-managed named volume — see "Bind mounts, not named volumes" below). This remains the primary input to Phase 4 (TTFB, selection frequency, etc.) — the weight history is supplementary detail explaining *why* the selection frequency looks the way it does, not a replacement for it.

## Per-window weight history

`config_writer.apply_weights()` appends every window's applied weights to `{algo}.weights.csv` (`aco.weights.csv`, `mc.weights.csv`, separate file per algorithm) — unconditionally, whether or not the weight actually changed from the prior window, so a time series has no gaps to fill in. Priming's initial weights land as the first row for free, since priming calls `apply_weights()` the same as every later window.

**Wide-form CSV**: `timestamp_ms, timestamp_iso, <one column per backend>` — one row per window. Chosen specifically so a line chart (x=time, y=weight, one line per backend) is a direct `df.set_index('timestamp_ms').plot()` away with no pivot step first — long-form (`timestamp, backend, weight`, one row per backend per window) was tried first but rejected once the actual goal (a multi-line chart) was stated plainly, since long-form needs a reshape before you get that chart. Column order is fixed to the canonical `hosts` list from `upstream-hosts.txt`, not whatever order a given algorithm's internal dict happens to iterate that window (observed to vary window-to-window for Markov, since it depends on log-parsing order) — a wide CSV's columns have to stay stable for the life of the file. Both timestamp forms are written: epoch ms for precise sorting/joins against the access logs (which use `$msec`), ISO for a human to eyeball directly.

**Tradeoff accepted, deliberately not engineered around:** if the backend pool in `upstream-hosts.txt` changes between container restarts that both append to the same (persistent, bind-mounted) `./logs`, the column set won't match the original header partway through the file. Expectation is to clear or rename `./logs` before a run against a genuinely different backend pool — the same thing you'd want for a clean comparison anyway, not a new burden this format introduces.

Deliberately just the applied integer weight (what's actually in the `.conf` file), not the raw pheromone level or stationary probability behind it — keeps the file minimal and directly answers "what was NGINX actually doing," which is what Phase 4 needs it for.

## Bind mounts, not named volumes

`./logs` and `./nginx-conf` (repo-root directories, gitignored) are bind-mounted into the container rather than using Docker named volumes. Named volumes are opaque — reaching the data means `docker exec`/`docker cp` every time. Since this whole project's point is inspecting the experiment data (and Phase 4's analysis scripts are meant to be runnable/adaptable directly), plain host directories you can `ls`/`cat`/point a local Python script at are more useful here. Both Compose files bind-mount the same host paths by design (same "switch modes without losing data" property named volumes would have given, without the opacity).

## Phase 1 algorithm stub

`controller/algorithms/stub.py`'s `EqualWeightStub` was wired to both `/aco` and `/mc` in Phase 1, so the sampling/config-writer/change-counter plumbing was exercisable end-to-end before any ACO or Markov math existed. Left in the tree, unwired, as a minimal reference implementation of the `Algorithm` interface — Phase 3 replaced it on `/mc` with the real Markov module.

## Phase 2: ACO

`controller/algorithms/aco.py`'s `AntColonyOptimization` is now wired to `/aco` in `sampler.py`. One pheromone float per backend: every window, evaporate all of them by `ACO_EVAPORATION_RATE` (default `0.1`, i.e. retain 90%), then add `ACO_DEPOSIT_CONSTANT / latency_ms` (default constant `10.0`) — lower latency deposits more, satisfying "higher latency = less reinforcement." Weights are read off the pheromone table **relative to its current max**, not normalized to sum to 100: `weight = clamp(100 * pheromone / max(pheromone.values()))`. Sum-based normalization gets coarser as backend count grows (each share shrinks toward `1/N`); max-relative scaling keeps the top backend at ~100 regardless of pool size, so it doesn't degrade at higher backend counts (see Scaling).

Both constants are overridable via environment variables (`ACO_EVAPORATION_RATE`, `ACO_DEPOSIT_CONSTANT`) and exposed in both Compose files the same way `TICK_SECONDS` is.

**Verified with a live 20-tick (100s) run at a 5s smoke-test tick:** priming immediately separated weights by observed latency (fastest backend → 100, slowest → 71 on one run); the change counter climbed continuously (26 changes over 22 windows) rather than flatlining, confirming ongoing learning rather than early convergence; `/aco`'s backend-selection distribution diverged from `/rr`'s and skewed toward the lower-latency backends. The change counter climbing on *every* window is expected, not a bug: the staircase degradation cycles are shorter than several sampling windows, so the "true" latency ranking itself keeps shifting, and ACO (by design) never fully settles while that's happening.

## Phase 3: Markov Chain

`controller/algorithms/markov.py`'s `MarkovChain` is now wired to `/mc` in `sampler.py`, running simultaneously with ACO on `/aco`. Genuinely memoryless: `update()` holds no instance state at all between calls (contrast `AntColonyOptimization.__init__`'s persisted `self.pheromone`) — the transition matrix is rebuilt entirely fresh from that window's `observations` dict every time.

**Transition matrix construction:** `score[h] = 1 / latency_ms[h]` (lower latency, higher score), and row `i`'s outgoing probabilities are `score[j] / sum(score[k] for k != i)` for every `j != i`, with `P[i][i] = 0` — no self-transitions, so every window models moving to a (possibly different) backend, weighted toward whichever others were faster. Excluding `i` from its own row's normalization denominator is what makes each row genuinely depend on state `i` (i.e. an actual transition matrix, not just the same preference vector copy-pasted into every row, which would trivialize "transition matrix" down to "normalized inverse-latency vector with an unnecessary square-matrix wrapper").

**Stationary distribution** is computed by plain power iteration (repeated `π ← πP` from a uniform start, up to 200 iterations or until the L1 change drops below `1e-9`) — no need for `numpy`/eigen-decomposition at this backend-count scale, and it's the standard textbook method regardless of scale.

**Weight conversion is direct scaling, not max-relative like ACO:** `weight = clamp(100 * stationary_probability)`. This is deliberately different from ACO's `100 * pheromone / max(pheromone)` — a stationary distribution is already a normalized proportion-of-time-in-each-state, so scaling it straight by 100 carries that proportion directly into the weight ratios; pheromone is an unbounded accumulator that only means something relative to the current leader. Same underlying pattern (turn a per-backend score into an NGINX weight), different scaling rule because the two scores have different shapes.

**Verified with a live 20-tick (100s) run, all three paths running simultaneously:** priming produced visibly different weight distributions for ACO vs. Markov from the *same* underlying latency observations (one run: ACO gave the slowest backend a weight of 41, Markov gave it 2) — concretely demonstrating ACO's momentum vs. Markov's memoryless responsiveness described in the design doc. Both change counters climbed on nearly every one of 20 windows (23/23) — Markov's lack of any smoothing makes it at least as noisy as ACO here, arguably more so, matching "adapts quickly... noisier." All three access logs (`rr`/`aco`/`mc`) landed at exactly 501 lines each (500 requests + the traffic generator's immediate first-fire), confirming genuinely concurrent traffic across all three paths, and `runs.log` recorded both `aco` and `mc` change counts in one record.

**Noticed after Phase 3:** ACO's weights top out near 100 (max-relative scaling) while Markov's top out in the 30s (direct scaling of an already-normalized stationary distribution) — see "Weight conversion" above. This is intentional (each is the natively-correct scaling for its own algorithm's underlying quantity) but makes head-to-head numeric comparison between the two awkward. Decided, deferred to Phase 4:
- An exact-match counter between ACO's and MC's per-window confs was considered and rejected: given the scale mismatch alone, it would read zero from window 1 onward regardless of whether the algorithms actually agree or disagree — it can't distinguish "behaving similarly" from "behaving differently," so it doesn't test what it's meant to test.
- The real comparison (which specific metric TBD — candidates: top-pick agreement per window, rank correlation, or cosine/L1 distance between the two weight vectors normalized to sum to 1) belongs in Phase 4, computed post-hoc from the logged access-log data, not live in `sampler.py` — the two sampling loops are independent threads that can drift slightly out of phase, so bucketing by wall-clock time after the fact is more robust than trying to line up "iteration N of ACO" with "iteration N of MC" live.
- The actual weights written to `aco.upstream.conf`/`mc.upstream.conf` stay on their native scales (no change) — Phase 4 adds a separate, comparison-only normalized view (both rescaled to sum to 1) rather than changing what's live.

### NGINX `worker_processes` pinned to 1 (bug found verifying Phase 2)

While checking `/rr`'s backend-selection distribution as an "it should be ~uniform" sanity check, it came back meaningfully skewed (178 vs 57 requests across backends, in a 501-request run) even though `/rr` is plain unweighted round robin that NGINX never touches. Cause: the base image's `worker_processes auto` had started 10 workers (one per visible CPU core on this machine), and NGINX's round-robin state — weighted or not — is kept independently *per worker process*. Each worker balances correctly on its own, but the aggregate across 10 independent counters at only ~500 total requests looked skewed. This affects `/aco` and `/mc` identically, not just `/rr` — it's a general noise floor from the process model, not something biasing one path over another.

First attempt at a fix — `nginx -g "daemon off; worker_processes 1;"` — crashed the container outright: `worker_processes` is already set in the base image's `nginx.conf`, and NGINX refuses a directive supplied both via `-g` and in the config file ("duplicate directive", exit code 1). Actual fix: `entrypoint.py`'s `pin_worker_processes()` patches that one `worker_processes auto;` line in `/etc/nginx/nginx.conf` in place (regex substitution) before NGINX starts, since the directive can't be set from our generated `conf.d` includes either (main-context only, those are `http{}`-scoped). Trades multi-core concurrency for a single, precisely interpretable round-robin/weighted state, matching this project's stated priority (reproducible measurement over raw throughput).

### Traffic generator's thread pool auto-scales with `--rps` (bug found load-testing)

`traffic_generator.py`'s per-path `ThreadPoolExecutor` was originally a fixed `MAX_WORKERS_PER_PATH = 20`, sized around the 5rps default. Verified fine up to 25rps (each ~5s window logged ~120-130 real access-log lines, matching 25rps × 5s almost exactly — no fallback probes needed once traffic was actually flowing). At 500rps the fixed pool would have silently become the bottleneck: Little's Law puts the sustainable ceiling for 20 workers at roughly 130-270rps given backend latencies up to ~330ms worst-case (max latency range + top degradation step), so `--rps 500` would have quietly achieved something well under 500 with zero error or warning — `ThreadPoolExecutor.submit()` just queues faster than it drains. Fixed by sizing the pool from the requested rps instead of a fixed constant: `workers_per_path = max(20, int(rps * 0.5))` (500rps -> 250 workers/path), logged at run start so the actual pool size is visible, not silently chosen.

**Load-tested live at 500rps/path (1500rps aggregate) with the fix in place:** the controller container (NGINX + the traffic generator + the sampler loops, all sharing one container) held at ~75-79% of one core throughout a 60s run; each backend container stayed at 9-14% CPU, 12-17MB RAM — comfortably idle. Confirms the controller, not the backends, is the bottleneck at high throughput, and that bottleneck is single-core-bound by design (NGINX pinned to `worker_processes 1` for reproducibility, and the Python traffic generator is GIL-bound regardless of thread count) rather than a Docker resource limit — neither Compose file sets one, so there was nothing to "raise." Explored as a one-off curiosity, not adopted as a config change: throughput is explicitly out of scope for this project's actual measurement goal (TTFB / algorithm comparison), and raising NGINX's `worker_processes` to use more cores would reintroduce the exact per-worker round-robin skew the fix above removed. Decision: leave `worker_processes 1`, keep the thread-pool auto-scaling fix (that one's a straightforward correctness improvement independent of the throughput question).

## `runs.log` format

Chosen as JSON Lines (one JSON object per run) rather than the pipe-delimited format used for NGINX access logs. The access log format is fixed-width by NGINX's `log_format` directive; `runs.log` has no such constraint, and JSONL lets a future algorithm's change-count key show up in new lines without breaking any code that reads older ones — matches "adding a new algorithm should be straightforward" elsewhere in this project.

## Operational gotchas found while testing Phase 1

- **Both Compose files share a project name.** Docker Compose derives the project name from the directory by default, not the `-f` filename — so `docker-compose.full.yml` and `docker-compose.controller.yml` run from the same directory land in the *same* project/volume namespace. That's intentional (see "same named volumes" above), but it means running `docker compose -f docker-compose.controller.yml up` while the full-mode stack's `controller` service is still up does **not** start a fresh controller-only container — Compose matches by project+service name and just reattaches to the one already running. Always `docker compose -f <the other file> down` before switching modes from the same directory.
- **The actionable validation-failure message has one source of truth.** `validate_backends.py`'s detailed failure report (which hosts, the full checked list, which file to edit, which Compose file to use) originally only printed from that script's own `main()`. `entrypoint.py` and `sampler.py` both call the lower-level `validate()` function directly and, before this was caught, only logged a bare Python list on failure — silently dropping the actionable message exactly where an operator would actually see it (container startup). Fixed by extracting `print_failure_report()` as a shared function all three callers use. If a new caller of `validate()` is added, it must call `print_failure_report()` on failure too, or this regresses again.

## Scaling

Nothing in `upstream-hosts.txt` parsing, NGINX config generation, or the sampler hardcodes a backend count — scaling from 5 to 10 to 25 backends in **`docker-compose.controller.yml`** (external mode) is purely editing `upstream-hosts.txt`. **`docker-compose.full.yml`** (local mode) is the exception: each locally-built backend is its own Compose service (so its specific `LATENCY_MIN_MS`/`LATENCY_MAX_MS`/`DEGRADATION_MULTIPLIER` tuning can differ), so adding local backends beyond the default 5 means adding both a service block here and a line in `upstream-hosts.txt`. This is inherent to Compose needing an explicit service per container instance, not a shortcut taken in the controller logic.
