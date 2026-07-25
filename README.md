# upstream-rl
Experiments to try non-deterministic load balancing methods for traffic shaping RL

See `AGENT.md` for architecture and design tradeoffs.

## Quick start (Phase 1 -- scaffolding only, no real ACO/Markov yet)

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
