FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PORT=5555

WORKDIR /app

# ffmpeg handles the actual encoding; the Intel VA-API drivers let it reach
# QuickSync through /dev/dri. When that fails the app falls back to libx264.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        intel-media-va-driver \
        i965-va-driver \
        va-driver-all \
        vainfo \
        gosu \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user. The entrypoint aligns its UID/GID with PUID/PGID
# and the mapped render node's group, then drops privileges before starting app.
RUN groupadd -r cctv && \
    useradd -r -g cctv -m -d /home/cctv -s /usr/sbin/nologin cctv && \
    (groupadd -r render || true) && \
    usermod -aG video,render cctv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir -p /app/data

VOLUME ["/app/data"]
EXPOSE 5555

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5555/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "run.py"]
