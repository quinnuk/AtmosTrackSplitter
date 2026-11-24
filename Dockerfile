FROM python:3.12-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/config \
    RIPS_DIR=/rips \
    OUTPUT_DIR=/output \
    STAGING_DIR=/staging \
    PORT=8765

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      7zip \
      ca-certificates \
      ffmpeg \
      mkvtoolnix \
      tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-docker.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt

# The extraction engine remains the existing Windows/Linux-compatible code.
COPY extractor.py settings.py split_now.py ./
COPY auto_titles.py webapp.py ./
COPY static ./static

RUN mkdir -p /config /rips /output /staging \
 && chmod 0777 /config

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)" || exit 1

ENTRYPOINT ["tini", "--"]
# A single worker is deliberate: job state is held in memory. Threads handle
# scan/extraction work without creating multiple independent job registries.
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 600 webapp:app"]
