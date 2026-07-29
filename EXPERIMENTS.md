⚠️ This file is committed to a public git repo. Never store secrets, credentials, API keys, environment-specific paths, or sensitive data here.

# Experiments

This is the forward-looking companion to `FINDINGS.md`: not what the experiment *showed*, but what's worth trying next, which knobs to push, and the math/reasoning to design each experiment well before spending run time on it. `AGENT.md` is the development log (what was built and why); `README.md` is the how-to-run-it guide. This file is the ideas backlog and the reasoning behind it, updated as ideas move from "considered" to "built and tested" (at which point the result belongs in `FINDINGS.md`, not here).

## Backend contention / the "many-LB" problem

**Motivation.** `least_conn`/`random two` are recommended for a single load balancer in front of a backend pool -- not for multiple independent load balancers sharing the same pool, since each one's connection-count view is blind to the others' concurrent load. This project's six upstream-selection mechanisms (`rr`/`random`/`random2`/`leastconn`/`aco`/`mc`) already structurally replicate that scenario today: each has its own NGINX `zone` (independent state, by design -- see `AGENT.md`), but all six share the same five physical backend processes in real time. `leastconn` has no idea what `random2`, `aco`, or `mc` are simultaneously doing to the same backend it just picked as "least loaded."

At today's default rps, this doesn't matter -- `docker stats` shows backends at 4-6.6% CPU with huge headroom, nowhere near contended. The open question is what happens to the close leastconn/aco/mc gap once that headroom disappears.

**Built 2026-07-29: a backend-side concurrency cap, live-adjustable from the traffic generator.**

- `backend/server.py`: `AdjustableLimiter` -- a resizable gate (counter + condition variable, since `threading.Semaphore` can't be resized in place) wrapping `_serve_default`/`_serve_static`, capping concurrent in-flight requests per backend. `limit=None` (the default) is unlimited, identical to the original unbounded-thread-per-connection behavior -- nothing changes unless something explicitly sets a limit.
- A `POST /admin/capacity {"limit": N}` endpoint on each backend, called directly (bypassing NGINX, same pattern as `sampler.py`'s `direct_probe()`), so the cap is live-adjustable **without a container restart** -- unlike `LATENCY_MIN_MS`/`DEGRADATION_*`, which stay restart-based on purpose (see "What NOT to move into the generator" below).
- `traffic_generator.py --contention {off,mild,moderate}` (default `off`): always resets every backend to unlimited first (so a prior run's cap never silently carries into a run that didn't ask for one), then for `mild`/`moderate` does one direct probe per backend (same mechanism as the priming step) to get a real current-conditions estimate of `W`, computes `N` via the Little's Law math below using the run's actual `--rps`, and pushes it to every backend before sending traffic. The resulting `N`, `ρ`, and probed `W` are written into the run record and printed in the stats report's `config:` line for every run (via `contention_summary()` in `analysis/analyze.py`), so any report is self-describing about whether/how contention was applied.
- Verified live (2026-07-29): `mild` at 40rps/6paths/5backends probed `W≈408.5ms`, computed `L≈19.6`, `N=25`; `moderate` probed `W≈498.2ms`, `L≈23.9`, `N=27`. Both produced the expected added queueing delay across all six paths in a 1-minute smoke test -- real effect, mechanism works as designed. Not yet run at real duration/volume for an actual finding (that's next).

**The math (Little's Law).** Natural, uncapped concurrency per backend:

```
λ_backend = (rps * paths) / num_backends     # arrival rate per backend, assuming even spread
L = λ_backend * W                             # W = avg response time, seconds
```

At 40rps/path (today's default), 6 paths, 5 backends, W≈0.35-0.4s: λ_backend=48rps, L≈16.8 -- matches the PIDs count observed live in `docker stats` (13-26) almost exactly.

**The cap N must stay above L, not below it**, or the system is unstable: arrivals outpace service capacity, the backlog grows without bound for as long as the run continues, and requests eventually hit the traffic generator's 10s timeout and fail outright rather than just running slow. The actual design knob is utilization, `ρ = L / N` (must stay < 1):

```
N = L / ρ_target
```

- `ρ_target ≈ 0.8` ("mild") → N ≈ L/0.8
- `ρ_target ≈ 0.9` ("moderate") → N ≈ L/0.9
- `ρ_target ≥ 1` → don't -- unstable, backlog grows forever

Pick *mild*, not extreme, contention as the target. The leastconn/aco/mc means are already very close (a handful of ms apart in recent runs) -- a little pressure should be enough to separate them without needing to break the system outright. There's also a real-world argument for staying mild: if an algorithm only falls apart under contention severe enough to require more backend compute or fewer LB instances to fix, that's not really an algorithm-choice question anymore -- it's a capacity question no LB algorithm can tune its way out of.

**If genuinely severe (N < L, unstable) contention is ever wanted**, don't run it continuously for the whole run -- pair it with a synchronized burst/pause cycle (all six traffic-generator loops pause together, not independently, or five zones keep hammering the backend while one pauses and nothing drains). Required pause length to fully drain a burst:

```
backlog_at_end_of_burst ≈ (λ − N/W) × T_burst
required_pause ≈ backlog_at_end_of_burst / (N/W)
```

Example: λ=48rps, W=0.35s, N=12 (well under L≈16.8), T_burst=55s → backlog ≈753 requests → needs ~22s to drain, not "a few seconds." If N is close to L instead (e.g. N=16), the backlog is much smaller (~127 requests) and drains in ~3s. This isn't the current plan (mild/stable contention doesn't need pausing at all), but the formula is here if severe contention is worth testing later.

**Why `random` becomes the key control once this exists.** `leastconn`/`random2` make decisions off a *signal* (local connection count) that's structurally blind to the other five zones' load on the same backend -- exactly the failure mode the "not recommended for many-LB" caveat describes. `random` has no signal at all, so it can't be fooled by invisible peer load the way a shared-but-incomplete signal can. Prediction: under real contention, `leastconn`/`random2` should degrade *relatively* more than `random`, which should be largely unaffected by this specific mechanism. This is also the reason `/random` is staying in the six-path lineup rather than being dropped (see the 2026-07-29 discussion) -- it goes from "never once flagged anything" to the specific control this experiment needs.

**Consideration for later, not acted on yet (raised 2026-07-29): `rr` may need to be excluded from contention-analysis comparisons, not just kept as a baseline.** `rr` isn't blind to *peer* load the way `leastconn`/`random2` are -- it's blind to *everything*, including the target backend's own current state. Under contention it'll keep sending its fixed share to an overloaded backend regardless, while every other algorithm (even `random`, indirectly, via `random two`'s tie-break where applicable) has at least some chance of steering away. That means `rr` could look artificially worse under contention for a reason that has nothing to do with the many-LB-blindness mechanism being tested -- it's just "has no adaptivity at all," which is true with or without contention. Worth revisiting whether `rr`-vs-`X` comparisons stay meaningful once contention is real, or whether the contention-specific analysis should lean on `random`-vs-`leastconn`/`random2` instead. Keeping `rr` in the mix for now.

## Algorithm tuning

leastconn/aco/mc have been landing within a handful of ms of each other in recent runs. Ideas for sharpening the gap, from most to least ready to try:

**ACO -- two knobs already exposed, neither varied yet this session:**
- `ACO_EVAPORATION_RATE` (default 0.1) -- how much prior pheromone survives each window. Lower = more memory/momentum (this is *why* aco pulled ahead of mc under the wider 150-600ms latency profile and at low rps -- its smoothing helps when signal is noisy or sparse, see `FINDINGS.md`). Higher = more reactive, closer to mc. Since leastconn wins partly by reacting to live state instantly, raising this slightly might close some responsiveness gap without losing all of aco's noise-smoothing edge.
- `ACO_DEPOSIT_CONSTANT` (default 10.0) -- how hard a good observation reinforces pheromone. Raising it sharpens backend separation faster.
- **Not yet a knob:** `_weights_from_pheromone()` scales linearly relative to the current max pheromone. If the top two backends' pheromone levels are close, aco spreads traffic almost evenly between them -- diluting its own advantage even when it's correctly identified a leader. A sharper transform (squaring the ratio, or a softmax with a temperature parameter) would make aco commit harder once it has a lead, closer to leastconn's all-or-nothing style. Probably the highest-leverage of the three, but needs a real code change, not just an env var.

**MC -- no real behavioral knob today.** `_build_transition_matrix` uses plain `1/latency_ms` as its score with no sharpness parameter; `STATIONARY_ITERATIONS`/`STATIONARY_TOLERANCE` are numerical-precision settings, not personality knobs (200 iterations converges near-instantly for 5 states regardless of these values). The natural addition: an exponent, `1/latency_ms^k`, where `k=1` is today's gentle behavior and `k>1` sharpens the stationary distribution toward the fastest backend -- the same "commit harder" lever as aco's weight-scaling idea above. Right now mc has no way to be more or less aggressive; it just is what it is.

**leastconn isn't tunable from this project's side at all.** It's NGINX's own built-in mechanism with no exposed parameters (NGINX Plus has a `slow_start` option; open-source NGINX, which this project uses, doesn't). Any tuning experiment here is about whether aco/mc can close the gap on leastconn, not the reverse.

## Run sizing: duration, volume, and sample size

Findings from the 2026-07-29 session, condensed into guidance for designing future runs:

- **`BOOTSTRAP_MAX_SAMPLE` is 150000** (raised from 5000, see `analysis/analyze.py`). At the old 5000 cap, p99 bootstrap CIs were 37-45ms wide with at least one real missed-significance case; uncapped at ~144k they came back 6-9ms wide with identical point estimates. The 5000 cap wasn't "good enough, just faster" -- it was materially widening reported uncertainty. Cost: ~10-11 min of CPU per full stats report at the new default, which now lands on *every* run's automatic end-of-run analysis, not just manual reruns.
- **Once total request volume is fixed, duration doesn't matter much for p99 significance.** A same-total-volume series (40rps/60min, 80rps/30min, 160rps/15min, 240rps/10min -- all 144k requests/path) hit p99 significance in 15 of 16 comparisons regardless of whether that volume came from 10 minutes or a full hour. What actually needs to be large is total request count, not wall-clock exposure -- the earlier 50rps/10min run's p99 problems (only ~30k requests) were a volume problem, not a duration problem per se.
- **But short runs are vulnerable to a *different*, real confound: backend-condition luck.** Every backend's degradation offset is a real-time, independently-drawn random walk (dwell means 40-120s per backend). A short run only samples a handful of independent dwell segments per backend (as few as 2, observed directly in a 5-minute run), so its time-weighted average condition can land meaningfully higher or lower than a longer run's, purely by chance -- this showed up as a ~36-38ms swing in every algorithm's mean TTFB simultaneously between two otherwise-identical 5-minute and 10-minute runs. This is a *different* mechanism from the bootstrap-CI/p99 issue above: it affects absolute levels (and cross-run comparisons), not the significance of within-run algorithm-vs-`rr` deltas. Rankings *within* a short run stay trustworthy; comparing absolute levels *across* separately-timed runs does not, without accounting for this.
- **How to check for it:** a scratchpad diagnostic (2026-07-29 session, not yet in the repo) reads `degradation-backend-N.log`, slices by a run's `[start_ts_ms, end_ts_ms]` window from `runs.log`, and computes the time-weighted mean offset + transition count per backend for that window -- directly answering "did this run get a representative sample of backend conditions, or an unlucky one." Worth porting into `analysis/` if this kind of cross-run comparison becomes routine.

## Backend condition determinism (deferred)

For a true apples-to-apples comparison across separately-timed runs (e.g. the sequential single-path isolation runs from 2026-07-29), the backend-luck confound above can't be fixed by more requests -- it needs the *same* degradation timeline replayed across runs, which needs a small code change (deferred, not built):

- **Seeded RNG** (simpler): give each backend's degradation draws their own `random.Random(seed)` instance via a `DEGRADATION_SEED` env var. Since the offset/dwell schedule is a pure function of elapsed wall-clock time (not request order), this makes the transition *sequence* reproducible run over run.
- **Recorded-schedule replay** (more work, more precise): read a `(elapsed_seconds, offset_ms, dwell_seconds)` schedule from a file instead of drawing live -- reuses the existing `DEGRADATION_LOG_PATH` format almost verbatim, and lets you hand-author specific test patterns rather than accepting whatever a seed produces.

Either way, getting the *same* window of the schedule replayed per run also needs the backend containers restarted between runs (reset the offset clock to t=0) -- an operational step, not more code.

## What NOT to move into the generator

Considered and rejected (2026-07-29): making `LATENCY_MIN_MS`/`MAX_MS` and `DEGRADATION_*` live-adjustable from the traffic generator, the same way the contention cap is planned to be. Rejected because these are the experiment's actual independent variables, not operational/session parameters like the contention cap:

1. **State interaction gets messy.** `DEGRADATION_MEAN_DWELL_SECONDS` interacts with an already-running dwell timer -- changing it mid-dwell raises real design questions (does the current dwell finish at the old cadence?) that a stateless resource-limit resize doesn't have.
2. **Reproducibility gets worse, not better.** Changing a backend's personality today requires editing `docker-compose.full.yml` and restarting -- a visible, diffable, deliberate act. A stray CLI flag on a `docker exec` invocation would let a run's real conditions silently drift from what's committed, with no trace.

Keep these knobs exactly where they are.

## Open items, not yet scoped

- Multiple-NGINX-node topologies (mentioned in passing, not yet elaborated here -- revisit if it comes up again).
