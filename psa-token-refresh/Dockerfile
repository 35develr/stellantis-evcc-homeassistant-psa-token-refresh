ARG BUILD_VERSION="1.6.1"
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
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata curl \
 && rm -rf /var/lib/apt/lists/*

# --- default engine: Playwright/Chromium, headless -------------------------------------
# This is what makes the add-on work on aarch64 as well (PyQtWebEngine has no Linux aarch64
# wheels). --only-shell installs the headless shell only (~260 MB unpacked instead of the
# full Chrome for Testing build). Bump the pinned version together with the docs.
RUN pip install --no-cache-dir "playwright==1.64.0" requests \
 && playwright install --with-deps --only-shell chromium \
 && chmod -R a+rX /ms-playwright

# --- legacy engine: PyQt5/QtWebEngine, amd64 image only --------------------------------
# BUILD_ARCH is passed by the Supervisor for every architecture it builds (build.py).
# Xvfb provides the headless "display" QtWebEngine needs.
RUN if [ "${BUILD_ARCH}" = "amd64" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends \
           xvfb qt5dxcb-plugin libasound2 libxkbcommon0 libxdamage1 libnss3 \
           libxcomposite1 libxcursor1 libxtst6 libxrandr2 libxi6 libgl1 \
           libegl1 libfontconfig1 fonts-liberation \
      && rm -rf /var/lib/apt/lists/* \
      && pip install --no-cache-dir PyQt5 PyQtWebEngine ; \
    else \
      echo "arch ${BUILD_ARCH}: qt backend not built - PyQtWebEngine has no Linux aarch64 wheels" ; \
    fi

WORKDIR /app
COPY psa-browser-token.py evcc_api.py patch_evcc_yaml.py \
     orchestrator.py run.sh /app/
RUN chmod a+x /app/run.sh

# The version has to live in the image: orchestrator.py compares its own version with the
# one the Supervisor reports for the installed app and warns when the two differ (that is
# what catches hand-edited files or a half-finished update). ARG BUILD_VERSION is passed by
# the Supervisor for every build (supervisor/apps/build.py).
RUN printf '%s\n' "${BUILD_VERSION}" > /app/VERSION

ENTRYPOINT ["/app/run.sh"]
