FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/yourname/immich-cleaner"
LABEL org.opencontainers.image.description="AI photo quality classifier for Immich"
LABEL org.opencontainers.image.license="MIT"

RUN groupadd -r cleaner && useradd -r -g cleaner cleaner

WORKDIR /app
RUN pip install --no-cache-dir requests
COPY cleaner.py .

RUN mkdir /data && chown cleaner:cleaner /data
USER cleaner

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import sqlite3, sys; sqlite3.connect('/data/cleaner.db').execute('SELECT 1'); sys.exit(0)" || exit 1

CMD ["python", "-u", "cleaner.py"]
