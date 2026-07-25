#!/usr/bin/env python3
"""NGINX config generator -- runs once at container startup.

Reads upstream-hosts.txt and DEPLOY_MODE and writes every file NGINX needs
under CONF_DIR:

  rr.upstream.conf   -- static, unweighted, never touched again
  aco.upstream.conf  -- equal-weight placeholder, rewritten by config_writer
  mc.upstream.conf   -- equal-weight placeholder, rewritten by config_writer
  upstream-rl.conf   -- resolver + log_format + server{} with one location
                        block per algorithm path

Nothing here is hardcoded to a specific number of backends.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import (
    read_upstream_hosts,
    log,
    CONF_DIR,
    NGINX_STOCK_CONF_D,
    NGINX_INCLUDE_BOOTSTRAP_PATH,
    LOG_DIR,
    DEPLOY_MODE,
    BACKEND_PORT,
    NGINX_EXPERIMENT_PORT,
    DYNAMIC_ALGO_NAMES,
    STATIC_ALGO_NAMES,
    ALL_ALGO_NAMES,
)
from nginx.upstream_conf import write_upstream_conf, upstream_name

# Equal-weight placeholder used for aco/mc until the priming run computes
# real algorithm-derived weights. Any equal integer in [1,100] is a valid
# placeholder -- the specific value doesn't matter since all backends carry
# the same weight (pure round robin in practice until priming overwrites it).
EQUAL_WEIGHT_PLACEHOLDER = 50

RESOLVER_BY_MODE = {
    # Docker's embedded DNS server, resolves service names.
    "full": "resolver 127.0.0.11 ipv6=off;",
    # Docker DNS can't resolve external addresses -- fall back to a public
    # resolver. See AGENT.md for the tradeoff this implies.
    "external": "resolver 8.8.8.8;",
}

LOG_FORMAT_NAME = "upstream_rl"
LOG_FORMAT_DIRECTIVE = (
    f"log_format {LOG_FORMAT_NAME} "
    "'$msec | $uri | $upstream_addr | $upstream_response_time | "
    "$upstream_header_time | $status | $sent_http_x_degradation_state';"
)


def render_main_conf(hosts, deploy_mode, port, log_dir):
    if deploy_mode not in RESOLVER_BY_MODE:
        raise ValueError(f"unknown DEPLOY_MODE={deploy_mode!r}, expected one of {list(RESOLVER_BY_MODE)}")

    lines = [RESOLVER_BY_MODE[deploy_mode], "", LOG_FORMAT_DIRECTIVE, ""]
    lines.append(f"server {{")
    # Deliberately NOT port 80 / default_server: the stock NGINX image ships
    # conf.d/default.conf as the default server on port 80. Listening on a
    # separate port here avoids a duplicate-default-server conflict and
    # leaves that default location fully undisturbed. See AGENT.md.
    lines.append(f"    listen {port};")
    lines.append("")

    for algo in ALL_ALGO_NAMES:
        lines.append(f"    location /{algo} {{")
        lines.append(f"        proxy_pass http://{upstream_name(algo)}/;")
        lines.append(f"        access_log {log_dir}/{algo}.access.log {LOG_FORMAT_NAME};")
        lines.append(f"        error_log {log_dir}/{algo}.error.log;")
        lines.append("    }")
        lines.append("")

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def write_include_bootstrap():
    """A single static line dropped into the base image's real conf.d (not
    volume-backed, so it never masks conf.d/default.conf) that pulls in
    everything the controller generates from the separate, volume-backed
    CONF_DIR. Cheap to regenerate identically on every startup."""
    content = f"include {CONF_DIR}/*.conf;\n"
    tmp_path = f"{NGINX_INCLUDE_BOOTSTRAP_PATH}.tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, NGINX_INCLUDE_BOOTSTRAP_PATH)
    log("generate_config", f"wrote {NGINX_INCLUDE_BOOTSTRAP_PATH} -> include {CONF_DIR}/*.conf")


def main():
    os.makedirs(NGINX_STOCK_CONF_D, exist_ok=True)
    os.makedirs(CONF_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    write_include_bootstrap()

    hosts = read_upstream_hosts()
    log("generate_config", f"generating NGINX config for {len(hosts)} backend(s), DEPLOY_MODE={DEPLOY_MODE}")

    for algo in STATIC_ALGO_NAMES:
        path = os.path.join(CONF_DIR, f"{algo}.upstream.conf")
        write_upstream_conf(path, algo, hosts, BACKEND_PORT, weights=None)
        log("generate_config", f"wrote {path} (unweighted, static)")

    for algo in DYNAMIC_ALGO_NAMES:
        path = os.path.join(CONF_DIR, f"{algo}.upstream.conf")
        weights = {h: EQUAL_WEIGHT_PLACEHOLDER for h in hosts}
        write_upstream_conf(path, algo, hosts, BACKEND_PORT, weights=weights)
        log("generate_config", f"wrote {path} (equal-weight placeholder)")

    main_conf_path = os.path.join(CONF_DIR, "upstream-rl.conf")
    content = render_main_conf(hosts, DEPLOY_MODE, NGINX_EXPERIMENT_PORT, LOG_DIR)
    tmp_path = f"{main_conf_path}.tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, main_conf_path)
    log("generate_config", f"wrote {main_conf_path} (port={NGINX_EXPERIMENT_PORT})")


if __name__ == "__main__":
    main()
