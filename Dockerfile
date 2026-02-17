FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN set -eux; \
    # Enable contrib + non-free repos so intel-media-va-driver (iHD) is reachable.
    # python:3.11-slim (Debian Bookworm) ships only "main" by default; the iHD
    # VAAPI driver lives in "non-free" and is required for Intel Gen 8+ hardware.
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
    # VPL GPU runtime – package name changed across Debian/Ubuntu releases; try each.
    for pkg in libmfx-gen1.2 libmfx-gen1 libmfx1; do \
        if apt-cache show "$pkg" >/dev/null 2>&1; then \
            apt-get install -y --no-install-recommends "$pkg" && break; \
        fi; \
    done; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
