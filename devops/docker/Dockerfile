FROM node:18-alpine AS mfe-build
WORKDIR /workspace/mfe
COPY frontend/PE_Dashboard_MFE/source/package*.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/PE_Dashboard_MFE/source/ ./
RUN npm run build
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PE_STATE_DIR=/data \
    PE_UI_MODE=bundled_mfe \
    PE_COOKIE_SECURE=true \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp
COPY backend/PE_Dashboard_API/configuration/requirements.txt ./configuration/requirements.txt
RUN pip install --no-cache-dir -r configuration/requirements.txt \
    && groupadd --system peapp \
    && useradd --system --gid peapp --home-dir /app --no-create-home peapp \
    && mkdir /data \
    && chown peapp:peapp /data
COPY --chown=peapp:peapp backend/PE_Dashboard_API/app/ ./app/
COPY --chown=peapp:peapp backend/PE_Dashboard_API/configuration/ ./configuration/
COPY --chown=peapp:peapp --from=mfe-build /workspace/mfe/build ./app/mfe
USER peapp
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"]
CMD ["uvicorn", "main:app", "--app-dir", "app", "--host", "0.0.0.0", "--port", "8765"]
