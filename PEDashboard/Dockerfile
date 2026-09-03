# PE Dashboard API image
# Build from repository root: docker build -f backend/PE_Dashboard_API/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PE_STATE_DIR=/data PE_UI_MODE=api PE_COOKIE_SECURE=true HOME=/tmp XDG_CACHE_HOME=/tmp
COPY backend/PE_Dashboard_API/configuration/requirements.txt /app/configuration/requirements.txt
RUN pip install --no-cache-dir -r /app/configuration/requirements.txt \
    && groupadd --gid 10001 peapp \
    && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home peapp \
    && mkdir -p /data /app \
    && chown -R 10001:10001 /data /app
COPY --chown=10001:10001 backend/PE_Dashboard_API/app/ /app/app/
COPY --chown=10001:10001 backend/PE_Dashboard_API/configuration/ /app/configuration/

USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"]
CMD ["uvicorn", "main:app", "--app-dir", "/app/app", "--host", "0.0.0.0", "--port", "8765"]
