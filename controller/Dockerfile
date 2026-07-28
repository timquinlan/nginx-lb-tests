FROM nginx:stable

# python3-matplotlib (not pip) deliberately: Debian's system Python
# refuses global pip installs (PEP 668, "externally managed environment"),
# and matplotlib is available as a regular apt package -- same install
# path as iputils-ping, no venv or --break-system-packages workaround
# needed. See AGENT.md for the analysis/ dependency tradeoff this is part
# of; analysis/requirements.txt covers the (separate) host-side pip case.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 iputils-ping python3-matplotlib \
    && rm -rf /var/lib/apt/lists/*

# LOG_DIR was previously only a Python-side default (controller/common.py),
# never an actual environment variable -- fine for every controller script
# invoked with common.py's own default already baked in, but it meant a
# bare `docker exec <container> python3 analysis/analyze.py` (documented in
# analysis/README.md as a supported way to run this) saw no LOG_DIR at all
# and fell back to analysis/log_reader.py's own host-side default
# (./logs, relative -- wrong inside the container). Exporting it as a real
# ENV makes it visible to any process in the container, one-off docker
# exec included, and gives both common.py and log_reader.py the same
# value without duplicating the literal.
ENV LOG_DIR=/var/log/nginx-lb-tests

WORKDIR /app
COPY controller/ /app/
COPY analysis/ /app/analysis/

CMD ["python3", "entrypoint.py"]
