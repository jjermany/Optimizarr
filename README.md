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

The service listens on `http://localhost:8080`.

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

If both UI auth variables are unset, API endpoints are open.

## API endpoints

### Health and system

- `GET /health`
- `GET /version`
- `GET /metrics`

### Settings

- `GET /settings`
- `POST /settings`

### Notification settings

- `GET /notifications/settings`
- `PUT /notifications/settings`
- `POST /notifications/test`

### Libraries and profiles

- `GET /libraries`
- `POST /libraries`
- `PUT /libraries/{library_id}`
- `DELETE /libraries/{library_id}`
- `GET /libraries/{library_id}/profile`
- `PUT /libraries/{library_id}/profile`
- `POST /libraries/{library_id}/scan`

### Jobs and queue

- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `POST /jobs/{job_id}/cancel`
- `POST /jobs/{job_id}/retry`
- `POST /jobs/{job_id}/pause`
- `POST /jobs/{job_id}/resume`
- `POST /jobs/{job_id}/abort`
- `POST /scan` (scan all enabled libraries)
- `POST /queue/pause`
- `POST /queue/resume`

Backward-compatible aliases also exist for:

- `POST /api/jobs`
- `GET /api/jobs`
- `GET /api/jobs/{job_id}`

### Recovery and websocket

- `POST /recovery/run`
- `GET /auth/ws-token` (only when a websocket token is required)
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
