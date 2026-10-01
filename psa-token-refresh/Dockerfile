ARG BUILD_VERSION="1.1.0"
ARG BUILD_ARCH="amd64"

FROM python:3.12-bookworm

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="psa-token-refresh" \
    org.opencontainers.image.description="Renews PSA/Stellantis tokens for evcc"

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    QTWEBENGINE_DISABLE_SANDBOX=1 \
    QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu" \
    PYTHONUNBUFFERED=1

# QtWebEngine braucht diese Laufzeit-Bibliotheken; Xvfb liefert den "Display" headless
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb \
      qt5dxcb-plugin \
      libasound2 \
      libxkbcommon0 \
      libxdamage1 \
      libnss3 \
      libxcomposite1 \
      libxcursor1 \
      libxtst6 \
      libxrandr2 \
      libxi6 \
      libgl1 \
      libegl1 \
      libfontconfig1 \
      fonts-liberation \
      ca-certificates \
      tzdata \
 && rm -rf /var/lib/apt/lists/*

# Deliberately unpinned (same as the upstream helper); PyQt5/PyQtWebEngine are
# x86_64-only wheels -> see arch: [amd64] in config.yaml
RUN pip install --no-cache-dir PyQt5 PyQtWebEngine requests

WORKDIR /app
COPY psa-browser-token.py evcc_api.py patch_evcc_yaml.py \
     orchestrator.py run.sh /app/
RUN chmod a+x /app/run.sh

ENTRYPOINT ["/app/run.sh"]
