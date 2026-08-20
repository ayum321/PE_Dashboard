FROM node:18-alpine AS mfe-build
WORKDIR /workspace/pe-dashboard-mfe
COPY pe-dashboard-mfe/package*.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY pe-dashboard-mfe/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY configuration/requirements.txt ./configuration/requirements.txt
RUN pip install --no-cache-dir -r configuration/requirements.txt
COPY app/ ./app/
COPY configuration/ ./configuration/
COPY documentation/ ./documentation/
COPY --from=mfe-build /workspace/pe-dashboard-mfe/build ./app/mfe
EXPOSE 8765
CMD ["uvicorn", "main:app", "--app-dir", "app", "--host", "0.0.0.0", "--port", "8765"]
