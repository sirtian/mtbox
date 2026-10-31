FROM python:3.11-slim

LABEL org.opencontainers.image.title="mtbox" \
      org.opencontainers.image.description="Lightweight M-Team subscription downloader for NAS" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/sirtian/mtbox"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN mkdir -p /config /downloads /media

ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=8s --start-period=30s --retries=3 \
  CMD python -c "import os, sys, urllib.request; p=os.environ.get('PORT', '8080'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + p + '/health', timeout=5).status == 200 else 1)"

CMD ["mtbox", "serve"]
