# ── Stage 1: Build the React frontend ──────────────────────────────────────────
FROM node:20-alpine AS frontend-build
WORKDIR /build

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build

# ── Stage 2: Production image ───────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN set -eux; \
    # Enable contrib + non-free repos so intel-media-va-driver (iHD) is reachable.
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/^Components: main$/Components: main contrib non-free non-free-firmware/' \
            /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's/ main$/ main contrib non-free non-free-firmware/' /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libvpl2 \
        intel-media-va-driver \
        libva-drm2; \
    for pkg in libmfx-gen1.2 libmfx-gen1 libmfx1; do \
        if apt-cache show "$pkg" >/dev/null 2>&1; then \
            apt-get install -y --no-install-recommends "$pkg" && break; \
        fi; \
    done; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Copy the built frontend into the image
COPY --from=frontend-build /build/dist ./static

# Bundle the app icon so branding endpoints resolve correctly
RUN mkdir -p /app/media/Logo
COPY icon.png /app/media/Logo/icon.png
COPY icon.png /app/media/Logo/logo.png

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
