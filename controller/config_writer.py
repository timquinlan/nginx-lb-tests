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
import csv
import json
import os
import subprocess

from common import log, now_ms, now_iso, CONF_DIR, LOG_DIR, BACKEND_PORT, DYNAMIC_ALGO_METHODS
from nginx.upstream_conf import write_upstream_conf


def state_path(algo_name):
    return os.path.join(LOG_DIR, f"{algo_name}.state.json")


def weight_history_path(algo_name):
    return os.path.join(LOG_DIR, f"{algo_name}.weights.csv")


def _append_weight_history(algo_name, hosts, weights):
    """Phase 4 detail data: the applied integer weight for every backend,
    every window, regardless of whether it changed from the prior window
    -- so a time series can be plotted with no gaps to fill in. One file
    per algorithm, wide-form: `timestamp_ms, timestamp_iso, <one column per
    backend>`. Chosen over long-form (one row per backend) specifically so
    a line chart -- x=time, y=weight, one line per backend -- is a direct
    `df.set_index('timestamp_ms').plot()` away, with no pivot step first.
    Column order is fixed to `hosts` (the canonical order from
    upstream-hosts.txt), not whatever order a given algorithm's internal
    dict happens to iterate in that window, since a wide CSV's columns
    have to stay stable for the life of the file.

    Tradeoff accepted: if the backend pool in upstream-hosts.txt changes
    between container restarts that both append to the same (persistent,
    bind-mounted) log directory, the column set won't match the original
    header partway through the file. Rather than build schema-migration
    logic for that, the expectation is: clear or rename ./logs before
    starting a run against a genuinely different backend pool, the same
    way you'd want to for a clean comparison anyway. Both timestamp forms
    are written: epoch ms for precise sorting/joins against the access
    logs (which use $msec), ISO for a human to eyeball directly. This is
    the one piece of Stream 1 (controller-internal state) that Phase 4
    actually reads -- see AGENT.md."""
    path = weight_history_path(algo_name)
    is_new = not os.path.exists(path)
    timestamp_ms = now_ms()
    timestamp_iso = now_iso()
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp_ms", "timestamp_iso"] + hosts)
        writer.writerow([timestamp_ms, timestamp_iso] + [weights[h] for h in hosts])


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
        method = DYNAMIC_ALGO_METHODS.get(algo_name)
        write_upstream_conf(conf_path, algo_name, hosts, BACKEND_PORT, weights=new_weights, method=method)
        reload_nginx()
        log("config_writer", f"{algo_name}: weights changed (change_count={state['change_count']}) -> {new_weights}")
    state["weights"] = new_weights
    _write_state(algo_name, state)
    _append_weight_history(algo_name, hosts, new_weights)
    return state["change_count"], changed
