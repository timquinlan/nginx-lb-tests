"""Rendering and writing of individual NGINX upstream {} blocks.

Shared by generate_config.py (initial rr/aco/mc confs at startup) and
config_writer.py (per-window aco/mc rewrites), so both paths produce
identical file formats.
"""
import os

# Every upstream block gets a shared-memory `zone` directive, unconditional
# -- not an opt-in per algorithm. Without one, NGINX keeps its selection
# state (round-robin/weighted-round-robin position, least_conn's live
# connection counts, random two's tiebreak) independently per worker
# process; with more than one worker, that means multiple independent
# selection cycles, and the *aggregate* distribution across all of them
# skews harder as worker count goes up -- confirmed live on /rr (2 workers,
# 6 workers, 6 workers+zone) before this was added, see AGENT.md. 64k is
# comfortably enough for this project's backend-pool sizes (5 by default,
# up to the 25 mentioned in the Scaling section) -- NGINX's own per-server
# shared-state footprint is on the order of a few hundred bytes, nowhere
# near 64k even at 25 servers.
DEFAULT_ZONE_SIZE = "64k"


def upstream_name(algo_name):
    return f"{algo_name}_backend"


def zone_name(algo_name):
    return f"{algo_name}_zone"


def render_upstream_conf(algo_name, hosts, port, weights=None, method=None):
    """weights=None means unweighted server entries (rr, and the
    method-based static algorithms below). A dict means every host must
    have an integer weight 1-100 (aco/mc, both at startup as an
    equal-weight placeholder and on every later rewrite).

    method, when given, is an NGINX upstream-block method directive (e.g.
    "random", "random two", "least_conn") rendered as the first line
    inside the block. For the static algorithms this is the *entire*
    selection mechanism, driven by NGINX itself (a dice roll or live
    connection count) with no weights and no controller involvement at
    all. combo is the one case that combines both: least_conn as the
    method, plus weight= entries the sampler keeps rewriting -- NGINX's
    least_conn then picks the server with the lowest
    active_connections/weight, so ACO's weights bias which of the
    least-busy servers wins a tie instead of overriding least_conn
    outright.

    Every block also gets a `zone` directive (see DEFAULT_ZONE_SIZE
    above), named after the algorithm so each path's shared state stays
    independent of every other path's."""
    lines = [f"upstream {upstream_name(algo_name)} {{"]
    lines.append(f"    zone {zone_name(algo_name)} {DEFAULT_ZONE_SIZE};")
    if method:
        lines.append(f"    {method};")
    for host in hosts:
        if weights is None:
            lines.append(f"    server {host}:{port};")
        else:
            lines.append(f"    server {host}:{port} weight={weights[host]};")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def write_upstream_conf(path, algo_name, hosts, port, weights=None, method=None):
    """Write atomically (temp file + rename) so a concurrent `nginx -s
    reload` never reads a half-written file."""
    content = render_upstream_conf(algo_name, hosts, port, weights, method)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, path)
