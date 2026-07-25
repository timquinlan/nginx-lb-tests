#!/usr/bin/env python3
"""Backend validation -- the very first step in the entrypoint sequence.

Pings every host in upstream-hosts.txt. In full mode, Docker service
hostnames may not resolve yet if backend containers are still starting, so
each host gets a few retries with a short delay before being declared
unreachable. Exits non-zero (and prints an actionable message) if any host
never responds, so Docker reports the container as failed rather than
silently running a broken experiment.
"""
import os
import subprocess
import sys
import time

from common import read_upstream_hosts, log, HOSTS_FILE, DEPLOY_MODE

RETRIES = int(os.environ.get("VALIDATE_RETRIES", "3"))
RETRY_DELAY_SECONDS = float(os.environ.get("VALIDATE_RETRY_DELAY", "2"))
PING_TIMEOUT_SECONDS = 2


def ping_once(host):
    result = subprocess.run(
        ["ping", "-c", "1", "-W", str(PING_TIMEOUT_SECONDS), host],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def validate(hosts):
    unreachable = []
    for host in hosts:
        ok = False
        for attempt in range(1, RETRIES + 1):
            if ping_once(host):
                ok = True
                break
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
        status = "OK" if ok else "UNREACHABLE"
        log("validate_backends", f"{host}: {status} (checked {min(attempt, RETRIES)}/{RETRIES} attempts)")
        if not ok:
            unreachable.append(host)
    return unreachable


def print_failure_report(hosts, unreachable):
    """The actionable, specific error message the spec calls for: which
    hosts failed, the full checked list, which file to edit, and which
    Compose file matches the current DEPLOY_MODE. Shared by every caller of
    validate() (entrypoint.py, sampler.py, and this module's own main())
    so the message is never silently dropped down to a bare host list --
    that was a real bug caught while wiring this up (see AGENT.md)."""
    log("validate_backends", "=" * 60)
    log("validate_backends", "BACKEND VALIDATION FAILED")
    log("validate_backends", f"Unreachable host(s): {', '.join(unreachable)}")
    log("validate_backends", f"All host(s) checked: {', '.join(hosts)}")
    log("validate_backends", f"Fix: edit {HOSTS_FILE} to remove/correct unreachable entries.")
    if DEPLOY_MODE == "full":
        log(
            "validate_backends",
            "DEPLOY_MODE=full expects Docker service hostnames -- check "
            "docker-compose.full.yml service names match upstream-hosts.txt, "
            "and that backend containers actually started.",
        )
    else:
        log(
            "validate_backends",
            "DEPLOY_MODE=external expects reachable external hostnames/IPs -- "
            "verify network access from the controller container, or switch to "
            "docker-compose.full.yml for local self-contained backends.",
        )
    log("validate_backends", "=" * 60)


def main():
    hosts = read_upstream_hosts()
    log("validate_backends", f"validating {len(hosts)} host(s) from {HOSTS_FILE} (DEPLOY_MODE={DEPLOY_MODE})")
    unreachable = validate(hosts)

    if unreachable:
        print_failure_report(hosts, unreachable)
        sys.exit(1)

    log("validate_backends", "all hosts reachable")


if __name__ == "__main__":
    main()
