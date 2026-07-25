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
import signal
import subprocess
import sys
import time

from common import log, read_upstream_hosts
import validate_backends
from nginx import generate_config
import sampler

NGINX_STARTUP_GRACE_SECONDS = 1.0


def start_nginx():
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
