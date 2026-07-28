⚠️ This file is committed to a public git repo. Never store secrets, credentials, API keys, environment-specific paths, or sensitive data here.

# Findings

This is the results/conclusions writeup -- what the experiment actually showed, consolidated across every run and every axis varied. `AGENT.md` is the development log (decisions, bugs, why things are built the way they are); `README.md` is the how-to-run-it guide. This file is neither -- it's the answer to "so what did you learn."

## What was tested

Six upstream-selection mechanisms running **simultaneously**, behind the same NGINX instance, against the same backend pool, on identical live traffic:

- `rr` -- unweighted round robin (the control).
- `random` -- NGINX `random` (a validity canary, expected to always track `rr`; see below).
- `random2` -- NGINX `random two` (pick 2 at random, route to whichever has fewer active connections).
- `leastconn` -- NGINX `least_conn` (always route to fewest active connections, pool-wide).
- `aco` -- this project's Ant Colony Optimization module: pheromone-weighted, evaporates and re-deposits every sampling window, has momentum ("slow to forget").
- `mc` -- this project's Markov Chain module: transition matrix rebuilt from scratch every window, no memory carried over.

Varied across runs: traffic rate (125-500rps/path, one run pushed to a nominal 1000 -- see caveat below), sampling tick (5s/10s/20s/60s), backend count (5 vs. 3), backend latency profile (5-80ms vs. 150-600ms), and NGINX worker topology (1 worker vs. 4 workers+shared zones). See `AGENT.md` for the underlying architecture and mechanisms.

## Headline result: the ranking is stable, not fragile

Across every condition tested, the same hierarchy holds:

**`leastconn` / `random2` (live connection-state) > `aco` / `mc` (historical-learning) > `rr` / `random` (no state at all)**

| Condition varied | Result |
|---|---|
| `--rps` 125 -> 250 -> 500 (4x range, ~7.5k -> ~300k requests/path) | Identical ranking every time -- `leastconn`, `random2`, `mc`, `aco`, then `rr`/`random` tied. Reproducibility data point, not just a throughput check. |
| Tick 60s vs. 10s, full 60-minute runs (6x the sample size of the original tick table) | `leastconn`/`random2` unaffected either way (they don't depend on tick at all -- no sampling window in their mechanism). `aco`/`mc`'s edge over `rr` collapses at 60s tick (`aco`: 0/5 stats significant; `mc`: 2/5), fully recovers at 10s (5/5 for both). |
| Backend count 5 vs. 3 | `leastconn`/`random2` held steady or strengthened slightly. `aco`/`mc` both weakened; `aco` specifically flipped its median stat to non-significant -- fewer backends shrinks `rr`'s own baseline disadvantage, leaving less headroom for a learned ranking to capture. |
| Latency profile 5-80ms vs. 150-600ms | Same six-way hierarchy, but `aco` and `mc` swap places (`aco` #3, `mc` #4 at the wider profile, reversed at the narrow one) -- see "The one thing that moves" below. |
| Worker topology 1 vs. 4 workers | Correctness issue, not a ranking change: 4 workers *without* a shared `zone` skews every algorithm's selection state (more independent per-worker cycles = worse aggregate skew, not better). With `zone` added to every upstream block, skew collapses back down and the ranking is unaffected -- this was a bug fix, not an experimental axis. |
| Nominal 1000rps/path (see caveat) | Same ranking again, at yet another (if imprecise) throughput point. |

`random` vs. `rr` never came back statistically significant in any run (p=0.21-0.97 across the whole session) -- exactly as expected, since an unweighted dice roll and unweighted round robin converge to the same distribution at scale. This is the intended role of `/random`: not a peer comparison algorithm, but a validity canary. Two independently-implemented, mechanistically distinct NGINX code paths agreeing on every single run is stronger evidence the measurement harness itself is trustworthy than either result alone would be.

## Tick sensitivity: the dwell-to-window ratio, not the absolute tick value

The `--tick`/10s default wasn't picked arbitrarily -- it came from live-testing four tick values against the same 10-minute, `--rps 250` config (`DEGRADATION_MEAN_DWELL_SECONDS` at 40/80/120s, unscaled):

| Tick (window) | Ratio (fastest backend) | `aco` vs `rr` mean | `mc` vs `rr` mean |
|---|---|---|---|
| 60s | 0.67x | -0.1ms, not significant | +1.5ms, not significant |
| 20s | 2x | -4.8ms, significant | -20.1ms, significant (aco's median was not) |
| **10s** | **4x** | **-12.3ms, all 5 stats significant** | **-32.4ms, all 5 stats significant** |
| 5s | 8x | -26.4ms, all 5 stats significant | -42.2ms, all 5 stats significant |

A clean dose-response relationship: 10s is the smallest tick that gave fully-significant separation for both algorithms at these dwell settings; 60s collapses both into statistical noise. **The ratio between the sampling window and how fast the environment actually changes is what matters, not the absolute tick value** -- if `DEGRADATION_MEAN_DWELL_SECONDS` or the backend timing changes, this table needs re-deriving rather than assuming 10s still lands in the safe zone; aim for at least ~4x the sampling window on the fastest backend class as a starting point.

Reconfirmed at 60-minute scale later in the session (6x the sample size of the table above, `--rps 500`) before committing to 10s long-term -- same result, not a small-sample artifact (see the tick row in the stability table above for the numbers). That larger run also surfaced an asymmetry the original table couldn't show at only 10 minutes: `mc` degrades *partially* under a slow tick (keeps mean/median significance, loses the tail), while `aco` collapses *completely* -- consistent with `aco`'s pheromone momentum compounding the lag on top of fewer update cycles, where `mc`'s memoryless recompute just gets less frequent.

## The one thing that moves: `aco` vs. `mc`

Every other pairing in the ranking is stable. The `aco`/`mc` order is the one genuine open question, and it flips based on two things:

1. **Tick speed.** `aco`'s pheromone-momentum design means a slow tick compounds two penalties at once -- fewer update cycles, plus an algorithm that's already sluggish to react. `mc` has no memory to compound with; a slow tick just means less-frequent full recalculation. At 60s tick, `aco` collapses to statistical noise while `mc` retains a partial (if weaker) edge. At 10s tick, both are fully significant and `mc` usually edges out `aco`.
2. **Latency spread.** At the original narrow 5-80ms backend range, `mc` beat `aco`. At the widened 150-600ms range, `aco` overtook `mc` -- reproduced at both a small (3-backend) and full (5-backend, 60-minute) scale. Working theory, not yet isolated: a wider absolute spread gives `aco`'s "lock onto a favorite and hold it" momentum more durable signal to lock onto, since backends are more clearly separated; `mc`'s from-scratch-every-window recompute doesn't benefit the same way, since it was already reacting fully regardless of separation. Not yet tested independent of the tick change made in the same session -- varying spread width alone, with tick and backend count held fixed, would isolate this properly.

Neither algorithm "wins" outright. The honest finding is that which one wins depends on the reaction-cadence-to-environment-change-rate ratio and the latency landscape's shape -- a real, structured answer, not a coin flip.

## Why `leastconn`/`random2` consistently win

Little's Law gives the mechanism: `leastconn`'s live connection count is a zero-lag, real-time estimate of a backend's current load (a slower backend accumulates open connections faster than it drains them, purely as a mechanical consequence of holding requests longer). `aco`/`mc` are both lagged estimators by construction -- their weights reflect *last window's* observations, one full sampling interval behind whatever is happening right now. No amount of tuning `aco`/`mc`'s reaction speed changes this structural gap; it can only be narrowed (see the tick findings above), not closed, without also paying the tick's own tradeoffs (a very fast tick means very little data per window, noisier weight updates).

This matches the real-world framing this project set out to test: the actual "why not just use X" objection for an L7 reverse proxy in front of an internal backend pool isn't an exotic alternative like anycast -- it's plain least-connections, which is what enterprises already run via HAProxy/NGINX/F5 BIG-IP. This project's own results agree with that industry default, for a legible, mechanistic reason (zero-lag vs. lagged information), not just "because that's what people do."

## The built-in's win isn't always fully decisive

The headline ranking (`leastconn` beats the best-performing adaptive algorithm on mean TTFB, every single run) is consistent -- but the stats report's incremental best-built-in-vs-best-adaptive comparison runs the same Mann-Whitney + bootstrap-CI machinery used for the control comparisons, across all five stats (mean/median/p90/p95/p99), and that lead isn't always statistically significant across the *whole* distribution:

| Run config | Best built-in vs. best adaptive | Stats significant |
|---|---|---|
| 60s tick, narrow (5-80ms) profile | `leastconn` vs. `mc`, +26.4ms mean | 5/5 -- mean, median, p90, p95, p99 |
| 10s tick, narrow profile | `leastconn` vs. `mc`, +15.1ms mean | 3/5 -- mean, median, p90; p95/p99 not significant |
| 10s tick, wide (150-600ms) profile | `leastconn` vs. `aco`, +8.5ms mean | 2/5 -- mean, median only; p90/p95/p99 not significant |
| 10s tick, wide profile, throughput-degraded run | `leastconn` vs. `aco`, +8.4ms mean | 2/5 -- mean, median only; p90/p95/p99 not significant |

Three of four documented best-vs-best comparisons lose significance on the tail percentiles even while mean/median hold -- meaning the winning adaptive algorithm's worst-case behavior (p90/p95/p99, the tail that actually matters for real SLAs) often wasn't reliably distinguishable from `leastconn`'s, even though its central tendency was. Likely explanation, not yet independently confirmed: tail percentiles are the noisiest stat at any given sample size (fewer effective observations feed each extreme quantile than feed a mean), so the bootstrap CI around a tail-percentile delta is systematically wider and harder to pull away from zero than the CI around a mean or median delta, independent of whether a true difference exists. This doesn't overturn the headline ranking -- `leastconn`'s point estimate is lower in every run, no exceptions -- but "leastconn wins" should be read as "wins on central tendency, reliably; wins on tail latency, only sometimes provably," not a clean sweep across the entire distribution.

## Caveats and limitations, stated plainly

- **Reload-noise confound, flagged but not corrected for.** `nginx -s reload` (fired nearly every sampling window by `aco`/`mc`) is a whole-process event -- it briefly churns *every* path's worker pool, not just the one whose conf changed. Reasoned to likely not bias within-run comparisons (every path shares identical reload timing), but cross-run comparisons where `aco`/`mc`'s reload frequency differs between configs (e.g. the tick comparison) carry an unquantified amount of this noise on their static baselines. Directly checkable from existing `{algo}.weights.csv` timestamps (bucket static-path TTFB by time-since-last-reload); not done yet.
- **Backend latency is synthetic and schedule-driven, never load-reactive.** Every backend's response time is `personality range + a randomly-drifting offset`, drawn independently of how much real concurrent traffic that backend is actually receiving. This is why the experiment can cleanly isolate "which selection mechanism reacts fastest to a changing ranking" -- but it also means a scenario like "a backend gets overloaded because multiple uncoordinated load balancers can't see each other's traffic" can't be demonstrated in this environment as currently built.
- **The traffic generator has its own silent throughput ceiling, and it compounds over time rather than settling -- discovered late.** A nominal "1000rps/path" run averaged ~652rps/path effective, but that average hides the actual shape: the selection-frequency charts show requests-per-window declining continuously for the *entire* hour (~1650/window at the start down to ~1250/window at the end, across every path, not just one) -- confirmed against a comparable run that hit its 500rps target exactly, whose equivalent chart is dead flat for the full hour with no trend. Mechanism: `ThreadPoolExecutor.submit()` has no bounded queue and no backpressure, so once submission rate structurally exceeds what the thread pool can sustain, tasks pile up in the executor's internal queue for the whole run -- a continuously growing backlog of queued Python objects that adds its own GC/scheduling overhead, dragging real throughput down further as the run continues. This is a compounding overload, not a one-time shortfall settling at a stable lower rate. The ranking direction was still unaffected -- the overloaded run reproduced the identical algorithm ordering -- but the number "1000rps" in that run's config should be read as "never reached steady state," not as a fifth confirmed rate point on the `--rps` axis. Not fixed as of this writing; see `AGENT.md` if revisiting.
- **Fair failure-injection testing (killing a backend outright) is a structurally unwinnable test for `aco`/`mc`, reasoned through but not built.** NGINX's own passive health checks (`fail_timeout` etc.) are cheaply tunable down to ~1s; `aco`/`mc` are bound by `TICK_SECONDS`, a genuinely expensive floor to lower (fast ticks starve the algorithms of data). A fair comparison would let both sides tune their reaction speed to its practical floor, and `least_conn`'s floor is simply lower. Concluded this isn't worth building -- the answer is knowable in advance, and building it anyway would just be an elaborate way of re-confirming a mechanism already understood.

## Bottom line

The result that matters isn't "ACO/Markov beat round robin" (they do, reliably, once the tick is fast enough relative to how fast the environment changes) -- it's that **neither adaptive algorithm ever beats NGINX's own `least_conn`**, across every configuration tried. The interesting, defensible claim this project can make is narrower and more useful than "adaptive learning wins": it's a mechanistic explanation of *why* a zero-lag live signal structurally beats a lagged learned one, plus a mapped-out characterization of exactly when each adaptive algorithm's own reaction speed becomes the bottleneck. That's a real result for a load-balancing methodology writeup, even if it isn't the flashiest possible headline.
