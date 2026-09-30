FROM node:24-bookworm-slim AS web
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY controller ./controller
RUN pip install --no-cache-dir . && useradd --system --uid 10001 controller && mkdir /data && chown controller /data
COPY --from=web /app/frontend/dist ./frontend/dist
USER controller
ENV CONTROLLER_DATABASE=/data/controller.sqlite3
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/api/health')"
CMD ["uvicorn", "controller.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8790", "--workers", "1"]
