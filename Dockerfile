FROM node:24-bookworm-slim AS web
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends adb ca-certificates && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY controller ./controller
RUN pip install --no-cache-dir . && python -m controller.screen_install /opt/scrcpy-server-v3.3.4 && useradd --system --uid 10001 --home-dir /data/adb controller && mkdir -p /data/adb && chown -R controller /data
COPY third_party/ ./third_party/
COPY --chmod=755 docker-entrypoint.sh ./docker-entrypoint.sh
COPY --from=web /app/frontend/dist ./frontend/dist
USER controller
ENV CONTROLLER_DATABASE=/data/controller.sqlite3
ENV SCREEN_SERVER_PATH=/opt/scrcpy-server-v3.3.4
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/api/health')"
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "controller.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8790", "--workers", "1"]
