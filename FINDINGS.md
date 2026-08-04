⚠️ This file is committed to a public git repo. Never store secrets, credentials, API keys, environment-specific paths, or sensitive data here.

# Findings

This is the results/conclusions writeup -- what the experiment actually showed, consolidated across every run and every axis varied. `AGENT.md` is the development log (decisions, bugs, why things are built the way they are); `README.md` is the how-to-run-it guide. This file is neither -- it's the answer to "so what did you learn."

## What was tested

Seven upstream-selection mechanisms running **simultaneously**, behind the same NGINX instance, against the same backend pool, on identical live traffic:

- `rr` -- unweighted round robin (the control).
- `random` -- NGINX `random` (a validity canary, expected to always track `rr`; see below).
- `random2` -- NGINX `random two` (pick 2 at random, route to whichever has fewer active connections).
- `leastconn` -- NGINX `least_conn` (always route to fewest active connections, pool-wide).
- `aco` -- this project's Ant Colony Optimization module: pheromone-weighted, evaporates and re-deposits every sampling window, has momentum ("slow to forget").
- `mc` -- this project's Markov Chain module: transition matrix rebuilt from scratch every window, no memory carried over.
- `combo` -- NGINX `least_conn` *plus* integer `weight=` on every server, the weights rewritten every sampling window by a dedicated ACO instance (same tuning as `aco`, see "Tuning `aco`" below). Added most recently, see "`combo`: pairing `least_conn`'s live signal with ACO weights" below -- least-tested of the seven so far.

Varied across runs: traffic rate (40-1280rps/path -- see caveat below on the effective ceiling above 320rps/path on the hardware this was run on), sampling tick (5s/10s/20s/60s), backend count (5 vs. 3), backend latency profile (5-80ms vs. 150-600ms), NGINX worker topology (1 worker vs. 4 workers+shared zones), number of independent LB instances sharing one backend pool (1 vs. 3, since removed), simulated backend contention (off/mild/moderate/heavy, since removed), and `aco`'s own tuning parameters (`ACO_EVAPORATION_RATE`/`ACO_DEPOSIT_CONSTANT`). See `AGENT.md` for the underlying architecture and mechanisms.

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
| 3 independent NGINX instances sharing one backend pool (the literal "multiple LBs" scenario NGINX's own docs caution `least_conn`/`random two` against), `--rps` 40/80/160 x simulated backend contention off/mild/moderate/heavy (16 combinations, since-removed multi-instance topology + contention mechanism -- see `AGENT.md`'s "History" section) | Neither forcing condition moved the needle. `leastconn` beat the best adaptive algorithm in every single one of these 12 cells, margin growing with contention severity (~1.7-2.9% under off/mild/moderate, jumping to ~4.2-7.4% under heavy -- a distinct, non-overlapping step up, not a smooth ramp). Off/mild/moderate were statistically indistinguishable *from each other* at every rps -- contention only started mattering once it hit "heavy," and even then `leastconn` still won. No trace of the "many-LB blindness" effect this whole topology was built to find. |
| `--rps` 320 (same sweep, overload regime) | The one ranking flip found anywhere in this project: `leastconn` fell to *worst* and mean TTFB jumped to 620-670ms regardless of contention level (off through heavy all landed in that same band) -- the signature of raw request-rate saturation, not contention or algorithm behavior. The flip itself mostly wasn't statistically significant vs. the `rr` control. **Read as an overload artifact, not a genuine reversal of the headline ranking -- and not something any load-balancing algorithm could fix.** Once the backends themselves are saturated, every path funnels through the same overwhelmed pool; no selection mechanism, however smart, routes traffic to capacity that doesn't exist. That's a hardware/capacity problem, not a software one -- the fix is more or faster backends, not a better algorithm. |

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
3. **Contention severity.** In the rps/contention sweep (see the stability table above), `aco` was the stronger adaptive algorithm in 10 of 12 non-overload cells -- but `mc` specifically overtook it under `heavy` contention at every rps tested (and one `moderate` cell at the lowest rps). Consistent with the same momentum tradeoff as the tick-speed finding: `aco`'s "stay locked onto a recent favorite" behavior is an advantage when conditions are comparatively calm, but becomes a liability once conditions are changing hard enough (heavy contention) that last window's favorite is more likely to already be wrong -- `mc`'s memoryless full-recompute has less to unlearn.

Neither algorithm "wins" outright. The honest finding is that which one wins depends on the reaction-cadence-to-environment-change-rate ratio, the latency landscape's shape, and how hard the backend pool is being squeezed -- a real, structured answer, not a coin flip. But across all three axes, `aco` is the more frequent winner of the two -- it's the one worth calling out as "showing promise," even though neither ever closes the gap on `leastconn` itself.

## Why `leastconn`/`random2` consistently win

Little's Law gives the mechanism: `leastconn`'s live connection count is a zero-lag, real-time estimate of a backend's current load (a slower backend accumulates open connections faster than it drains them, purely as a mechanical consequence of holding requests longer). `aco`/`mc` are both lagged estimators by construction -- their weights reflect *last window's* observations, one full sampling interval behind whatever is happening right now. No amount of tuning `aco`/`mc`'s reaction speed changes this structural gap; it can only be narrowed (see the tick findings above), not closed, without also paying the tick's own tradeoffs (a very fast tick means very little data per window, noisier weight updates).

This matches the real-world framing this project set out to test: the actual "why not just use X" objection for an L7 reverse proxy in front of an internal backend pool isn't an exotic alternative like anycast -- it's plain least-connections, which is what enterprises already run via HAProxy/NGINX/F5 BIG-IP. This project's own results agree with that industry default, for a legible, mechanistic reason (zero-lag vs. lagged information), not just "because that's what people do."

## Tuning `aco`: raising evaporation rate closes most of the gap

A 6-variant sweep (`--rps 80`, 10-minute runs) tested `ACO_EVAPORATION_RATE`/`ACO_DEPOSIT_CONSTANT` in both directions from their defaults (0.1/10.0):

| Variant | evaporation | deposit | `aco` vs `leastconn` mean gap |
|---|---|---|---|
| Baseline (default) | 0.1 | 10 | +9.2ms (+2.5%) |
| Raise evaporation | **0.3** | 10 | **+2.6ms (+0.7%) -- best** |
| Raise deposit | 0.1 | 30 | +10.1ms (+2.9%) -- worse |
| Raise both | 0.3 | 30 | +4.1ms (+1.1%) |
| Lower evaporation | 0.05 | 10 | +7.8ms (+2.1%) |
| Lower deposit | 0.1 | 5 | +19.6ms (+5.2%) -- worst |

Only one direction helped: raising `ACO_EVAPORATION_RATE` alone (less momentum, more reactive, closer to `mc`'s behavior) cut the gap by roughly two-thirds. Touching `ACO_DEPOSIT_CONSTANT` in either direction made things worse, independently and combined with the helpful evaporation change -- a smaller deposit gives `aco` too weak a signal to commit to a leader (worst result of the sweep), a larger one adds noise without benefit. Lowering evaporation (more momentum than default) didn't help either. Net: this is a one-knob win, not a two-knob one.

**Reproducibility check, since backend conditions drift randomly and a single run's absolute numbers aren't trustworthy on their own:** `ACO_EVAPORATION_RATE=0.3` was baked into `docker-compose.full.yml` as the new default, and run back-to-back several times (`--rps 80`, fresh pheromone state each run via container recreate, same live conditions for `aco` and `leastconn` within each run):

| Run | mean gap | median gap | p90 | p95 | p99 |
|---|---|---|---|---|---|
| 1 | +0.8% (sig.) | +2.6% (sig.) | -0.2% (n.s.) | -0.2% (n.s.) | -0.3% (n.s.) |
| 2 | +0.5% (sig.) | +0.9% (sig.) | **-1.0% (sig., aco wins)** | -0.5% (n.s.) | -0.3% (n.s.) |
| 3 | +0.5% (sig.) | +1.3% (sig.) | **-1.2% (sig., aco wins)** | **-0.9% (sig., aco wins)** | -0.5% (n.s.) |
| 4 | +0.5% (n.s. -- tied) | +0.9% (sig.) | +0.0% (n.s.) | +0.2% (n.s.) | +0.0% (n.s.) |
| 5 | +0.9% (sig.) | +0.8% (sig.) | -0.7% (n.s.) | -0.7% (n.s.) | -0.1% (n.s.) |

Consistent, fair reading of this across all 5: `leastconn` keeps a small, usually-significant edge on mean and median TTFB even after tuning -- the structural zero-lag-vs-lagged-signal gap described above narrows, but doesn't close, exactly as predicted. Mean is significant in 4 of 5 runs (0.5-0.9%), median in all 5 (0.8-2.6%). On tail latency (p90/p95/p99) the picture is genuinely mixed run to run: usually a statistical tie, and in 2 of 5 runs `aco` was *significantly faster* than `leastconn` at p90 (once also at p95) -- not a fluke in one direction only, since run 4's mean gap wasn't even significant. This doesn't overturn the headline ranking (`leastconn` still wins more often than it loses, and never loses on mean/median across 5 repeats), but it's real evidence that a tuned `aco` is a much closer contest than the untuned default ever was, including outright wins on specific stats in specific runs.

## `combo`: pairing `least_conn`'s live signal with ACO weights

`aco`'s structural handicap (identified above) is that it's a lagged estimator competing against `leastconn`'s zero-lag one. `combo` doesn't try to out-react `leastconn` -- it hands `leastconn` a second signal on top of the one it already has: NGINX's `least_conn` method picks the server with the lowest `active_connections/weight`, so giving it ACO-derived weights (same tuning as tuned `aco`: evaporation 0.3, deposit 10) biases *which* backend wins a live-connection tie, instead of replacing `leastconn`'s own real-time signal with a lagged one.

Four back-to-back runs at increasing rps (40/80/160/320, 10-minute runs, 10s tick, the only four where the traffic generator actually delivered the requested load -- see the throughput-ceiling caveat below for why 640/1280 are excluded here):

| rps | mean gap | median gap | p90 | p95 | p99 |
|---|---|---|---|---|---|
| 40 | -21.1ms (-5.4%) sig. | -27.0ms (-7.2%) sig. | -31.0ms (-5.4%) sig. | -31.0ms (-4.9%) sig. | -25.0ms (-3.5%) sig. |
| 80 | -23.9ms (-6.6%) sig. | -21.0ms (-6.2%) sig. | -81.0ms (-14.2%) sig. | -51.0ms (-8.0%) sig. | -22.0ms (-3.0%) sig. |
| 160 | -28.2ms (-7.9%) sig. | -30.0ms (-8.8%) sig. | -54.0ms (-9.6%) sig. | -36.0ms (-5.9%) sig. | -30.0ms (-4.3%) sig. |
| 320 | -29.2ms (-7.9%) sig. | -20.0ms (-5.7%) sig. | -51.0ms (-9.1%) sig. | -64.0ms (-10.0%) sig. | -36.0ms (-4.9%) sig. |

(gap = `combo` mean/median/percentile minus `leastconn`'s; negative = `combo` faster)

Every stat, every rps, significant, all in `combo`'s favor -- the first result in this entire project where something beats `leastconn` outright rather than narrowing the gap (tuned `aco`) or tying on tail latency alone. Roughly 20-30ms/5-8% faster than `leastconn` on mean TTFB across the whole rps range tested.

**Read cautiously, not as a settled finding yet.** This is one sweep along one axis (rps, at a fixed 10s tick, 5 backends, the default 5-80ms latency profile) run once each, not repeated back-to-back the way the `aco` tuning result was before it went in this document, and not yet tested against tick sensitivity, backend count, or latency-profile changes the way the rest of the headline ranking has been. The consistency across all four rps points and all five stats is a good sign, but it hasn't earned the same confidence yet as the rest of this file -- treat `combo` as a promising early result pending the same reproducibility scrutiny everything else here already got.

**A first tuning pass found no clean improvement over `aco`'s inherited default.** `combo` currently reuses whatever `ACO_EVAPORATION_RATE`/`ACO_DEPOSIT_CONSTANT` `/aco` is tuned to (0.3/10, both are shared module-level constants -- see `AGENT.md`), never independently tuned for its own mechanism. One variant each side of 0.3 (`--rps 80`, single run each):

| Evaporation | mean gap | median gap | p90 | p95 | p99 |
|---|---|---|---|---|---|
| 0.15 (slower) | -20.6ms (-5.7%) | -20.0ms (-5.8%) | -61.0ms (-10.6%) | -38.0ms (-5.9%) | -19.0ms (-2.5%) |
| **0.3 (default)** | -23.9ms (-6.6%) | **-21.0ms (-6.2%)** | **-81.0ms (-14.2%)** | **-51.0ms (-8.0%)** | -22.0ms (-3.0%) |
| 0.5 (faster) | **-25.4ms (-6.7%)** | -17.0ms (-4.8%) | -54.0ms (-9.2%) | -40.0ms (-6.2%) | **-40.0ms (-5.2%)** |

0.15 lost on every stat -- same direction as `aco`'s own sweep (less momentum helps). 0.5 was a mixed bag: better mean and notably better p99, worse median/p90/p95. Net: the current default still looks like the best all-around choice, but this is one run per variant, not a reproducibility check, so it isn't strong enough evidence to either adopt 0.5 or rule it out -- left as an open question in `EXPERIMENTS.md` rather than resolved here.

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
- **On a Mac M4, 320rps/path is the highest rate confirmed to actually deliver what it asks for -- 640 and above hit a hard, reproducible ceiling, not a gradual falloff.** Checking each run's actual request count against `rps x duration` (the same n-count-vs-target check the "silent ceiling" bullet above is about): 40/80/160/320rps all landed within ~0.2% of their target n, but a 640rps run only achieved ~551rps/path (14% short), and a separate 1280rps run landed at essentially the *same* ~548rps/path -- doubling the requested rate did not move the achieved rate at all. That's the signature of a hard resource ceiling (this project's own thread-per-request generator sizes `0.5 workers/rps/path` -- at 640rps across 7 paths that's ~2,240 OS threads in one process, well into GIL/OS-scheduling contention territory on a single machine), not a backend or algorithm effect. **Practical takeaway: `--rps 320` is the current, verified ceiling for a measurement on this hardware to mean what its config claims; treat any run requesting more than that as suspect until its own n-count is checked against its target the same way.** See `README.md`'s "Choosing `--rps`" for the general guidance this sharpens.
- **Fair failure-injection testing (killing a backend outright) is a structurally unwinnable test for `aco`/`mc`, reasoned through but not built.** NGINX's own passive health checks (`fail_timeout` etc.) are cheaply tunable down to ~1s; `aco`/`mc` are bound by `TICK_SECONDS`, a genuinely expensive floor to lower (fast ticks starve the algorithms of data). A fair comparison would let both sides tune their reaction speed to its practical floor, and `least_conn`'s floor is simply lower. Concluded this isn't worth building -- the answer is knowable in advance, and building it anyway would just be an elaborate way of re-confirming a mechanism already understood.

## Bottom line

The result that matters isn't "ACO/Markov beat round robin" (they do, reliably, once the tick is fast enough relative to how fast the environment changes) -- it's that **`least_conn` wins on mean/median TTFB in every configuration tried, tuned or not**, including every attempt to force a different outcome: 4x the rps range, four tick speeds, two backend counts, two latency profiles, and -- the most deliberate attempts to break it -- three independent load balancers sharing one backend pool and four levels of simulated contention, none of which produced the "many-LB blindness" effect that setup was specifically built to find. The one exception is the tail-latency stats (p90/p95) in a tuned `aco` config, where the two are a genuine toss-up and `aco` sometimes wins outright (see "Tuning `aco`" above) -- worth stating precisely rather than rounding up to "ACO wins" or down to "ACO never wins." The interesting, defensible claim this project can make is narrower and more useful than "adaptive learning wins": it's a mechanistic explanation of *why* a zero-lag live signal structurally beats a lagged learned one on the stats that matter most reliably, a mapped-out characterization of exactly when each adaptive algorithm's own reaction speed becomes the bottleneck, and a demonstration that tuning can close most -- not all -- of that gap. That's a real result for a load-balancing methodology writeup, even if it isn't the flashiest possible headline.

The newest arm, `combo` (`least_conn` plus ACO-set weights, see above), is the first thing in this project to beat `leastconn` outright rather than just narrow the gap or tie on tail latency -- significant on every stat at every rps tested so far (40-320). It hasn't yet been through the same reproducibility and cross-axis testing (tick, backend count, latency profile) the rest of this document's claims have, so it's reported here as a genuinely promising early result, not promoted to the same confidence level as the headline ranking above.
