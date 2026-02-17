# Optimizarr

Optimizarr is a FastAPI-based media optimization service designed to discover media files, queue transcoding jobs, monitor system resources, and emit real-time events for a frontend dashboard.

## Features

- Library management for media folders under `/media`
- Configurable per-library optimization profiles
- Job queue with pause/resume/cancel/retry/abort controls
- Manual and automatic library scanning
- Startup and on-demand recovery workflow
- Email notification settings + test trigger endpoint
- Real-time websocket event stream for UI updates
- Basic auth protection for UI/API access (optional)

## Architecture at a glance

- **Backend**: FastAPI + SQLAlchemy (`app/`)
- **Worker**: background queue worker thread for optimization jobs
- **Discovery**: discovery worker thread for interval/watcher scans
- **Notifications**: background notification worker thread
- **Frontend**: Vite + React app (`frontend/`)

## Requirements

- Docker (recommended) for containerized deployment
- Or local Python 3.11+ for development
- FFmpeg (for real transcoding workloads)

## Quick start (Docker)

```bash
docker compose up --build
```

## Using prebuilt GHCR images

If you deploy from published container images instead of building locally, pull the API image from GitHub Container Registry:

```bash
docker pull ghcr.io/jjermany/optimizarr:latest
```

`docker-compose.unraid.yml` is configured to use `ghcr.io/jjermany/optimizarr:latest` for the backend API service.

The frontend listens on `http://localhost:8085` and proxies API traffic to the backend container.

Mounted volumes in `docker-compose.yml`:

- `./media:/media`
- `./cache:/cache`
- `./config:/config`

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
npm run dev
```

## Configuration

Key environment variables:

- `OPTIMIZARR_VERSION`: reported by `GET /version`
- `OPTIMIZARR_UI_USERNAME`: enable basic auth username
- `OPTIMIZARR_UI_PASSWORD`: enable basic auth password
- `OPTIMIZARR_WS_TOKEN`: optional websocket token gate

- `LIBVA_DRIVER_NAME`: set to `iHD` for modern Intel GPUs (including Core Ultra / Arc iGPU)
- `QSV_DEVICE`: DRM render node used by QSV (default `/dev/dri/renderD128`)

If both UI auth variables are unset, API endpoints are open.

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
- `POST /api/scan` (scan all enabled libraries)
- `POST /api/queue/pause`
- `POST /api/queue/resume`

Backward-compatible aliases also exist for:

- `POST /api/jobs`
- `GET /api/jobs`
- `GET /api/jobs/{job_id}`

### Recovery and websocket

- `POST /api/recovery/run`
- `GET /api/auth/ws-token` (only when a websocket token is required)
- `WS /ws`

## Testing

Run backend tests:

```bash
pytest
```

Run frontend tests:

```bash
cd frontend
npm test
```

## Notes

- Library paths must be absolute and remain under `/media`.
- The Docker image includes FFmpeg and installs Intel VA-API/media runtime packages when available in the base distro repositories.


## Intel QSV troubleshooting

If jobs fail with `qsv_encode_failed` but `ffmpeg -encoders` shows `*_qsv` encoders, the issue is usually runtime device initialization rather than missing encoder support.

1. Ensure `/dev/dri` is passed to the API container (`devices: - /dev/dri:/dev/dri`).
2. Set `LIBVA_DRIVER_NAME=iHD` for newer Intel GPUs.
3. Ensure the render node exists and is accessible (typically `/dev/dri/renderD128`).
4. If your host uses a different render node, set `QSV_DEVICE`/profile `qsv_device` accordingly.

Optimizarr now builds QSV pipelines with explicit QSV device initialization and hardware upload filters so encode can run on Intel Quick Sync even when hardware decode acceleration is unavailable for the input codec.
