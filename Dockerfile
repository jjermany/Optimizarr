FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libvpl2; \
    if apt-cache show intel-media-va-driver >/dev/null 2>&1; then \
        apt-get install -y --no-install-recommends intel-media-va-driver; \
    fi; \
    if apt-cache show libmfx1 >/dev/null 2>&1; then \
        apt-get install -y --no-install-recommends libmfx1; \
    elif apt-cache show libmfx-gen1 >/dev/null 2>&1; then \
        apt-get install -y --no-install-recommends libmfx-gen1; \
    fi; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
