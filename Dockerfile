# syntax=docker/dockerfile:1.7
# ark-watch image: one image, two roles (daemon / api), selected by compose `command`.

# --- SQLite >= 3.51.3 (Debian trixie ships 3.46; 3.51.3 fixes the WAL-reset
# corruption bug). Official amalgamation, pinned + checksum-verified. The
# sha256 was computed from the file whose SHA3-256 matched sqlite.org's
# published 454e45f6...0338 for sqlite-autoconf-3530400.tar.gz.
FROM python:3.12-slim-trixie AS sqlite
ARG SQLITE_TARBALL=2026/sqlite-autoconf-3530400.tar.gz
ADD --checksum=sha256:0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c \
    https://sqlite.org/${SQLITE_TARBALL} /tmp/sqlite.tar.gz
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev make \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir /tmp/sqlite && tar -xzf /tmp/sqlite.tar.gz -C /tmp/sqlite --strip-components=1 \
 && cd /tmp/sqlite \
 && CFLAGS="-O2 -DSQLITE_ENABLE_COLUMN_METADATA" ./configure --prefix=/opt/sqlite --disable-static \
 && make -j"$(nproc)" install

# --- Python deps from the committed lockfile (project code is copied later)
FROM ghcr.io/astral-sh/uv:0.12.23-python3.12-trixie-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# --- runtime
FROM python:3.12-slim-trixie
ARG GIT_SHA=unknown
ENV PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    ARKWATCH_DATA_DIR=/data \
    PYTHONPATH=/app \
    PATH=/app/.venv/bin:$PATH \
    LD_LIBRARY_PATH=/opt/sqlite/lib \
    GIT_SHA=${GIT_SHA}
LABEL org.opencontainers.image.revision=${GIT_SHA}

COPY --from=sqlite /opt/sqlite/lib/ /opt/sqlite/lib/
COPY --from=builder /app/.venv /app/.venv
WORKDIR /app
COPY --chmod=a+rX arkwatch ./arkwatch
COPY --chmod=a+rX config ./config

# /app/data -> /data: ~25 job modules still resolve ROOT/data/arkwatch.db
# (or cwd-relative data/arkwatch.db); the symlink keeps every path on the volume
RUN groupadd -g 10001 arkwatch \
 && useradd -u 10001 -g arkwatch -M -d /app -s /usr/sbin/nologin arkwatch \
 && mkdir /data && chown arkwatch:arkwatch /data && ln -s /data /app/data \
 && python -c "import sqlite3, sys; print('sqlite', sqlite3.sqlite_version); sys.exit(sqlite3.sqlite_version_info < (3, 51, 3))" \
 && python -c "from zoneinfo import ZoneInfo; ZoneInfo('Asia/Jakarta')"

USER 10001:10001
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=15s --start-period=120s --retries=3 \
    CMD ["python", "-m", "arkwatch", "healthcheck"]
CMD ["python", "-m", "arkwatch", "daemon"]
