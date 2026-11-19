# syntax=docker/dockerfile:1
# python:3.12-slim, digest pinned so a rebuild cannot pick up a different image.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin timefence \
 && install -d -o timefence -g timefence -m 700 /data \
 && rm -f /etc/localtime /etc/timezone

WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY deploy/docker-entrypoint.sh /usr/local/bin/timefence-entrypoint
RUN pip install --no-deps . && rm -rf /app/src && chmod 755 /usr/local/bin/timefence-entrypoint

USER timefence:timefence
# TZ is optional: without it the app follows the host timezone when you mount
# /etc/localtime (see docs/docker.md), and otherwise falls back to UTC. The
# image's own /etc/localtime and /etc/timezone are removed above, so a mount is
# a regular file whose content can be matched instead of a symlink that keeps
# naming the image's Etc/UTC.
ENV DATA_DIR=/data \
    BASE_PATH=/ \
    LISTEN_HOST=127.0.0.1 \
    LISTEN_PORT=8099 \
    SECURE_COOKIES=true \
    LOG_LEVEL=INFO

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,sys,urllib.request as u; b=os.environ.get('BASE_PATH','/').rstrip('/'); p=os.environ.get('LISTEN_PORT','8099'); sys.exit(0 if u.urlopen(f'http://127.0.0.1:{p}{b}/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/timefence-entrypoint"]
