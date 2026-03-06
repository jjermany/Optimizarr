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
- Built-in admin login UI with optional dual-factor authentication (TOTP)

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
| `OPTIMIZARR_SESSION_COOKIE_SECURE` | `auto` | Session cookie `Secure` policy (`auto`, `true`, `false`) |
| `OPTIMIZARR_BOOTSTRAP_TOKEN` | _(auto-generated)_ | Required one-time token for first admin setup; if unset, Optimizarr prints a generated token to the logs |
| `OPTIMIZARR_SECRETS_KEY` | _(unset)_ | Optional base64url 32-byte key for secrets encryption at rest |
| `OPTIMIZARR_SECRETS_KEY_PATH` | `/config/optimizarr.secrets.key` | Path to persisted encryption key file when env key is unset |
| `OPTIMIZARR_WORKSPACE_ROOT_BASE` | `/cache` | Allowed parent directory for `workspace_root` |
| `OPTIMIZARR_LOG_MAX_BYTES` | `5242880` | Max size per log file before rotation (5 MiB default) |
| `OPTIMIZARR_LOG_BACKUP_COUNT` | `10` | Number of rotated log files to keep (`.1`, `.2`, etc.) |

Authentication:
- On first startup (or first startup after upgrading from legacy basic-auth builds), Optimizarr prompts you to create an admin account.
- Initial admin setup requires the bootstrap token from `OPTIMIZARR_BOOTSTRAP_TOKEN` or the one-time token printed in the backend logs.
- During setup you can enable dual-factor authentication (TOTP) or skip it.
- After setup, API/UI access requires a login session cookie.
- Before admin setup is completed, non-setup API routes are blocked.
- State-changing authenticated API calls require a CSRF token header (`X-CSRF-Token`) that matches the `optimizarr_csrf` cookie.
- Login attempts are rate limited to reduce brute-force risk.

Secrets at rest:
- Sensitive integration fields (SMTP password, Plex token, Prowlarr API key, qBittorrent password, SABnzbd API key) are stored encrypted in the database.
- If `OPTIMIZARR_SECRETS_KEY` is unset, Optimizarr auto-generates and persists a key at `OPTIMIZARR_SECRETS_KEY_PATH`.

Filesystem boundaries:
- Library paths must stay under `MEDIA_ROOT`.
- The directory browser only lists folders under `MEDIA_ROOT`.
- `workspace_root` must stay under `OPTIMIZARR_WORKSPACE_ROOT_BASE`.

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
- `WS /ws`

### Authentication

- `GET /api/auth/status`
- `POST /api/auth/totp/secret`
- `POST /api/auth/bootstrap`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/account`
- `POST /api/auth/account`
- `POST /api/auth/account/2fa/enable`
- `POST /api/auth/account/2fa/disable`

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

1. **sysfs engine stats** (`/sys/class/drm/card*/engine/*/busy_time_ms`) — works inside Docker without any extra capabilities on kernels that expose per-engine busy-time counters.
2. **`intel_gpu_top`** — used when sysfs counters are unavailable (the common case on Intel iGPU). This tool requires `CAP_PERFMON` to call `perf_event_open()`. Docker's default seccomp profile allows that syscall when the capability is present, so the container must be started with `--cap-add=PERFMON`. The `docker-compose.yml`, `docker-compose.unraid.yml`, and `optimizarr.xml` templates already include this.

   If you manage the container manually, add the flag:
   ```
   --cap-add=PERFMON
   ```
   Unraid users: this appears in the **Extra Parameters** field in the Docker template GUI. After adding it, **stop and recreate** the container (not just restart).

3. **`nvidia-smi`** — used automatically for NVIDIA GPUs when present.

If GPU% still shows 0% after recreating the container with `--cap-add=PERFMON`, run:

```bash
docker exec optimizarr intel_gpu_top -J -s 250 -c 1
```

If that prints JSON with an `engines` key the metric collection is working. If it prints nothing or an error, check that `/dev/dri` is passed through correctly.
