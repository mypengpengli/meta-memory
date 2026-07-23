ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ARG META_MEMORY_UID=10001
ARG META_MEMORY_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    META_MEMORY_CONFIG=/config/config.toml \
    META_MEMORY_STORE=/data/store \
    META_MEMORY_AGENTS_FILE=/config/agents.json \
    META_MEMORY_BACKUP_DIR=/backups \
    META_MEMORY_UID=${META_MEMORY_UID} \
    META_MEMORY_GID=${META_MEMORY_GID} \
    HOME=/home/meta-memory

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends tzdata util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${META_MEMORY_GID}" meta-memory \
    && useradd --uid "${META_MEMORY_UID}" --gid "${META_MEMORY_GID}" \
        --home-dir /home/meta-memory --create-home --shell /usr/sbin/nologin meta-memory

WORKDIR /opt/meta-memory

COPY pyproject.toml README.md ./
COPY meta_memory ./meta_memory
COPY migrations ./migrations
COPY scripts ./scripts

RUN python -m pip install .

COPY docker/entrypoint.sh /usr/local/bin/meta-memory-entrypoint
COPY docker/maintenance-loop.sh /usr/local/bin/meta-memory-maintenance-loop
COPY docker/backup.sh /usr/local/bin/meta-memory-backup

RUN chmod 0755 \
        /usr/local/bin/meta-memory-entrypoint \
        /usr/local/bin/meta-memory-maintenance-loop \
        /usr/local/bin/meta-memory-backup \
    && install -d -o meta-memory -g meta-memory -m 0750 \
        /data /data/store /data/.container-runtime /config /backups

EXPOSE 8765

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/readyz', timeout=2).read()"]

ENTRYPOINT ["meta-memory-entrypoint"]
CMD ["meta-memory", "--config", "/config/config.toml", "serve", "--store", "/data/store", "--agents-file", "/config/agents.json", "--host", "0.0.0.0", "--port", "8765"]
