# Optimizarr

Optimizarr is an automated media optimization service with a web UI. It discovers media files, queues hardware-accelerated transcoding jobs, monitors system resources, and streams real-time progress to a dashboard — all from a single Docker container.

## Features

- Library management for media folders under `MEDIA_ROOT` (recommended: `/data`)
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
  --add-host host.docker.internal:host-gateway \
  -v /path/to/data:/data \
  -v /path/to/config:/config \
  -v /path/to/cache:/cache \
  -e MEDIA_ROOT=/data \
  -e LIBVA_DRIVER_NAME=iHD \
  -e QSV_DEVICE=/dev/dri/renderD128 \
  -e OPTIMIZARR_QMMD_METRICS_URL=http://host.docker.internal:9000/metrics \
  ghcr.io/jjermany/optimizarr:latest
```

Volumes:

| Host path | Container path | Purpose |
|---|---|---|
| `/path/to/data` | `/data` | Root data path containing completed downloads and final media libraries |
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
| `MEDIA_ROOT` | `/data` | Root folder shown by the library picker; set this to the shared data mount that contains both completed downloads and final libraries |
| `OPTIMIZARR_VERSION` | `0.1.0` | Reported by `GET /version` |
| `OPTIMIZARR_SESSION_COOKIE_SECURE` | `auto` | Session cookie `Secure` policy (`auto`, `true`, `false`) |
| `OPTIMIZARR_BOOTSTRAP_TOKEN` | _(auto-generated)_ | Required one-time token for first admin setup; if unset, Optimizarr prints a generated token to the logs |
| `OPTIMIZARR_SECRETS_KEY` | _(unset)_ | Optional base64url 32-byte key for secrets encryption at rest |
| `OPTIMIZARR_SECRETS_KEY_PATH` | `/config/optimizarr.secrets.key` | Path to persisted encryption key file when env key is unset |
| `OPTIMIZARR_WORKSPACE_ROOT_BASE` | `/cache` | Allowed parent directory for `workspace_root` |
| `OPTIMIZARR_LOG_MAX_BYTES` | `5242880` | Max size per log file before rotation (5 MiB default) |
| `OPTIMIZARR_LOG_BACKUP_COUNT` | `10` | Number of rotated log files to keep (`.1`, `.2`, etc.) |
| `OPTIMIZARR_QMMD_METRICS_URL` | _(auto-detect)_ | Optional qmassa/qmmd Prometheus metrics URL, for example `http://host.docker.internal:9000/metrics`; when set, the dashboard prefers qmmd Intel GPU readings before `intel_gpu_top` |
| `OPTIMIZARR_QMMD_AUTO_DISCOVERY` | `true` | Try common Docker host addresses on qmmd's default port `9000` when `OPTIMIZARR_QMMD_METRICS_URL` is unset |
| `OPTIMIZARR_QBT_STRIKE_CHECK_INTERVAL_SECONDS` | `60` | How often Optimizarr checks owned qBittorrent torrents for metadata/stalled/slow strikes |
| `OPTIMIZARR_QBT_METADATA_MAX_STRIKES` | `3` | Strikes before removing an owned qBittorrent torrent stuck downloading metadata; applies to public and private torrents |
| `OPTIMIZARR_QBT_STALLED_MAX_STRIKES` | `3` | Strikes before removing an owned qBittorrent torrent in a stalled/error state |
| `OPTIMIZARR_QBT_SLOW_MIN_SPEED_BPS` | `0` | Minimum speed for slow-download strike checks; `0` disables slow-download strikes |
| `OPTIMIZARR_QBT_SLOW_MAX_STRIKES` | `3` | Strikes before removing an owned qBittorrent torrent below `OPTIMIZARR_QBT_SLOW_MIN_SPEED_BPS` |
| `OPTIMIZARR_QBT_SLOW_IGNORE_PRIVATE` | `true` | Skip slow-download strikes for qBittorrent torrents reported as private |

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

Recommended layout:
- Mount your full data tree into the container at `/data`.
- Set `MEDIA_ROOT=/data`.
- Keep completed downloads under `/data/complete/...`.
- Keep final libraries somewhere under `/data/...`.

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

The GPU stat on the dashboard is collected using the following priority order:

1. **qmassa/qmmd** — preferred when `OPTIMIZARR_QMMD_METRICS_URL` points at the qmassa project's `qmmd` Prometheus exporter, or when auto-discovery finds qmmd on the Docker host at port `9000`. This is the best option for newer Intel Arc / xe-driver systems because qmmd exposes physical engine utilization ratios, which generally match video-load reporting better than the older probes.
2. **`intel_gpu_top`** — built-in fallback for Intel GPUs because it reports per-engine busy percentages and Intel's own power rail readings when available. Utilization is clamped to 0-100% before it reaches the dashboard. This tool requires `CAP_PERFMON` to call `perf_event_open()`. Docker's default seccomp profile allows that syscall when the capability is present, so the container must be started with `--cap-add=PERFMON`. The `docker-compose.yml`, `docker-compose.unraid.yml`, and `optimizarr.xml` templates already include this.

   If you manage the container manually, add the flag:
   ```
   --cap-add=PERFMON
   ```
   Unraid users: this appears in the **Extra Parameters** field in the Docker template GUI. After adding it, **stop and recreate** the container (not just restart).

3. **sysfs engine stats** (`/sys/class/drm/card*/engine/*/busy_time_ms`) — fallback that works inside Docker without any extra capabilities on kernels that expose per-engine busy-time counters.
4. **GT frequency ratio** — last-resort Intel fallback for QSV workloads where engine counters stay at 0%; this reports clock pressure, not true utilization.
5. **`nvidia-smi`** — used automatically for NVIDIA GPUs when present.

If GPU% still shows 0% after recreating the container with `--cap-add=PERFMON`, run:

```bash
docker exec optimizarr intel_gpu_top -J -s 250 -c 1
```

If that prints JSON with an `engines` key the fallback metric collection is working. If `intel_gpu_top` prints nothing or an error, check that `/dev/dri` is passed through correctly.
