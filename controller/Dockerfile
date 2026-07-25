FROM nginx:stable

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY controller/ /app/

CMD ["python3", "entrypoint.py"]
