# PE Dashboard React MFE image
# Build from repository root: docker build -f frontend/PE_Dashboard_MFE/Dockerfile .
FROM node:18-alpine AS build

WORKDIR /workspace/react-dashboard

COPY frontend/PE_Dashboard_MFE/source/package*.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

COPY frontend/PE_Dashboard_MFE/source/ ./
RUN npm run build

# Final runtime image
FROM nginx:1.27-alpine

# Create stratosphere non-root user matching Blue Yonder standard (UID 1100)
RUN addgroup -g 1100 -S stratosphere && \
    adduser -u 1100 -S -G stratosphere -s /bin/sh stratosphere

# Copy build artifacts and config
COPY --from=build --chown=1100:1100 /workspace/react-dashboard/build /usr/share/nginx/html
COPY --chown=1100:1100 frontend/PE_Dashboard_MFE/nginx.conf /etc/nginx/nginx.conf
COPY --chown=1100:1100 frontend/PE_Dashboard_MFE/start_mfe.sh /app/start_mfe.sh

# Ensure permissions for non-root execution
RUN mkdir -p /tmp /usr/share/nginx/html && \
    sed -i -e 's/\r$//' -e '1s/^\xef\xbb\xbf//' /app/start_mfe.sh && \
    chmod +x /app/start_mfe.sh && \
    chown -R 1100:1100 /usr/share/nginx/html /app /etc/nginx && \
    chmod 1777 /tmp

EXPOSE 8080

USER 1100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1

CMD ["/bin/sh", "/app/start_mfe.sh"]