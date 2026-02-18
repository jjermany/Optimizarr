# Optimizarr

Optimizarr is an automated media optimization service with a web UI. It discovers media files, queues hardware-accelerated transcoding jobs, monitors system resources, and streams real-time progress to a dashboard — all from a single Docker container.

## Features

- Library management for media folders under `/media`
- Configurable per-library encoding profiles (codec, CRF/CBR, resolution, schedule)
- Job queue with pause / resume / cancel / retry / abort controls
- Manual and automatic library scanning
- Startup and on-demand recovery workflow
- Email notification settings + test trigger
- Real-time WebSocket event stream for UI updates
- Intel QSV / VAAPI hardware encoding support
- Optional basic auth for UI / API access

## Architecture

- **Single container** — React UI is built into the image and served by FastAPI on port `8080`. No separate nginx container required.
- **Backend**: FastAPI + SQLAlchemy (`app/`)
- **Frontend**: Vite + React (`frontend/`)
- **Workers**: background threads for job queue, library discovery, notifications, and workspace cleanup

---

## Unraid deployment

### Option 1 — Add the template via terminal (recommended)

Run the following in the Unraid terminal to install the template into Community Applications:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/optimizarr.xml \
  https://raw.githubusercontent.com/jjermany/Optimizarr/main/optimizarr.xml
```

Then go to **Docker → Add Container** and select **Optimizarr** from the template drop-down. The template pre-fills all paths, ports, GPU device passthrough, and environment variables.

### Option 2 — docker compose

```bash
# Pull and start (single container, port 8080)
docker compose -f docker-compose.unraid.yml up -d
```

The web UI will be available at `http://<unraid-ip>:8080`.

---

## Quick start (local / generic Docker)

```bash
# Build and run from source
docker compose up --build
```

```bash
# Pull the published image
docker pull ghcr.io/jjermany/optimizarr:latest
docker run -d \
  --name optimizarr \
  -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -v /path/to/media:/media \
  -v /path/to/config:/config \
  -v /path/to/cache:/cache \
  -e LIBVA_DRIVER_NAME=iHD \
  -e QSV_DEVICE=/dev/dri/renderD128 \
  ghcr.io/jjermany/optimizarr:latest
```

Volumes:

| Host path | Container path | Purpose |
|---|---|---|
| `/path/to/media` | `/media` | Media libraries |
| `/path/to/config` | `/config` | Database + settings (persistent) |
| `/path/to/cache` | `/cache` | Temporary encode workspace |

---

## Local development

### Backend

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev   # Vite dev server on :5173, proxies /api and /ws to :8080
```

---

## Configuration

Key environment variables:

| Variable | Default | Description |
|---|---|---|
| `LIBVA_DRIVER_NAME` | `iHD` | VA-API driver. Use `iHD` for Intel Gen 8+ / Arc |
| `QSV_DEVICE` | `/dev/dri/renderD128` | DRM render node for QSV |
| `OPTIMIZARR_VERSION` | `0.1.0` | Reported by `GET /version` |
| `OPTIMIZARR_UI_USERNAME` | _(unset)_ | Enable basic auth — username |
| `OPTIMIZARR_UI_PASSWORD` | _(unset)_ | Enable basic auth — password |
| `OPTIMIZARR_WS_TOKEN` | _(unset)_ | Optional WebSocket token gate |

If both auth variables are unset, the API is open.

---

## API endpoints

### Health and system

- `GET /api/health`
- `GET /api/version`
- `GET /api/metrics`

### Settings

- `GET /api/settings`
- `POST /api/settings`

### Notification settings

- `GET /api/notifications/settings`
- `PUT /api/notifications/settings`
- `POST /api/notifications/test`

### Libraries and profiles

- `GET /api/libraries`
- `POST /api/libraries`
- `PUT /api/libraries/{library_id}`
- `DELETE /api/libraries/{library_id}`
- `GET /api/libraries/{library_id}/profile`
- `PUT /api/libraries/{library_id}/profile`
- `POST /api/libraries/{library_id}/scan`

### Jobs and queue

- `POST /api/jobs`
- `GET /api/jobs`
- `GET /api/jobs/{job_id}`
- `POST /api/jobs/{job_id}/cancel`
- `POST /api/jobs/{job_id}/retry`
- `POST /api/jobs/{job_id}/pause`
- `POST /api/jobs/{job_id}/resume`
- `POST /api/jobs/{job_id}/abort`
- `POST /api/scan`
- `POST /api/queue/pause`
- `POST /api/queue/resume`

### Recovery and WebSocket

- `POST /api/recovery/run`
- `GET /api/auth/ws-token`
- `WS /ws`

---

## Testing

```bash
# Backend
pytest

# Frontend
cd frontend && npm test
```

---

## Intel QSV troubleshooting

If jobs fail with `qsv_encode_failed` but `ffmpeg -encoders` lists `*_qsv` encoders, the issue is usually device initialisation rather than missing encoder support.

1. Ensure `/dev/dri` is passed through (`--device /dev/dri:/dev/dri`).
2. Set `LIBVA_DRIVER_NAME=iHD` for Intel Gen 8+ / Arc GPUs.
3. Confirm the render node exists: `ls /dev/dri/` — typically `renderD128`.
4. If your host uses a different node, set `QSV_DEVICE` accordingly.

---

## GPU dashboard percentage

The GPU% stat on the dashboard is collected using the following priority order:

1. **sysfs engine stats** (`/sys/class/drm/card*/engine/*/busy_time_ms`) — the preferred method. Works inside Docker without any extra capabilities. Requires Linux kernel 5.11 or newer (all current Unraid versions qualify).
2. **`intel_gpu_top`** — fallback for older kernels. Inside Docker this tool needs access to the kernel perf interface, which is blocked by Docker's default seccomp profile. To enable it add to your container:
   ```
   --cap-add=SYS_ADMIN --security-opt seccomp=unconfined
   ```
   or set `Privileged: true` in your Unraid template.
3. **`nvidia-smi`** — used automatically for NVIDIA GPUs if present.

If GPU% shows 0% while encoding, the most common cause on Unraid is that the sysfs paths aren't exposed inside the container. Verify with:

```bash
docker exec optimizarr ls /sys/class/drm/card0/engine/
```

If the directory is empty or missing, add `--privileged` (or the cap-add/seccomp options above) to your container's extra parameters.
