⚠️ This file is committed to a public git repo. Never store secrets, credentials, API keys, environment-specific paths, or sensitive data here.

# Findings

This is the results/conclusions writeup -- what the experiment actually showed, consolidated across every run and every axis varied. `AGENT.md` is the development log (decisions, bugs, why things are built the way they are); `README.md` is the how-to-run-it guide. This file is neither -- it's the answer to "so what did you learn."

## What was tested

Six upstream-selection mechanisms running **simultaneously**, behind the same NGINX instance, against the same backend pool, on identical live traffic:

- `rr_control` -- unweighted round robin (the control).
- `leastconn` -- NGINX `least_conn` (always route to fewest active connections, pool-wide).
- `aco_wrr` -- this project's Ant Colony Optimization module, plain weighted round robin: pheromone-weighted, evaporates and re-deposits every sampling window, has momentum ("slow to forget").
- `mc_wrr` -- this project's Markov Chain module, plain weighted round robin: transition matrix rebuilt from scratch every window, no memory carried over.
- `aco_lc` -- NGINX `least_conn` *plus* integer `weight=` on every server, the weights rewritten every sampling window by a dedicated ACO instance (same tuning as `aco_wrr`, see "Tuning `aco_wrr`" below). The one mechanism in this project that beats `leastconn` outright rather than closing the gap on it, see "`aco_lc`: the clean winner" below.
- `mc_lc` -- same idea as `aco_lc`, pairing `least_conn` with a dedicated Markov Chain instance's weights instead.

`/random` and `/random2` (NGINX `random`/`random two`) also existed earlier in this project, as validity canaries rather than peer algorithms -- removed once their validation purpose was fulfilled; see `AGENT.md`'s History section for that result.

Varied across runs: traffic rate (40-1280rps/path -- see caveat below on the effective ceiling above 320rps/path on the hardware this was run on), NGINX worker topology (1 worker vs. 4 workers+shared zones), number of independent LB instances sharing one backend pool (1 vs. 3, since removed), simulated backend contention (off/mild/moderate/heavy, since removed), and `aco_wrr`'s own tuning parameters (`ACO_EVAPORATION_RATE`/`ACO_DEPOSIT_CONSTANT`). Everything here runs against the project's current, settled default configuration (10s tick, 5 backends, 5-80ms latency profile) unless stated otherwise. See `AGENT.md` for the underlying architecture and mechanisms.

## Headline result: the ranking is stable, not fragile

Across every condition tested, the same hierarchy holds:

**`aco_lc` (`least_conn` + ACO weights) > `leastconn` (live connection-state) > `aco_wrr` / `mc_wrr` (historical-learning) > `rr_control` (no state at all)**

`aco_lc`'s spot at the top is confirmed (see "`aco_lc`: the clean winner" below) but on a narrower evidence base than everything below it in this table: 6 runs across two axes (rps, ACO tuning), not yet the multi-instance/contention and overload battery the rest of this hierarchy has survived. Every one of those 6 runs was a clean sweep on all five stats, with zero exceptions -- strong enough to call outright, but stated here plainly rather than implying it's had the same scrutiny as the rest.

| Condition varied | Result |
|---|---|
| `--rps` 125 -> 250 -> 500 (4x range, ~7.5k -> ~300k requests/path) | Identical ranking every time -- `leastconn`, `mc_wrr`, `aco_wrr`, then `rr_control` last. Reproducibility data point, not just a throughput check. |
| Worker topology 1 vs. 4 workers | Correctness issue, not a ranking change: 4 workers *without* a shared `zone` skews every algorithm's selection state (more independent per-worker cycles = worse aggregate skew, not better). With `zone` added to every upstream block, skew collapses back down and the ranking is unaffected -- this was a bug fix, not an experimental axis. |
| Nominal 1000rps/path (see caveat) | Same ranking again, at yet another (if imprecise) throughput point. |
| 3 independent NGINX instances sharing one backend pool (the literal "multiple LBs" scenario NGINX's own docs caution `least_conn`/`random two` against), `--rps` 40/80/160 x simulated backend contention off/mild/moderate/heavy (16 combinations, since-removed multi-instance topology + contention mechanism -- see `AGENT.md`'s "History" section) | Neither forcing condition moved the needle. `leastconn` beat the best adaptive algorithm in every single one of these 12 cells, margin growing with contention severity (~1.7-2.9% under off/mild/moderate, jumping to ~4.2-7.4% under heavy -- a distinct, non-overlapping step up, not a smooth ramp). Off/mild/moderate were statistically indistinguishable *from each other* at every rps -- contention only started mattering once it hit "heavy," and even then `leastconn` still won. No trace of the "many-LB blindness" effect this whole topology was built to find. |
| `--rps` 320 (same sweep, overload regime) | The one ranking flip found anywhere in this project: `leastconn` fell to *worst* and mean TTFB jumped to 620-670ms regardless of contention level (off through heavy all landed in that same band) -- the signature of raw request-rate saturation, not contention or algorithm behavior. The flip itself mostly wasn't statistically significant vs. the `rr_control` control. **Read as an overload artifact, not a genuine reversal of the headline ranking -- and not something any load-balancing algorithm could fix.** Once the backends themselves are saturated, every path funnels through the same overwhelmed pool; no selection mechanism, however smart, routes traffic to capacity that doesn't exist. That's a hardware/capacity problem, not a software one -- the fix is more or faster backends, not a better algorithm. |

Historically, `/random` vs. `/rr_control` never came back statistically significant in any run (p=0.21-0.97) -- exactly as expected of a validity canary, since an unweighted dice roll and unweighted round robin converge to the same distribution at scale. Both `/random` and `/random2` have since been removed from the roster now that they've served that purpose; see `AGENT.md`'s History section.

## Why `leastconn` consistently wins

Little's Law gives the mechanism: `leastconn`'s live connection count is a zero-lag, real-time estimate of a backend's current load (a slower backend accumulates open connections faster than it drains them, purely as a mechanical consequence of holding requests longer). `aco_wrr`/`mc_wrr` are both lagged estimators by construction -- their weights reflect *last window's* observations, one full sampling interval behind whatever is happening right now. No amount of tuning `aco_wrr`/`mc_wrr`'s reaction speed changes this structural gap; it can only be narrowed, not closed, without also paying the tick's own tradeoffs (a very fast tick means very little data per window, noisier weight updates).

This matches the real-world framing this project set out to test: the actual "why not just use X" objection for an L7 reverse proxy in front of an internal backend pool isn't an exotic alternative like anycast -- it's plain least-connections, which is what enterprises already run via HAProxy/NGINX/F5 BIG-IP. This project's own results agree with that industry default, for a legible, mechanistic reason (zero-lag vs. lagged information), not just "because that's what people do."

## Tuning `aco_wrr`: raising evaporation rate closes most of the gap

A 6-variant sweep (`--rps 80`, 10-minute runs) tested `ACO_EVAPORATION_RATE`/`ACO_DEPOSIT_CONSTANT` in both directions from their defaults (0.1/10.0):

| Variant | evaporation | deposit | `aco_wrr` vs `leastconn` mean gap |
|---|---|---|---|
| Baseline (default) | 0.1 | 10 | +9.2ms (+2.5%) |
| Raise evaporation | **0.3** | 10 | **+2.6ms (+0.7%) -- best** |
| Raise deposit | 0.1 | 30 | +10.1ms (+2.9%) -- worse |
| Raise both | 0.3 | 30 | +4.1ms (+1.1%) |
| Lower evaporation | 0.05 | 10 | +7.8ms (+2.1%) |
| Lower deposit | 0.1 | 5 | +19.6ms (+5.2%) -- worst |

Only one direction helped: raising `ACO_EVAPORATION_RATE` alone (less momentum, more reactive, closer to `mc_wrr`'s behavior) cut the gap by roughly two-thirds. Touching `ACO_DEPOSIT_CONSTANT` in either direction made things worse, independently and combined with the helpful evaporation change -- a smaller deposit gives `aco_wrr` too weak a signal to commit to a leader (worst result of the sweep), a larger one adds noise without benefit. Lowering evaporation (more momentum than default) didn't help either. Net: this is a one-knob win, not a two-knob one.

**Reproducibility check, since backend conditions drift randomly and a single run's absolute numbers aren't trustworthy on their own:** `ACO_EVAPORATION_RATE=0.3` was baked into `docker-compose.full.yml` as the new default, and run back-to-back several times (`--rps 80`, fresh pheromone state each run via container recreate, same live conditions for `aco_wrr` and `leastconn` within each run):

| Run | mean gap | median gap | p90 | p95 | p99 |
|---|---|---|---|---|---|
| 1 | +0.8% (sig.) | +2.6% (sig.) | -0.2% (n.s.) | -0.2% (n.s.) | -0.3% (n.s.) |
| 2 | +0.5% (sig.) | +0.9% (sig.) | **-1.0% (sig., aco_wrr wins)** | -0.5% (n.s.) | -0.3% (n.s.) |
| 3 | +0.5% (sig.) | +1.3% (sig.) | **-1.2% (sig., aco_wrr wins)** | **-0.9% (sig., aco_wrr wins)** | -0.5% (n.s.) |
| 4 | +0.5% (n.s. -- tied) | +0.9% (sig.) | +0.0% (n.s.) | +0.2% (n.s.) | +0.0% (n.s.) |
| 5 | +0.9% (sig.) | +0.8% (sig.) | -0.7% (n.s.) | -0.7% (n.s.) | -0.1% (n.s.) |

Consistent, fair reading of this across all 5: `leastconn` keeps a small, usually-significant edge on mean and median TTFB even after tuning -- the structural zero-lag-vs-lagged-signal gap described above narrows, but doesn't close, exactly as predicted. Mean is significant in 4 of 5 runs (0.5-0.9%), median in all 5 (0.8-2.6%). On tail latency (p90/p95/p99) the picture is genuinely mixed run to run: usually a statistical tie, and in 2 of 5 runs `aco_wrr` was *significantly faster* than `leastconn` at p90 (once also at p95) -- not a fluke in one direction only, since run 4's mean gap wasn't even significant. This doesn't overturn the headline ranking (`leastconn` still wins more often than it loses, and never loses on mean/median across 5 repeats), but it's real evidence that a tuned `aco_wrr` is a much closer contest than the untuned default ever was, including outright wins on specific stats in specific runs.

## `aco_lc`: the clean winner -- `least_conn`'s live signal plus ACO weights

`aco_wrr`'s structural handicap (identified above) is that it's a lagged estimator competing against `leastconn`'s zero-lag one. `aco_lc` doesn't try to out-react `leastconn` -- it hands `leastconn` a second signal on top of the one it already has: NGINX's `least_conn` method picks the server with the lowest `active_connections/weight`, so giving it ACO-derived weights biases *which* backend wins a live-connection tie, instead of replacing `leastconn`'s own real-time signal with a lagged one.

**Six runs, two independently varied axes, zero exceptions.** Four back-to-back runs at increasing rps (40/80/160/320, 10-minute runs, 10s tick, tuned default evaporation 0.3/deposit 10 -- the only four rps points where the traffic generator actually delivered the requested load, see the throughput-ceiling caveat below for why 640/1280 are excluded):

| rps | mean gap | median gap | p90 | p95 | p99 |
|---|---|---|---|---|---|
| 40 | -21.1ms (-5.4%) sig. | -27.0ms (-7.2%) sig. | -31.0ms (-5.4%) sig. | -31.0ms (-4.9%) sig. | -25.0ms (-3.5%) sig. |
| 80 | -23.9ms (-6.6%) sig. | -21.0ms (-6.2%) sig. | -81.0ms (-14.2%) sig. | -51.0ms (-8.0%) sig. | -22.0ms (-3.0%) sig. |
| 160 | -28.2ms (-7.9%) sig. | -30.0ms (-8.8%) sig. | -54.0ms (-9.6%) sig. | -36.0ms (-5.9%) sig. | -30.0ms (-4.3%) sig. |
| 320 | -29.2ms (-7.9%) sig. | -20.0ms (-5.7%) sig. | -51.0ms (-9.1%) sig. | -64.0ms (-10.0%) sig. | -36.0ms (-4.9%) sig. |

Plus two more runs from the tuning sweep (`--rps 80`, evaporation 0.15 and 0.5 -- see "Tuning" below), which vary `aco_lc`'s own ACO weighting rather than rps and land as two more independent data points, not a repeat of the row above:

| evaporation | mean gap | median gap | p90 | p95 | p99 |
|---|---|---|---|---|---|
| 0.15 | -20.6ms (-5.7%) sig. | -20.0ms (-5.8%) sig. | -61.0ms (-10.6%) sig. | -38.0ms (-5.9%) sig. | -19.0ms (-2.5%) sig. |
| 0.5 | -25.4ms (-6.7%) sig. | -17.0ms (-4.8%) sig. | -54.0ms (-9.2%) sig. | -40.0ms (-6.2%) sig. | -40.0ms (-5.2%) sig. |

(gap = `aco_lc` mean/median/percentile minus `leastconn`'s; negative = `aco_lc` faster)

30 of 30 stat-comparisons across all 6 runs are significant, every one in `aco_lc`'s favor -- no exceptions in either direction, at any rps tested (40-320) or any tuning tested (evaporation 0.15-0.5). This is the clean winner: the first and only mechanism in this project that beats `leastconn` outright, on every stat, rather than narrowing the gap (tuned `aco_wrr`) or tying on tail latency alone. Typically 20-30ms/5-8% faster than `leastconn` on mean TTFB, with the tail-latency wins (p90 especially) often larger in percentage terms than the mean win.

**Scope, stated plainly.** This result rests on two axes (rps, ACO tuning) against the project's current default configuration -- it has not yet been run through the multi-instance/contention battery the rest of this document's headline ranking survived. Given zero exceptions across 6 runs and 2 independently varied axes, that's enough to call `aco_lc` the winner rather than "promising" -- but it's a narrower evidence base than everything else in this file, worth re-running through the same battery if `aco_lc` becomes something this project leans on further.

**Tuning: the current default (evaporation 0.3) still looks like the best all-around choice, though it's not the whole story.** `aco_lc` currently reuses whatever `ACO_EVAPORATION_RATE`/`ACO_DEPOSIT_CONSTANT` `/aco_wrr` is tuned to (0.3/10, both shared module-level constants -- see `AGENT.md`), never independently tuned for its own mechanism. One variant each side of 0.3 (`--rps 80`, single run each, gap vs. `leastconn`):

| Evaporation | mean gap | median gap | p90 | p95 | p99 |
|---|---|---|---|---|---|
| 0.15 (slower) | -20.6ms (-5.7%) | -20.0ms (-5.8%) | -61.0ms (-10.6%) | -38.0ms (-5.9%) | -19.0ms (-2.5%) |
| **0.3 (default)** | -23.9ms (-6.6%) | **-21.0ms (-6.2%)** | **-81.0ms (-14.2%)** | **-51.0ms (-8.0%)** | -22.0ms (-3.0%) |
| 0.5 (faster) | **-25.4ms (-6.7%)** | -17.0ms (-4.8%) | -54.0ms (-9.2%) | -40.0ms (-6.2%) | **-40.0ms (-5.2%)** |

0.15 lost on every stat -- same direction as `aco_wrr`'s own sweep (less momentum helps). 0.5 was a mixed bag: better mean and notably better p99, worse median/p90/p95. All three still beat `leastconn` significantly on every stat (that's where the 6-run/30-comparison tally above comes from) -- the open question here is only ever "which flavor of winning is best," never "does aco_lc win." Net: the current default still looks like the best all-around choice, but one run per variant isn't a reproducibility check, so it isn't strong evidence to adopt 0.5 over it either -- left as an open question in `EXPERIMENTS.md`. Also open: `mc_lc` now exists too (see `AGENT.md`), and whether it wants tuning of its own -- MC has no tunable knob today at all, so this specific question doesn't yet apply, but the general "does the `_lc` variant want different behavior than its `_wrr` sibling" question does.

## Caveats and limitations, stated plainly

- **Previously flagged "reload-noise confound" was checked and doesn't hold up -- corrected, not just removed.** Earlier drafts of this document worried that `nginx -s reload` (fired nearly every sampling window by `aco_wrr`/`mc_wrr`/`aco_lc`/`mc_lc`) might add unquantified TTFB noise to every path's baseline, not just the one whose conf changed, since a reload is process-wide -- every path's workers get replaced together. Checked against NGINX's actual reload semantics rather than left as an assumption: a reload is graceful, not disruptive -- old workers finish whatever they already had in flight to completion, uninterrupted; new workers are up and accepting before old ones stop; and the master process, which owns every zone's shared memory (connection counts, round-robin position, `least_conn`'s live state), is never restarted, so that state isn't reset either. There's no mechanism left by which a reload could add latency noise to a path whose own conf didn't change -- see `AGENT.md`, "Zone data survives `nginx -s reload`."
- **Backend latency is synthetic and schedule-driven, never load-reactive.** Every backend's response time is `personality range + a randomly-drifting offset`, drawn independently of how much real concurrent traffic that backend is actually receiving. This is why the experiment can cleanly isolate "which selection mechanism reacts fastest to a changing ranking" -- but it also means a scenario like "a backend gets overloaded because multiple uncoordinated load balancers can't see each other's traffic" can't be demonstrated in this environment as currently built.
- **The traffic generator has its own silent throughput ceiling, and it compounds over time rather than settling -- discovered late.** A nominal "1000rps/path" run averaged ~652rps/path effective, but that average hides the actual shape: the selection-frequency charts show requests-per-window declining continuously for the *entire* hour (~1650/window at the start down to ~1250/window at the end, across every path, not just one) -- confirmed against a comparable run that hit its 500rps target exactly, whose equivalent chart is dead flat for the full hour with no trend. Mechanism: `ThreadPoolExecutor.submit()` has no bounded queue and no backpressure, so once submission rate structurally exceeds what the thread pool can sustain, tasks pile up in the executor's internal queue for the whole run -- a continuously growing backlog of queued Python objects that adds its own GC/scheduling overhead, dragging real throughput down further as the run continues. This is a compounding overload, not a one-time shortfall settling at a stable lower rate. The ranking direction was still unaffected -- the overloaded run reproduced the identical algorithm ordering -- but the number "1000rps" in that run's config should be read as "never reached steady state," not as a fifth confirmed rate point on the `--rps` axis. Not fixed as of this writing; see `AGENT.md` if revisiting.
- **On a Mac M4, 320rps/path is the highest rate confirmed to actually deliver what it asks for -- 640 and above hit a hard, reproducible ceiling, not a gradual falloff.** Checking each run's actual request count against `rps x duration` (the same n-count-vs-target check the "silent ceiling" bullet above is about): 40/80/160/320rps all landed within ~0.2% of their target n, but a 640rps run only achieved ~551rps/path (14% short), and a separate 1280rps run landed at essentially the *same* ~548rps/path -- doubling the requested rate did not move the achieved rate at all. That's the signature of a hard resource ceiling (this project's own thread-per-request generator sizes `0.5 workers/rps/path` -- at 640rps across 7 paths that's ~2,240 OS threads in one process, well into GIL/OS-scheduling contention territory on a single machine), not a backend or algorithm effect. **Practical takeaway: `--rps 320` is the current, verified ceiling for a measurement on this hardware to mean what its config claims; treat any run requesting more than that as suspect until its own n-count is checked against its target the same way.** See `README.md`'s "Choosing `--rps`" for the general guidance this sharpens.
- **Fair failure-injection testing (killing a backend outright) is a structurally unwinnable test for `aco_wrr`/`mc_wrr`, reasoned through but not built.** NGINX's own passive health checks (`fail_timeout` etc.) are cheaply tunable down to ~1s; `aco_wrr`/`mc_wrr` are bound by `TICK_SECONDS`, a genuinely expensive floor to lower (fast ticks starve the algorithms of data). A fair comparison would let both sides tune their reaction speed to its practical floor, and `least_conn`'s floor is simply lower. Concluded this isn't worth building -- the answer is knowable in advance, and building it anyway would just be an elaborate way of re-confirming a mechanism already understood.

## Bottom line

**The best-performing mechanism found in this project is `aco_lc`** (`least_conn` plus ACO-set weights, see above) -- a clean, significant win over `leastconn` on every stat (mean/median/p90/p95/p99), across every condition tried so far: 6 runs, 2 independently varied axes (rps 40-320, ACO evaporation 0.15-0.5), zero exceptions in either direction. It doesn't out-react `leastconn`'s zero-lag signal the way `aco_wrr`/`mc_wrr` try and fail to -- it hands that same live signal a second, ACO-derived input to break ties with, and that combination beats plain `leastconn` outright rather than just narrowing the gap on it.

Below `aco_lc`, the rest of the original hierarchy still holds exactly as tested: **`leastconn` (live connection-state) beats `aco_wrr`/`mc_wrr` (historical-learning) on mean/median TTFB in every configuration tried, tuned or not**, including every attempt to force a different outcome: 4x the rps range, and -- the most deliberate attempts to break it -- three independent load balancers sharing one backend pool and four levels of simulated contention, none of which produced the "many-LB blindness" effect that setup was specifically built to find. The one exception is the tail-latency stats (p90/p95) in a tuned `aco_wrr` config, where the two are a genuine toss-up and `aco_wrr` sometimes wins outright (see "Tuning `aco_wrr`" above) -- worth stating precisely rather than rounding up to "ACO wins" or down to "ACO never wins." The mechanistic explanation for all of it is the same one: a zero-lag live signal structurally beats a lagged learned one on the stats that matter most reliably -- and `aco_lc`'s win is consistent with that mechanism, not an exception to it, since it doesn't replace the live signal, it sharpens it.

`aco_lc`'s result rests on a narrower evidence base than everything below it in this hierarchy -- 2 axes, not the full multi-instance/contention battery the rest of this document survived (see "`aco_lc`: the clean winner" above for the exact scope). Zero exceptions across 6 runs is enough to call it the winner rather than hedge, but it's worth re-running through that same battery if `aco_lc` becomes something this project leans on further. `mc_lc` also exists now (see `AGENT.md`) but hasn't yet gone through this project's reproducibility/scope scrutiny at all -- not included in the "clean winner" claim above pending its own runs.
