⚠️ This file is committed to a public git repo. Never store secrets, credentials, API keys, environment-specific paths, or sensitive data here.

# Experiments

This is the forward-looking companion to `FINDINGS.md`: not what the experiment *showed*, but what's worth trying next, which knobs to push, and the math/reasoning to design each experiment well before spending run time on it. `AGENT.md` is the development log (what was built and why); `README.md` is the how-to-run-it guide. This file is the ideas backlog and the reasoning behind it, updated as ideas move from "considered" to "built and tested" (at which point the result belongs in `FINDINGS.md`, not here).

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

- **`BOOTSTRAP_MAX_SAMPLE` is 50000** (raised from 5000 to 150000 on 2026-07-29, brought back down to 50000 on 2026-07-30 as a deliberate CPU-cost/precision middle ground -- see `analysis/analyze.py`). At the old 5000 cap, p99 bootstrap CIs were 37-45ms wide with at least one real missed-significance case; uncapped at ~144k they came back 6-9ms wide with identical point estimates. The 5000 cap wasn't "good enough, just faster" -- it was materially widening reported uncertainty. 50000 is still 10x the sample size that produced the tight CIs, so it isn't expected to meaningfully regress precision, and for the current default run size (24000 requests/path at 40rps/10min) the cap doesn't even engage -- the full sample is already under it.
- **Once total request volume is fixed, duration doesn't matter much for p99 significance.** A same-total-volume series (40rps/60min, 80rps/30min, 160rps/15min, 240rps/10min -- all 144k requests/path) hit p99 significance in 15 of 16 comparisons regardless of whether that volume came from 10 minutes or a full hour. What actually needs to be large is total request count, not wall-clock exposure -- the earlier 50rps/10min run's p99 problems (only ~30k requests) were a volume problem, not a duration problem per se.
- **But short runs are vulnerable to a *different*, real confound: backend-condition luck.** Every backend's degradation offset is a real-time, independently-drawn random walk (dwell means 40-120s per backend). A short run only samples a handful of independent dwell segments per backend (as few as 2, observed directly in a 5-minute run), so its time-weighted average condition can land meaningfully higher or lower than a longer run's, purely by chance -- this showed up as a ~36-38ms swing in every algorithm's mean TTFB simultaneously between two otherwise-identical 5-minute and 10-minute runs. This is a *different* mechanism from the bootstrap-CI/p99 issue above: it affects absolute levels (and cross-run comparisons), not the significance of within-run algorithm-vs-`rr` deltas. Rankings *within* a short run stay trustworthy; comparing absolute levels *across* separately-timed runs does not, without accounting for this.
- **How to check for it:** a scratchpad diagnostic (2026-07-29 session, not yet in the repo) reads `degradation-backend-N.log`, slices by a run's `[start_ts_ms, end_ts_ms]` window from `runs.log`, and computes the time-weighted mean offset + transition count per backend for that window -- directly answering "did this run get a representative sample of backend conditions, or an unlucky one." Worth porting into `analysis/` if this kind of cross-run comparison becomes routine.

## Backend condition determinism (deferred)

For a true apples-to-apples comparison across separately-timed runs (e.g. the sequential single-path isolation runs from 2026-07-29), the backend-luck confound above can't be fixed by more requests -- it needs the *same* degradation timeline replayed across runs, which needs a small code change (deferred, not built):

- **Seeded RNG** (simpler): give each backend's degradation draws their own `random.Random(seed)` instance via a `DEGRADATION_SEED` env var. Since the offset/dwell schedule is a pure function of elapsed wall-clock time (not request order), this makes the transition *sequence* reproducible run over run.
- **Recorded-schedule replay** (more work, more precise): read a `(elapsed_seconds, offset_ms, dwell_seconds)` schedule from a file instead of drawing live -- reuses the existing `DEGRADATION_LOG_PATH` format almost verbatim, and lets you hand-author specific test patterns rather than accepting whatever a seed produces.

Either way, getting the *same* window of the schedule replayed per run also needs the backend containers restarted between runs (reset the offset clock to t=0) -- an operational step, not more code.

## What NOT to move into the generator

Considered and rejected (2026-07-29): making `LATENCY_MIN_MS`/`MAX_MS` and `DEGRADATION_*` live-adjustable from the traffic generator. Rejected because these are the experiment's actual independent variables, not operational/session parameters:

1. **State interaction gets messy.** `DEGRADATION_MEAN_DWELL_SECONDS` interacts with an already-running dwell timer -- changing it mid-dwell raises real design questions (does the current dwell finish at the old cadence?) that a stateless resource-limit resize doesn't have.
2. **Reproducibility gets worse, not better.** Changing a backend's personality today requires editing `docker-compose.full.yml` and restarting -- a visible, diffable, deliberate act. A stray CLI flag on a `docker exec` invocation would let a run's real conditions silently drift from what's committed, with no trace.

Keep these knobs exactly where they are.
