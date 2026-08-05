#!/usr/bin/env python3
"""Container entrypoint. Runs the automatic setup sequence, then supervises
NGINX for the life of the container:

    validate backends -> generate NGINX configs -> start NGINX (background)
    -> prime algorithm state (reloads NGINX) -> start sampling loops
    -> supervise NGINX (forward signals, exit with its exit code)

See AGENT.md "Entrypoint / Orchestration Model" for why NGINX is supervised
rather than `exec`'d at the end: it has to already be running before
priming's `nginx -s reload`, so a second `exec nginx` here would conflict
with the one already bound to the port.

The traffic generator is NOT started by this script -- it's a separate,
manually-triggered step: `docker exec <container> python3 traffic_generator.py ...`
"""
import os
import re
import signal
import subprocess
import sys
import time

from common import log, read_upstream_hosts
import validate_backends
from nginx import generate_config
import sampler

NGINX_STARTUP_GRACE_SECONDS = 1.0
NGINX_MAIN_CONF_PATH = "/etc/nginx/nginx.conf"
# See pin_worker_processes() below -- moving off 1 without `zone`
# directives on the upstream blocks reintroduces the per-worker skew
# artifact that pinning to 1 originally fixed. Not yet paired with zone.
WORKER_PROCESSES_COUNT = int(os.environ.get("WORKER_PROCESSES_COUNT", "4"))

# Default 1024 (the base image's own default, never previously overridden)
# runs out at real experiment volume -- confirmed live: at high rps against
# the 150-600ms backend profile, `1024 worker_connections are not enough`
# fired thousands of times per run, inflating measured TTFB with
# connection-ceiling artifacts that have nothing to do with backend latency
# or algorithm choice. 8192 gives comfortable headroom well beyond what a
# single-instance run at these defaults needs.
WORKER_CONNECTIONS_COUNT = int(os.environ.get("WORKER_CONNECTIONS_COUNT", "8192"))


def pin_worker_connections():
    # events{} is also main-context (like worker_processes above), so this
    # patches the base image's nginx.conf in place rather than trying to
    # supply it from our conf.d includes (only valid inside http{}) or via
    # `nginx -g` (would collide with the base image's own directive, same
    # "duplicate directive" crash worker_processes hit -- see AGENT.md).
    with open(NGINX_MAIN_CONF_PATH) as f:
        content = f.read()
    patched, count = re.subn(r"worker_connections\s+\d+;", f"worker_connections {WORKER_CONNECTIONS_COUNT};", content, count=1)
    if count == 0:
        log("entrypoint", f"WARNING: no worker_connections directive found in {NGINX_MAIN_CONF_PATH}, leaving as-is")
        return
    with open(NGINX_MAIN_CONF_PATH, "w") as f:
        f.write(patched)
    log("entrypoint", f"pinned worker_connections to {WORKER_CONNECTIONS_COUNT} in {NGINX_MAIN_CONF_PATH}")


def pin_worker_processes():
    # worker_processes is a main-context directive -- it can't be set from
    # our conf.d includes (those are only included inside the http{}
    # block). Passing it via `nginx -g` doesn't work either: the base
    # image's nginx.conf already sets `worker_processes auto;`, and NGINX
    # refuses a directive supplied both ways ("duplicate directive"; this
    # actually crashed the container the first time this was tried -- see
    # AGENT.md). So this patches that one line in place instead.
    #
    # Pinned to 4 rather than left at the image's default `auto` (one per
    # CPU core, uncapped) or the earlier value of 1. NGINX's round-robin
    # (weighted or not), least_conn, and random-two state are all kept
    # per-worker *unless* the owning upstream block has a `zone`
    # directive -- without one, more workers means more independent
    # round-robin/connection-count cycles, and the *aggregate* distribution
    # across them skews harder as worker count goes up (confirmed live,
    # see AGENT.md: 2 workers vs. 6 workers vs. 6 workers+zone on /rr_control).
    # WORKER_PROCESSES_COUNT going to 4 here is step one of that move --
    # `zone` directives on the upstream blocks are the other half and are
    # not yet added (see AGENT.md/memory), so every path currently
    # inherits the same aggregate-skew artifact the single-worker pin was
    # originally introduced to avoid, until that follow-up lands.
    with open(NGINX_MAIN_CONF_PATH) as f:
        content = f.read()
    patched, count = re.subn(r"worker_processes\s+\S+;", f"worker_processes {WORKER_PROCESSES_COUNT};", content, count=1)
    if count == 0:
        log("entrypoint", f"WARNING: no worker_processes directive found in {NGINX_MAIN_CONF_PATH}, leaving as-is")
        return
    with open(NGINX_MAIN_CONF_PATH, "w") as f:
        f.write(patched)
    log("entrypoint", f"pinned worker_processes to {WORKER_PROCESSES_COUNT} in {NGINX_MAIN_CONF_PATH}")


def start_nginx():
    pin_worker_processes()
    pin_worker_connections()
    proc = subprocess.Popen(["nginx", "-g", "daemon off;"])
    time.sleep(NGINX_STARTUP_GRACE_SECONDS)
    if proc.poll() is not None:
        log("entrypoint", f"nginx exited immediately with code {proc.returncode} -- check generated config in CONF_DIR")
        sys.exit(proc.returncode or 1)
    return proc


def main():
    hosts = read_upstream_hosts()
    unreachable = validate_backends.validate(hosts)
    if unreachable:
        validate_backends.print_failure_report(hosts, unreachable)
        sys.exit(1)

    generate_config.main()

    nginx_proc = start_nginx()
    log("entrypoint", f"nginx started (pid={nginx_proc.pid})")

    def forward_signal(signum, _frame):
        log("entrypoint", f"received signal {signum}, forwarding to nginx (pid={nginx_proc.pid})")
        nginx_proc.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)

    # Priming reloads NGINX via config_writer once initial weights are
    # computed; sampler.main() then starts the continuous per-algorithm
    # sampling loops as background threads and returns without blocking.
    sampler.main()

    log(
        "entrypoint",
        "setup complete: NGINX primed, sampling loops running. Start an experiment run with "
        "`docker exec <container> python3 traffic_generator.py --tick T --rps R --duration D`",
    )

    exit_code = nginx_proc.wait()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
