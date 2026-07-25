"""Rendering and writing of individual NGINX upstream {} blocks.

Shared by generate_config.py (initial rr/aco/mc confs at startup) and
config_writer.py (per-window aco/mc rewrites), so both paths produce
identical file formats.
"""
import os


def upstream_name(algo_name):
    return f"{algo_name}_backend"


def render_upstream_conf(algo_name, hosts, port, weights=None):
    """weights=None means unweighted (plain round robin, used only for the
    static rr conf). A dict means every host must have an integer weight
    1-100 (aco/mc, both at startup as an equal-weight placeholder and on
    every later rewrite)."""
    lines = [f"upstream {upstream_name(algo_name)} {{"]
    for host in hosts:
        if weights is None:
            lines.append(f"    server {host}:{port};")
        else:
            lines.append(f"    server {host}:{port} weight={weights[host]};")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def write_upstream_conf(path, algo_name, hosts, port, weights=None):
    """Write atomically (temp file + rename) so a concurrent `nginx -s
    reload` never reads a half-written file."""
    content = render_upstream_conf(algo_name, hosts, port, weights)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, path)
