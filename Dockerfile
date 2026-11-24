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
      ca-certificates \
      ffmpeg \
      mkvtoolnix \
      tar \
      tini \
      xz-utils \
 && rm -rf /var/lib/apt/lists/*

# Debian 13 currently packages 7-Zip 25.01, which cannot read some Blu-ray
# UDF 2.50 images containing real-time (ICB file type 249) stream files.
# Use the official upstream Linux binary containing the Blu-ray UDF fix.
ADD --checksum=sha256:2b539cf067b2ad48e1baa5093dbaa8818b73542d1222a0ffde9dc5f2922ea62e \
    https://github.com/ip7z/7zip/releases/download/26.06/7z2606-linux-x64.tar.xz \
    /tmp/7zip.tar.xz
RUN mkdir -p /tmp/7zip \
 && tar -xJf /tmp/7zip.tar.xz -C /tmp/7zip \
 && install -m 0755 /tmp/7zip/7zz /usr/local/bin/7z \
 && /usr/local/bin/7z i | grep -q '26.06' \
 && rm -rf /tmp/7zip /tmp/7zip.tar.xz

WORKDIR /app
COPY requirements-docker.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt

# The extraction engine remains the existing Windows/Linux-compatible code.
COPY extractor.py settings.py split_now.py auto_titles.py webapp.py qbittorrent.py iso_support.py poster_art.py ./
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
