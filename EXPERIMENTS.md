⚠️ This file is committed to a public git repo. Never store secrets, credentials, API keys, environment-specific paths, or sensitive data here.

# Experiments

This is the forward-looking companion to `FINDINGS.md`: not what the experiment *showed*, but what's worth trying next, which knobs to push, and the math/reasoning to design each experiment well before spending run time on it. `AGENT.md` is the development log (what was built and why); `README.md` is the how-to-run-it guide. This file is the ideas backlog and the reasoning behind it, updated as ideas move from "considered" to "built and tested" (at which point the result belongs in `FINDINGS.md`, not here).

## Algorithm tuning

**`ACO_EVAPORATION_RATE`/`ACO_DEPOSIT_CONSTANT` have now been swept -- see `FINDINGS.md`, "Tuning `aco_wrr`."** Raising evaporation to 0.3 (default 0.1) cut the mean gap to `leastconn` by roughly two-thirds and is now the project default; raising or lowering the deposit constant made things worse in every direction tried. What's left below is what's still open, not what's already been done.

**Not yet a knob:** `_weights_from_pheromone()` scales linearly relative to the current max pheromone. If the top two backends' pheromone levels are close, aco_wrr/aco_lc spread traffic almost evenly between them -- diluting their own advantage even when they've correctly identified a leader. A sharper transform (squaring the ratio, or a softmax with a temperature parameter) would make them commit harder once they have a lead, closer to leastconn's all-or-nothing style. Probably the highest-leverage remaining lever, but needs a real code change, not just an env var -- worth trying next given how much the evaporation-rate change alone bought.

**MC -- no real behavioral knob today.** `_build_transition_matrix` uses plain `1/latency_ms` as its score with no sharpness parameter; `STATIONARY_ITERATIONS`/`STATIONARY_TOLERANCE` are numerical-precision settings, not personality knobs (200 iterations converges near-instantly for 5 states regardless of these values). The natural addition: an exponent, `1/latency_ms^k`, where `k=1` is today's gentle behavior and `k>1` sharpens the stationary distribution toward the fastest backend -- the same "commit harder" lever as aco's weight-scaling idea above. Right now mc_wrr/mc_lc have no way to be more or less aggressive; they just are what they are.

**leastconn isn't tunable from this project's side at all.** It's NGINX's own built-in mechanism with no exposed parameters (NGINX Plus has a `slow_start` option; open-source NGINX, which this project uses, doesn't). Any tuning experiment aimed at `leastconn` directly is about whether aco_wrr/mc_wrr can close the gap on it, not the reverse -- but see `aco_lc`/`mc_lc` below for a way around that limit rather than through it.

**`aco_lc` (`least_conn` + ACO-set weights) tuning: a first pass found no clean winner, still open.** `aco_lc` currently reuses `/aco_wrr`'s exact tuning (`ACO_EVAPORATION_RATE`/`ACO_DEPOSIT_CONSTANT`, both read as shared module-level constants in `controller/algorithms/aco.py` -- see `AGENT.md`), inherited rather than independently chosen -- plausible it wants something different, since `aco_lc` isn't the sole reaction mechanism the way `aco_wrr` alone is (`least_conn`'s live connection count already supplies the zero-lag correction it might not need to earn through weight-tuning the same way `aco_wrr` does).

One evaporation-rate variant each side of the current default (`--rps 80`, single 10-minute run per variant, `aco_lc` vs `leastconn` gap):

| Evaporation | mean | median | p90 | p95 | p99 |
|---|---|---|---|---|---|
| 0.15 (slower/more momentum) | -20.6ms (-5.7%) | -20.0ms (-5.8%) | -61.0ms (-10.6%) | -38.0ms (-5.9%) | -19.0ms (-2.5%) |
| **0.3 (current default)** | -23.9ms (-6.6%) | **-21.0ms (-6.2%)** | **-81.0ms (-14.2%)** | **-51.0ms (-8.0%)** | -22.0ms (-3.0%) |
| 0.5 (faster/less momentum) | **-25.4ms (-6.7%)** | -17.0ms (-4.8%) | -54.0ms (-9.2%) | -40.0ms (-6.2%) | **-40.0ms (-5.2%)** |

0.15 was strictly worse than the current default on every stat -- consistent with `aco_wrr`'s own sweep, where less momentum (higher evaporation) helped. 0.5 was a mixed result rather than a clean win: better mean and notably better p99, worse median/p90/p95 -- not enough to displace 0.3 as the default off one run each, but different enough from the current default's profile to be worth a proper reproducibility check (several repeated runs, the same way the `aco_wrr` tuning result was confirmed) before drawing any real conclusion. Not run yet. If it holds up, worth also testing whether it changes with `ACO_DEPOSIT_CONSTANT` held fixed vs varied together.

Also worth trying now that `mc_lc` exists alongside `aco_lc` (see `FINDINGS.md`): whether the same "does the *_lc variant want different tuning than its *_wrr sibling" question applies to MC too, even though MC has no tunable knob yet (see "MC -- no real behavioral knob today" above) -- once one exists, it's the same open question for both algorithm pairs, not just ACO's.

Testing the ACO side of this properly (isolating `aco_lc`'s own tuning from `/aco_wrr`'s) needs `ACO_LC_EVAPORATION_RATE`/`ACO_LC_DEPOSIT_CONSTANT` env vars and turning `aco.py`'s two module-level constants into per-instance constructor args -- not done as of this writing. Today, any env var change moves both `/aco_wrr` and `/aco_lc` together, which is fine for evaluating `aco_lc` vs `leastconn` in isolation (which is all the sweep above needed) but would conflate the two if a future experiment also cared about `/aco_wrr`'s own numbers from the same run.

## Run sizing: duration, volume, and sample size

Findings from the 2026-07-29 session, condensed into guidance for designing future runs:

- **`BOOTSTRAP_MAX_SAMPLE` is 50000** (raised from 5000 to 150000 on 2026-07-29, brought back down to 50000 on 2026-07-30 as a deliberate CPU-cost/precision middle ground -- see `analysis/analyze.py`). At the old 5000 cap, p99 bootstrap CIs were 37-45ms wide with at least one real missed-significance case; uncapped at ~144k they came back 6-9ms wide with identical point estimates. The 5000 cap wasn't "good enough, just faster" -- it was materially widening reported uncertainty. 50000 is still 10x the sample size that produced the tight CIs, so it isn't expected to meaningfully regress precision, and for the current default run size (24000 requests/path at 40rps/10min) the cap doesn't even engage -- the full sample is already under it.
- **Once total request volume is fixed, duration doesn't matter much for p99 significance.** A same-total-volume series (40rps/60min, 80rps/30min, 160rps/15min, 240rps/10min -- all 144k requests/path) hit p99 significance in 15 of 16 comparisons regardless of whether that volume came from 10 minutes or a full hour. What actually needs to be large is total request count, not wall-clock exposure -- the earlier 50rps/10min run's p99 problems (only ~30k requests) were a volume problem, not a duration problem per se.
- **But short runs are vulnerable to a *different*, real confound: backend-condition luck.** Every backend's degradation offset is a real-time, independently-drawn random walk (dwell means 40-120s per backend). A short run only samples a handful of independent dwell segments per backend (as few as 2, observed directly in a 5-minute run), so its time-weighted average condition can land meaningfully higher or lower than a longer run's, purely by chance -- this showed up as a ~36-38ms swing in every algorithm's mean TTFB simultaneously between two otherwise-identical 5-minute and 10-minute runs. This is a *different* mechanism from the bootstrap-CI/p99 issue above: it affects absolute levels (and cross-run comparisons), not the significance of within-run algorithm-vs-`rr_control` deltas. Rankings *within* a short run stay trustworthy; comparing absolute levels *across* separately-timed runs does not, without accounting for this.
- **How to check for it:** a scratchpad diagnostic (2026-07-29 session, not yet in the repo) reads `degradation-backend-N.log`, slices by a run's `[start_ts_ms, end_ts_ms]` window from `runs.log`, and computes the time-weighted mean offset + transition count per backend for that window -- directly answering "did this run get a representative sample of backend conditions, or an unlucky one." Worth porting into `analysis/` if this kind of cross-run comparison becomes routine.

## Backend condition determinism (deferred)

For a true apples-to-apples comparison across separately-timed runs (e.g. the sequential single-path isolation runs from 2026-07-29), the backend-luck confound above can't be fixed by more requests -- it needs the *same* degradation timeline replayed across runs, which needs a small code change (deferred, not built):

- **Seeded RNG** (simpler): give each backend's degradation draws their own `random.Random(seed)` instance via a `DEGRADATION_SEED` env var. Since the offset/dwell schedule is a pure function of elapsed wall-clock time (not request order), this makes the transition *sequence* reproducible run over run.
- **Recorded-schedule replay** (more work, more precise): read a `(elapsed_seconds, offset_ms, dwell_seconds)` schedule from a file instead of drawing live -- reuses the existing `DEGRADATION_LOG_PATH` format almost verbatim, and lets you hand-author specific test patterns rather than accepting whatever a seed produces.

Either way, getting the *same* window of the schedule replayed per run also needs the backend containers restarted between runs (reset the offset clock to t=0) -- an operational step, not more code.

