# PE Dashboard React MFE image
# Build from repository root: docker build -f frontend/PE_Dashboard_MFE/Dockerfile .
FROM nginx:1.27-alpine

# Create stratosphere non-root user matching Blue Yonder standard (UID 1100)
RUN addgroup -g 1100 -S stratosphere && \
    adduser -u 1100 -S -G stratosphere -s /bin/sh stratosphere

# Copy pre-built React static assets directly (no private npm/artifactory dependency needed)
COPY --chown=1100:1100 frontend/PE_Dashboard_MFE/build/ /usr/share/nginx/html/
COPY --chown=1100:1100 frontend/PE_Dashboard_MFE/nginx.conf /etc/nginx/nginx.conf
COPY --chown=1100:1100 frontend/PE_Dashboard_MFE/start_mfe.sh /app/start_mfe.sh

# Ensure permissions for non-root execution and clean line endings
RUN mkdir -p /tmp /usr/share/nginx/html && \
    sed -i -e 's/\r$//' -e '1s/^\xef\xbb\xbf//' /app/start_mfe.sh && \
    sed -i -e 's/\r$//' -e '1s/^\xef\xbb\xbf//' /etc/nginx/nginx.conf && \
    chmod +x /app/start_mfe.sh && \
    chown -R 1100:1100 /usr/share/nginx/html /app /etc/nginx && \
    chmod 1777 /tmp

EXPOSE 8080

USER 1100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1

CMD ["/bin/sh", "/app/start_mfe.sh"]