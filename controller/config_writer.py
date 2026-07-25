#!/usr/bin/env python3
"""Compares newly computed weights against the previous window, writes the
upstream conf if they changed, and reloads NGINX.

Change-count state is persisted to a small JSON file per algorithm under
LOG_DIR rather than kept only in the sampler loop's memory, because the
traffic generator (a separate process, invoked via `docker exec` against
the long-running container) needs to read the live count to print
`aco changes: N | mc changes: N` and to write the final count to
runs.log. Only one process (that algorithm's sampling loop) ever writes
its state file, so there's no write/write race -- traffic_generator only
reads.
"""
import json
import os
import subprocess

from common import log, CONF_DIR, LOG_DIR, BACKEND_PORT
from nginx.upstream_conf import write_upstream_conf


def state_path(algo_name):
    return os.path.join(LOG_DIR, f"{algo_name}.state.json")


def read_state(algo_name):
    path = state_path(algo_name)
    if not os.path.exists(path):
        return {"weights": None, "change_count": 0}
    with open(path) as f:
        return json.load(f)


def _write_state(algo_name, state):
    path = state_path(algo_name)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f)
    os.replace(tmp_path, path)


def reload_nginx():
    result = subprocess.run(["nginx", "-s", "reload"], capture_output=True, text=True)
    if result.returncode != 0:
        # Reload failures leave the previous config running -- not fatal to
        # the loop, but must be visible to the operator immediately.
        log("config_writer", f"nginx -s reload FAILED: {result.stderr.strip()}")
    return result.returncode == 0


def apply_weights(algo_name, hosts, new_weights):
    """new_weights: {host: int 1-100}. Returns (change_count, changed)."""
    state = read_state(algo_name)
    changed = state["weights"] != new_weights
    if changed:
        state["change_count"] += 1
        conf_path = os.path.join(CONF_DIR, f"{algo_name}.upstream.conf")
        write_upstream_conf(conf_path, algo_name, hosts, BACKEND_PORT, weights=new_weights)
        reload_nginx()
        log("config_writer", f"{algo_name}: weights changed (change_count={state['change_count']}) -> {new_weights}")
    state["weights"] = new_weights
    _write_state(algo_name, state)
    return state["change_count"], changed
