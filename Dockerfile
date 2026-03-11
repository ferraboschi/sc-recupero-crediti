# SC Recupero Crediti - Backend API
# Docker image for FastAPI backend (PostgreSQL via Supabase or SQLite locale)
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (include libpq for psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ ./backend/

# Copy .env.example as default (can be overridden at runtime)
COPY .env.example .env

# Create data directory (for SQLite fallback)
RUN mkdir -p data/logs

# Expose port (Render assigns $PORT dynamically)
EXPOSE 8000

# Health check (uses $PORT if set, fallback to 8000)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/api/health || exit 1

# Run application (use $PORT for Render compatibility, default 8000)
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
