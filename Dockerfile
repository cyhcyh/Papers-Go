FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend DATA_DIR=/data FRONTEND_DIR=/app/frontend/dist
COPY backend/requirements.txt ./backend/requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY LICENSE ./LICENSE
COPY backend/ ./backend/
COPY --from=web /web/dist ./frontend/dist
RUN mkdir -p /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
