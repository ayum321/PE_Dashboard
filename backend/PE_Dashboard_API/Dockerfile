# PE Dashboard API image
# Build from repository root: docker build -f backend/PE_Dashboard_API/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PE_STATE_DIR=/tmp/pe_dashboard_state \
    PE_UI_MODE=api \
    PE_COOKIE_SECURE=true \
    HOME=/home/stratosphere \
    XDG_CACHE_HOME=/tmp

# Create a user and group for running the application
RUN groupadd --gid 1100 "stratosphere" && \
    useradd --create-home --no-log-init --shell "/bin/bash" --uid 1100 --gid 1100 "stratosphere"

# Install dependencies
COPY backend/PE_Dashboard_API/configuration/requirements.txt /app/configuration/requirements.txt
RUN pip install --no-cache-dir -r /app/configuration/requirements.txt

# Ensure writable state and temp directories
RUN mkdir -p /tmp/pe_dashboard_state /data /app && \
    chown -R 1100:1100 /tmp/pe_dashboard_state /data /app /home/stratosphere && \
    chmod 1777 /tmp

# Copy app source code and configuration
COPY --chown=1100:1100 backend/PE_Dashboard_API/app/ /app/app/
COPY --chown=1100:1100 backend/PE_Dashboard_API/configuration/ /app/configuration/
COPY --chown=1100:1100 backend/PE_Dashboard_API/start_main.sh /app/start_main.sh

# Make startup script executable
RUN chmod +x /app/start_main.sh

# Expose the API port
EXPOSE 8765

USER 1100

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"]

CMD ["/app/start_main.sh"]