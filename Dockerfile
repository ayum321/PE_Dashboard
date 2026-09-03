# PE Dashboard API image
# Build from repository root: docker build -f backend/PE_Dashboard_API/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PE_STATE_DIR=/tmp PE_UI_MODE=api PE_COOKIE_SECURE=true HOME=/home/stratosphere XDG_CACHE_HOME=/tmp

COPY backend/PE_Dashboard_API/configuration/requirements.txt /app/configuration/requirements.txt

# Create a user and group for running the application
RUN groupadd --gid 1100 "stratosphere" && \
    useradd --create-home --no-log-init --shell "/bin/bash" --uid 1100 --gid 1100 "stratosphere" && \
    pip install --no-cache-dir -r /app/configuration/requirements.txt && \
    mkdir -p /data /app /tmp/pe_dashboard_state && \
    chown -R 1100:1100 /data /app /home/stratosphere /tmp/pe_dashboard_state && \
    chmod 1777 /tmp

COPY --chown=1100:1100 backend/PE_Dashboard_API/app/ /app/app/
COPY --chown=1100:1100 backend/PE_Dashboard_API/configuration/ /app/configuration/

USER 1100:1100
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"]
CMD ["uvicorn", "main:app", "--app-dir", "/app/app", "--host", "0.0.0.0", "--port", "8765"]
