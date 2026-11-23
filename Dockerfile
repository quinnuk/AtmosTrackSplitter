FROM lscr.io/linuxserver/webtop:debian-xfce

USER root

ENV APP_DIR=/app \
    VENV_DIR=/opt/atmos-venv \
    ATMOS_RIPS_DIR=/rips \
    ATMOS_OUTPUT_DIR=/music-videos

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      fonts-dejavu-core \
      mkvtoolnix \
      python3 \
      python3-pip \
      python3-tk \
      python3-venv \
      xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR \${APP_DIR}
COPY . \${APP_DIR}

RUN python3 -m venv \${VENV_DIR} \
    && \${VENV_DIR}/bin/pip install --no-cache-dir --upgrade pip \
    && \${VENV_DIR}/bin/pip install --no-cache-dir -r requirements.txt \
    && printf '%s\n' \
      '[Desktop Entry]' \
      'Type=Application' \
      'Name=Atmos Track Splitter' \
      'Comment=Split Blu-ray Atmos albums into Plex-ready tracks' \
      'Exec=/usr/local/bin/atmos-track-splitter' \
      'Terminal=false' \
      'X-GNOME-Autostart-enabled=true' \
      > /etc/xdg/autostart/atmos-track-splitter.desktop \
    && printf '%s\n' \
      '#!/usr/bin/env bash' \
      'export HOME=/config' \
      'cd /app' \
      'exec /opt/atmos-venv/bin/python /app/main.py' \
      > /usr/local/bin/atmos-track-splitter \
    && chmod 0755 /usr/local/bin/atmos-track-splitter

EXPOSE 3000 3001
