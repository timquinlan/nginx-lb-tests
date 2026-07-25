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

## Two data streams

- **Stream 1 (operational, not analyzed):** algorithm internal state, fallback-probe notices — stderr only.
- **Stream 2 (the experimental evidence):** NGINX access logs + `runs.log`, on the `./logs` host directory (bind-mounted, not a Docker-managed named volume — see "Bind mounts, not named volumes" below). This is what Phase 4 reads.

## Bind mounts, not named volumes

`./logs` and `./nginx-conf` (repo-root directories, gitignored) are bind-mounted into the container rather than using Docker named volumes. Named volumes are opaque — reaching the data means `docker exec`/`docker cp` every time. Since this whole project's point is inspecting the experiment data (and Phase 4's analysis scripts are meant to be runnable/adaptable directly), plain host directories you can `ls`/`cat`/point a local Python script at are more useful here. Both Compose files bind-mount the same host paths by design (same "switch modes without losing data" property named volumes would have given, without the opacity).

## Phase 1 algorithm stub

`controller/algorithms/stub.py`'s `EqualWeightStub` is wired to both `/aco` and `/mc` in Phase 1. It always returns the same weight regardless of observations, so the sampling/config-writer/change-counter plumbing is exercisable end-to-end before any ACO or Markov math exists (standing instruction: don't implement those until Phase 1 is verified). Expect the change counter to land at 1 per algorithm (the placeholder → primed transition) and stay there — that's the plumbing working correctly, not a bug.

## `runs.log` format

Chosen as JSON Lines (one JSON object per run) rather than the pipe-delimited format used for NGINX access logs. The access log format is fixed-width by NGINX's `log_format` directive; `runs.log` has no such constraint, and JSONL lets a future algorithm's change-count key show up in new lines without breaking any code that reads older ones — matches "adding a new algorithm should be straightforward" elsewhere in this project.

## Operational gotchas found while testing Phase 1

- **Both Compose files share a project name.** Docker Compose derives the project name from the directory by default, not the `-f` filename — so `docker-compose.full.yml` and `docker-compose.controller.yml` run from the same directory land in the *same* project/volume namespace. That's intentional (see "same named volumes" above), but it means running `docker compose -f docker-compose.controller.yml up` while the full-mode stack's `controller` service is still up does **not** start a fresh controller-only container — Compose matches by project+service name and just reattaches to the one already running. Always `docker compose -f <the other file> down` before switching modes from the same directory.
- **The actionable validation-failure message has one source of truth.** `validate_backends.py`'s detailed failure report (which hosts, the full checked list, which file to edit, which Compose file to use) originally only printed from that script's own `main()`. `entrypoint.py` and `sampler.py` both call the lower-level `validate()` function directly and, before this was caught, only logged a bare Python list on failure — silently dropping the actionable message exactly where an operator would actually see it (container startup). Fixed by extracting `print_failure_report()` as a shared function all three callers use. If a new caller of `validate()` is added, it must call `print_failure_report()` on failure too, or this regresses again.

## Scaling

Nothing in `upstream-hosts.txt` parsing, NGINX config generation, or the sampler hardcodes a backend count — scaling from 5 to 10 to 25 backends in **`docker-compose.controller.yml`** (external mode) is purely editing `upstream-hosts.txt`. **`docker-compose.full.yml`** (local mode) is the exception: each locally-built backend is its own Compose service (so its specific `LATENCY_MIN_MS`/`LATENCY_MAX_MS`/`DEGRADATION_MULTIPLIER` tuning can differ), so adding local backends beyond the default 5 means adding both a service block here and a line in `upstream-hosts.txt`. This is inherent to Compose needing an explicit service per container instance, not a shortcut taken in the controller logic.
