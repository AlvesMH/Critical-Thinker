# ---------- Stage 1: build frontend ----------
FROM node:20-bookworm-slim AS frontend_builder
WORKDIR /src

# install deps
COPY frontend/package*.json ./frontend/
RUN cd frontend && npm ci

# build
COPY frontend ./frontend
RUN cd frontend && npm run build


# ---------- Stage 2: build backend + serve frontend ----------
FROM python:3.12-slim AS app
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# backend deps
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# backend code
COPY backend ./backend

# copy built frontend into the image
COPY --from=frontend_builder /src/frontend/dist ./frontend_dist

# tell the backend where dist is
ENV FRONTEND_DIST=/app/frontend_dist

# Render sets PORT (default 10000); bind to it
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
