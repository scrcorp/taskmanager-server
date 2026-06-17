FROM python:3.12-slim

WORKDIR /app

# System dependencies
# libpango/libpangoft2/libgdk-pixbuf: WeasyPrint runtime libs (warning PDF rendering).
# fonts-dejavu-core: glyph coverage for the rendered document.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl fonts-dejavu-core \
      libpango-1.0-0 libpangoft2-1.0-0 libgdk-pixbuf-2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY alembic.ini .
COPY alembic/ alembic/
COPY app/ app/
COPY static/ static/
COPY start.sh .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

CMD ["./start.sh"]
