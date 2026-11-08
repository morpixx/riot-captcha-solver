# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12
ARG CAMOUFOX_VERSION=0.5.6

# Camoufox/Firefox и GeoIP скачиваются отдельным слоем, чтобы правки кода
# не вызывали повторную загрузку браузера (~2.5 ГБ).
FROM python:${PYTHON_VERSION}-slim-bookworm AS camoufox
ARG CAMOUFOX_VERSION
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/opt/camoufox-cache

RUN pip install --no-cache-dir "camoufox[geoip]==${CAMOUFOX_VERSION}"
RUN python -m camoufox fetch \
 && (python -c "from camoufox.geolocation import download_mmdb; download_mmdb()" \
     || echo "WARN: GeoIP prefetch failed; it will be downloaded on first use")

FROM python:${PYTHON_VERSION}-slim-bookworm AS app
ARG CAMOUFOX_VERSION
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/opt/camoufox-cache \
    HOME=/home/app \
    DATA_DIR=/app/data

# Системные библиотеки Firefox/Camoufox. Точный набор для Debian bookworm.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gosu tini \
      fonts-liberation fonts-noto-color-emoji \
      libasound2 libatk-bridge2.0-0 libatk1.0-0 libcairo2 libcups2 \
      libdbus-1-3 libdbus-glib-1-2 libdrm2 libegl1 libexpat1 libfontconfig1 \
      libgbm1 libgl1 libglib2.0-0 libgtk-3-0 libnspr4 libnss3 \
      libpango-1.0-0 libpci3 libstdc++6 libudev1 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-dri3-0 libxcb-glx0 libxcb-icccm4 \
      libxcb-image0 libxcb-keysyms1 libxcb-present0 libxcb-randr0 libxcb-render0 \
      libxcb-shape0 libxcb-shm0 libxcb-sync1 libxcb-util1 libxcb-xfixes0 \
      libxcb-xinerama0 libxcb-xkb1 libxcomposite1 libxdamage1 libxext6 \
      libxfixes3 libxkbcommon-x11-0 libxkbcommon0 libxrandr2 libxrender1 \
      libxt6 libxtst6 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app
WORKDIR /app

COPY --from=camoufox /opt/camoufox-cache /opt/camoufox-cache
RUN chown -R app:app /opt/camoufox-cache

COPY pyproject.toml README.md server.py cookie_extractor.py ./
COPY src ./src
RUN pip install --no-cache-dir "camoufox[geoip]==${CAMOUFOX_VERSION}" . \
 && chown -R app:app /app

COPY scripts ./scripts
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /app/data/input /app/data/output \
 && chown -R app:app /app

EXPOSE 8000
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "server.py"]
