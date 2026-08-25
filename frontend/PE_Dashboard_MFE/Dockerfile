# PE Dashboard React MFE image
# Build from repository root: docker build -f frontend/PE_Dashboard_MFE/Dockerfile .
FROM node:18-alpine AS build
WORKDIR /workspace/react-dashboard
COPY frontend/PE_Dashboard_MFE/source/package*.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/PE_Dashboard_MFE/source/ ./
RUN npm run build
FROM nginx:1.27-alpine
COPY --from=build /workspace/react-dashboard/build /usr/share/nginx/html
COPY frontend/PE_Dashboard_MFE/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
