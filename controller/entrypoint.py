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


def pin_worker_processes():
    # worker_processes is a main-context directive -- it can't be set from
    # our conf.d includes (those are only included inside the http{}
    # block). Passing it via `nginx -g` doesn't work either: the base
    # image's nginx.conf already sets `worker_processes auto;`, and NGINX
    # refuses a directive supplied both ways ("duplicate directive"; this
    # actually crashed the container the first time this was tried -- see
    # AGENT.md). So this patches that one line in place instead.
    #
    # Pinned to 1 rather than left at the image's default `auto`: NGINX's
    # round-robin (weighted or not) state is kept per-worker, so with
    # worker_processes=auto (one per CPU core) each worker round-robins
    # independently and the *aggregate* distribution across all of them can
    # look meaningfully skewed even though every individual worker is
    # balancing correctly -- purely a multi-process artifact, not signal
    # from any algorithm. Since this project's stated goal is a
    # reproducible, precisely interpretable comparison between algorithms
    # (not raw throughput), a single worker trades away multi-core
    # concurrency for a clean, singular round-robin/weighted state to
    # measure against. Revisit if a future phase needs the throughput
    # headroom.
    with open(NGINX_MAIN_CONF_PATH) as f:
        content = f.read()
    patched, count = re.subn(r"worker_processes\s+\S+;", "worker_processes 1;", content, count=1)
    if count == 0:
        log("entrypoint", f"WARNING: no worker_processes directive found in {NGINX_MAIN_CONF_PATH}, leaving as-is")
        return
    with open(NGINX_MAIN_CONF_PATH, "w") as f:
        f.write(patched)
    log("entrypoint", f"pinned worker_processes to 1 in {NGINX_MAIN_CONF_PATH}")


def start_nginx():
    pin_worker_processes()
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
