"""Shared constants and helpers used across controller scripts.

Kept dependency-free (stdlib only) since this same module is imported by
validate_backends.py, the NGINX config generator, config_writer.py,
sampler.py, and traffic_generator.py -- all of which may run as short-lived
one-off invocations via `docker exec`.
"""
import datetime
import os
import re
import socket
import sys

# --- Paths -----------------------------------------------------------------
# CONF_DIR is where the controller writes its *own* generated confs
# (rr_control/leastconn/aco_wrr/mc_wrr/aco_lc/mc_lc upstream blocks + the
# main server block). It is deliberately
# NOT /etc/nginx/conf.d: a bind mount (or named volume) mounted straight
# onto conf.d would overlay (and hide) the base image's own
# conf.d/default.conf the first time the mount is used, which breaks the
# "default NGINX location keeps working, undisturbed" property this
# project relies on for a free liveness check on port 80. Instead,
# generate_config.py drops one static, non-mount-backed include file into
# the real /etc/nginx/conf.d that just does `include {CONF_DIR}/*.conf;`
# -- see NGINX_STOCK_CONF_D below.
CONF_DIR = os.environ.get("CONF_DIR", "/var/lib/nginx-lb-tests/nginx-conf.d")
NGINX_STOCK_CONF_D = "/etc/nginx/conf.d"
NGINX_INCLUDE_BOOTSTRAP_PATH = os.path.join(NGINX_STOCK_CONF_D, "nginx-lb-tests-include.conf")

# LOG_DIR and HOSTS_FILE are backed by a named volume / bind mount
# respectively so they persist and are user-editable independent of the
# image.
LOG_DIR = os.environ.get("LOG_DIR", "/var/log/nginx-lb-tests")
HOSTS_FILE = os.environ.get("HOSTS_FILE", "/app/upstream-hosts.txt")

RUNS_LOG_PATH = os.path.join(LOG_DIR, "runs.log")

# --- Deployment mode ---------------------------------------------------------
DEPLOY_MODE = os.environ.get("DEPLOY_MODE", "full")  # "full" | "external"

# --- Backend / NGINX network parameters -------------------------------------
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8080"))
NGINX_EXPERIMENT_PORT = int(os.environ.get("NGINX_EXPERIMENT_PORT", "8080"))

# --- Timing ------------------------------------------------------------------
TICK_SECONDS = float(os.environ.get("TICK_SECONDS", "60"))

# --- Algorithms --------------------------------------------------------------
# Round robin is static/unweighted and has no controller module (see
# AGENT.md). ACO and MC are the underlying algorithms whose weights the
# sampler rewrites every sampling window; each is exposed on two paths --
# a plain weighted-round-robin upstream (*_wrr) and a weighted-least_conn
# one (*_lc, see DYNAMIC_ALGO_METHODS below).
#
# leastconn is also static, same as rr_control: NGINX's own upstream-block
# method directive (see STATIC_ALGO_METHODS below) does the selection
# internally, every request -- no weight computation, no sampling loop, no
# weights.csv, no change counter. It exists to compare against this
# project's adaptive algorithms the same way rr_control does, just with a
# different built-in NGINX mechanism driving selection. `random`/`random2`
# were removed -- see AGENT.md's "History" section.
STATIC_ALGO_NAMES = ("rr_control", "leastconn")
# aco_wrr/mc_wrr pair a dedicated ACO/MarkovChain instance with plain
# weighted round robin (no method directive). aco_lc/mc_lc pair the same
# two algorithms with NGINX's least_conn method instead, layering a
# learned weight signal on top of least_conn's live one -- see
# DYNAMIC_ALGO_METHODS below. All four have their own independent
# algorithm instance (see sampler.py's ALGORITHMS dict) -- aco_wrr and
# aco_lc never share pheromone state, same for mc_wrr/mc_lc.
DYNAMIC_ALGO_NAMES = ("aco_wrr", "mc_wrr", "aco_lc", "mc_lc")
ALL_ALGO_NAMES = STATIC_ALGO_NAMES + DYNAMIC_ALGO_NAMES

# NGINX upstream-block method directive per static algorithm, written as
# the first line inside the block (see nginx/upstream_conf.py). None means
# no directive at all -- NGINX's own default upstream method is already
# plain round robin, so omitting the line reproduces rr_control's
# historical unweighted conf byte-for-byte.
STATIC_ALGO_METHODS = {
    "rr_control": None,
    "leastconn": "least_conn",
}

# Same idea as STATIC_ALGO_METHODS, for the dynamic (weight-writing)
# algorithms -- aco_wrr/mc_wrr are plain weighted-round-robin (no method
# directive), while aco_lc/mc_lc layer their respective learned weights on
# top of NGINX's least_conn method (a "weighted least_conn" upstream:
# NGINX picks the server with the lowest active_connections/weight, so a
# higher-weight backend can carry more concurrent connections before it's
# judged "as busy" as a lower-weight one).
DYNAMIC_ALGO_METHODS = {
    "aco_wrr": None,
    "mc_wrr": None,
    "aco_lc": "least_conn",
    "mc_lc": "least_conn",
}

MIN_WEIGHT = 1
MAX_WEIGHT = 100

# The placeholder weight generate_config.py writes for the dynamic algorithms before priming
# runs, and the constant weight stub.py's reference EqualWeightStub always
# returns. Any equal integer in [MIN_WEIGHT, MAX_WEIGHT] would be a valid
# placeholder (all backends carrying the same weight is pure round robin in
# practice); shared here so the two aren't two independent "50"s that happen
# to agree.
EQUAL_WEIGHT_PLACEHOLDER = 50


def read_upstream_hosts(path=None):
    """Parse upstream-hosts.txt: one hostname/IP per line, '#' comments,
    blank lines ignored. This is the single source of truth for the backend
    pool -- nothing else in the codebase hardcodes a backend count."""
    path = path or HOSTS_FILE
    hosts = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            hosts.append(line)
    if not hosts:
        raise ValueError(f"{path} contains no backend hosts")
    return hosts


def now_ms():
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log(component, message, stream=sys.stderr):
    """Stream 1 (operational) logging -- stderr/stdout only, never analyzed
    by Phase 4 tooling. See AGENT.md 'Two Distinct Data Streams'."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    print(f"[{ts}] [{component}] {message}", file=stream, flush=True)


def read_location_paths(main_conf_path=None):
    """Read algorithm paths back out of the generated NGINX config, rather
    than hardcoding /rr_control, /aco_wrr, /mc_wrr anywhere downstream (traffic generator
    requirement)."""
    main_conf_path = main_conf_path or os.path.join(CONF_DIR, "nginx-lb-tests.conf")
    paths = []
    pattern = re.compile(r"^\s*location\s+(/\S+)\s*\{")
    with open(main_conf_path) as f:
        for line in f:
            m = pattern.match(line)
            if m:
                paths.append(m.group(1))
    return paths


def resolve_ip_to_host_map(hosts, port=None):
    """$upstream_addr in the NGINX access log is a resolved ip:port, never
    the original hostname from upstream-hosts.txt (NGINX resolves each
    `server host:port;` entry at config-load/reload time). Build the
    reverse map so log lines can be attributed back to the backend that
    upstream-hosts.txt named. Best-effort: a host that fails to resolve
    right now is skipped rather than raising, since it may still be
    starting up (see validate_backends.py for the retry logic used before
    this point in the sequence).
    """
    port = port or BACKEND_PORT
    mapping = {}
    for host in hosts:
        try:
            ip = socket.gethostbyname(host)
        except socket.gaierror:
            continue
        mapping[f"{ip}:{port}"] = host
    return mapping
