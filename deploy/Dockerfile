FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    XDG_CACHE_HOME=/ms-playwright/cache

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gcc \
        libsqlite3-dev \
        fonts-liberation \
        libnss3 \
        libnspr4 \
        libatk-bridge2.0-0 \
        libdrm2 \
        libxkbcommon0 \
        libgbm1 \
        libasound2 \
        libxshmfence1 \
        libx11-6 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxrandr2 \
        libpango-1.0-0 \
        libcairo2 \
        libgtk-3-0 \
        libdbus-glib-1-2 \
        libdbus-1-3 \
        libxt6 \
        libx11-xcb1 \
        libxtst6 \
        libxcursor1 \
        libxi6 \
        libxrender1 \
        libpci3 \
        libegl1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && python -m playwright install-deps firefox

# Camoufox (BROWSER_ENGINE=camoufox): kernel, GeoIP .mmdb and fpgen model are baked
# into the image under $XDG_CACHE_HOME/camoufox; runtime launches never download.
RUN python -m camoufox fetch \
    && python -c "from camoufox.fpgen_model import is_pinned, load_fpgen; load_fpgen(); assert is_pinned(); from camoufox.pkgman import camoufox_path, launch_path; print(launch_path(camoufox_path(download_if_missing=False)))" \
    && python -c "from camoufox.geolocation import get_mmdb_path; assert get_mmdb_path('ipv4').exists()"

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        fonts-dejavu-core \
        fonts-noto-color-emoji \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY app /app/app
# The whole extension: the runner copies it per run. .dockerignore keeps out the personal private-config.mjs.
COPY extensions/chatgpt-signup /app/extensions/chatgpt-signup

EXPOSE 8008

CMD ["sh", "-c", "rm -f /tmp/.X99-lock /tmp/.X11-unix/X99; mkdir -p /tmp/.X11-unix; Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 & export DISPLAY=${DISPLAY:-:99}; exec uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT:-8008}"]
