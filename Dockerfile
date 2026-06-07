FROM python:3.12-slim

WORKDIR /app

# Build deps needed for lxml and other compiled extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Install package and dependencies (including Google Drive extras)
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir ".[drive]"

# Persistent volume will be mounted at /data
RUN mkdir -p /data

# Point DB and PDFs at the persistent volume
ENV ZOTERPILE_DB_PATH=/data/refs.db
ENV ZOTERPILE_PDF_STORAGE_PATH=/data/pdfs

EXPOSE 8080

# --public binds to 0.0.0.0 so Fly's proxy can reach the server
CMD ["zoterpile", "web", "--public", "--port", "8080"]
