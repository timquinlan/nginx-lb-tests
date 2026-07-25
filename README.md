# upstream-rl
Experiments to try non-deterministic load balancing methods for traffic shaping RL

See `AGENT.md` for architecture and design tradeoffs.

## Quick start (Phases 1-3 built: RR baseline, ACO, Markov all running -- Phase 4 analysis tooling not yet built)

```sh
docker compose -f docker-compose.full.yml up --build -d
```

This validates backends, generates NGINX config, primes `/aco` and `/mc` with equal weights, and starts the per-algorithm sampling loops automatically -- no further setup needed. It does **not** send any traffic on its own. Trigger an experiment run manually, against the already-running container:

```sh
docker exec -it $(docker compose -f docker-compose.full.yml ps -q controller) \
  python3 traffic_generator.py --tick 5 --rps 5 --duration 4   # smoke test: 5s tick, ~20s total
```

Run it again (with different `--tick`/`--rps`/`--duration`) as many times as you like against the same container -- each run appends its own record to `runs.log`. See `AGENT.md` for why setup and traffic generation are split this way.

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
