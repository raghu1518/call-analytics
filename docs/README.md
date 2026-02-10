# Call Analytics Platform

Enterprise-ready call analytics platform built with Django.

It supports:
- Batch call processing (upload audio, transcript, analytics, summaries)
- Realtime event ingestion from cloud providers
- Live supervisor alerts (sentiment, escalation keywords, dead-air, risk)
- Genesys Cloud connector (OAuth, subscriptions, websocket stream, retries, health)
- Genesys AudioHook listener (listen-only dual-party call audio ingest)
- Automated deployment scripts for Linux and Windows

Companion Word guide:
- `./Call_Analytics_Implementation_Guide.docx`

## Table of Contents
- [1. Product Overview](#1-product-overview)
- [2. Core Capabilities](#2-core-capabilities)
- [3. Architecture](#3-architecture)
- [4. Repository Layout](#4-repository-layout)
- [5. Prerequisites](#5-prerequisites)
- [6. Setup](#6-setup)
- [7. Start and Run](#7-start-and-run)
- [8. User Workflow](#8-user-workflow)
- [9. API Reference](#9-api-reference)
- [10. Realtime Cloud Integration](#10-realtime-cloud-integration)
- [11. Genesys Cloud Integration](#11-genesys-cloud-integration)
- [12. Configuration Reference](#12-configuration-reference)
- [13. Logging and Runtime Files](#13-logging-and-runtime-files)
- [14. Troubleshooting](#14-troubleshooting)
- [15. Security and Production Checklist](#15-security-and-production-checklist)
- [16. Command Cheat Sheet](#16-command-cheat-sheet)
- [17. Deployment Automation](#17-deployment-automation)
- [18. Nginx Single Domain Multi-Path Setup](#18-nginx-single-domain-multi-path-setup)

## 1. Product Overview
This application processes contact-center conversations into actionable insights.

Primary outcomes:
- Transcript with timestamps/speaker segmentation
- AI summary, topics, action items, Q&A, sentiment
- Realtime event timeline and supervisor alerts
- Detailed call workspace UI (audio player, events, transcript, AI insights)

How it works end-to-end:
1. Audio and events come in from upload workflows or realtime providers.
2. The pipeline produces transcripts and analysis artifacts and stores DB records.
3. Realtime worker endpoints update call state, events, and alerts continuously.
4. Dashboard and call detail UI render both historical and live insights.
5. Genesys connector and AudioHook workers bridge Genesys data/audio into the same local realtime APIs.

## 2. Core Capabilities
### Batch analytics
- Upload audio from UI
- Transcribe (Sarvam STT)
- Build analysis bundle and summaries
- Export JSON and CSV from call details

### Realtime analytics and alerting
- Ingest realtime events from any cloud source via `POST /api/realtime/events`
- Stream updates to UI via Server-Sent Events (SSE)
- Generate supervisor alerts based on configurable risk rules
- Store rolling live audio windows for in-progress calls

### Genesys Cloud connector
- OAuth client-credentials authentication
- Topic subscription builder (manual and preset queue/user discovery)
- Notifications websocket consumption
- Event mapping to local realtime ingest API
- Retries, reconnect, status heartbeat, health endpoint

### Deployment and operations
- One-command deploy/uninstall scripts for Linux and Windows
- Linux `systemd` service orchestration
- Windows Scheduled Task startup orchestration
- Daily rotating application/error logs

## 3. Architecture
```text
Batch flow
UI Upload -> Django view -> Pipeline -> Transcript/Analysis files -> DB -> Dashboard/Detail UI

Realtime flow
Cloud Event Source (Genesys/Twilio/etc)
  -> POST /api/realtime/events
  -> Realtime DB tables (realtime_call, realtime_event, supervisor_alert)
  -> SSE /api/realtime/stream
  -> Live UI updates (alerts/risk)

Genesys flow
run_genesys_connector worker
  -> OAuth token
  -> Notification channel + subscriptions
  -> WebSocket consume
  -> map payload
  -> POST /api/realtime/events

Genesys live-audio flow (listen-only)
run_genesys_audiohook_listener worker
  <- Genesys AudioHook WebSocket (agent + customer media)
  -> decode media packets (PCMU/PCMA/L16)
  -> POST /api/realtime/audio/chunk
  -> rolling WAV + live player + realtime alert pipeline
```

## 4. Repository Layout
| Path | Purpose |
|---|---|
| `app/` | Django app (views, models, services, templates, static assets) |
| `app/services/pipeline.py` | Batch audio processing pipeline |
| `app/services/genesys_connector.py` | Genesys connector worker/service |
| `app/management/commands/` | CLI commands (`run_genesys_connector`, `run_genesys_audiohook_listener`, `build_genesys_topics`) |
| `core/` | Django project settings/urls/wsgi/asgi |
| `deploy/` | Automated installation/uninstallation scripts (Linux + Windows) |
| `data/uploads/` | Uploaded source audio files |
| `data/outputs/` | Generated transcripts/analysis artifacts |
| `data/runtime/` | Runtime status files (e.g. Genesys connector heartbeat) |
| `data/db/` | SQLite DB file |
| `log/` | Rotating app/error logs |
| `scripts/run.ps1` | Convenience run script |

## 5. Prerequisites
- Python 3.10+
- Windows PowerShell (for commands shown here)
- Linux bash + `systemd` (if using Linux service automation)
- Windows Task Scheduler (if using Windows startup automation)
- Internet access for Sarvam and/or Genesys APIs
- `ffmpeg` installed if processing long/varied audio formats with `pydub`

Optional (provider-specific):
- Sarvam API key for batch processing
- Genesys Cloud OAuth client for realtime connector

## 6. Setup
### 6.0 Quick automated setup (recommended)
Linux:
```bash
chmod +x deploy/linux/install.sh deploy/linux/uninstall.sh
sudo ./deploy/linux/install.sh
```

Windows:
```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows\install.ps1
```

Detailed deploy automation reference:
- `https://github.com/raghu1518/call-analytics/blob/main/deploy/README.md`

### 6.1 Create virtual environment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 6.2 Configure environment
```powershell
Copy-Item .env.example .env
```
Then update `.env` values (see [Configuration Reference](#12-configuration-reference)).

### 6.3 Run migrations
For this repo (including realtime tables):
```powershell
python manage.py migrate
```

If migrating from an older SQLModel/FastAPI DB snapshot, you may need:
```powershell
python manage.py migrate --fake-initial
```

### 6.4 Validate setup
```powershell
python manage.py check
```

## 7. Start and Run
### 7.1 Start web app
Default Django port:
```powershell
python manage.py runserver
```

Recommended port for this project (matches realtime docs/examples):
```powershell
python manage.py runserver 8009
```

Or:
```powershell
.\scripts\run.ps1
```

### 7.2 Start Genesys connector worker (optional)
In a second terminal:
```powershell
python manage.py run_genesys_connector
```

Dry run mode (connect + map, no forwarding):
```powershell
python manage.py run_genesys_connector --dry-run --log-level DEBUG
```

### 7.3 Start Genesys AudioHook listener (optional, live audio)
In another terminal:
```powershell
python manage.py run_genesys_audiohook_listener
```

Dry run mode:
```powershell
python manage.py run_genesys_audiohook_listener --dry-run --log-level DEBUG
```

### 7.4 Service/task managed runtime
If you installed with deploy automation:
- Linux service names:
  - `call-analytics-web.service`
  - `call-analytics-genesys-connector.service`
  - `call-analytics-genesys-audiohook.service`
- Windows task names:
  - `CallAnalytics-Web`
  - `CallAnalytics-GenesysConnector`
  - `CallAnalytics-GenesysAudioHook`

## 8. User Workflow
1. Open dashboard (`/`).
2. Go to upload page (`/upload`).
3. Upload call audio and submit.
4. Monitor status in dashboard (queued/processing/completed/failed).
5. Open call detail page for:
   - audio player and waveform
   - transcript timeline
   - AI insights (topics/actions/Q&A/events)
   - live supervisor alert panel (for realtime-enabled calls)
6. Export insights/transcript from call detail when needed.

## 9. API Reference
### Batch/UI APIs
| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | Dashboard |
| `GET/POST` | `/upload` | Upload page and upload action |
| `POST` | `/glossary/upload` | Upload glossary CSV |
| `POST` | `/calls/bulk` | Bulk actions |
| `GET` | `/calls/<call_id>` | Call detail page |
| `GET` | `/calls/<call_id>/audio` | Stream call audio |
| `GET` | `/calls/<call_id>/export` | Export insights/transcript |
| `GET` | `/api/metrics` | Dashboard chart metrics |
| `GET` | `/api/calls/<call_id>` | Batch call status |

### Realtime APIs
| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/realtime/events` | Ingest realtime cloud event payload |
| `POST` | `/api/realtime/audio/chunk` | Ingest base64 audio chunk + optional transcript segment(s) |
| `GET` | `/api/realtime/stream?call_id=<id>` | SSE stream for live UI updates |
| `GET` | `/api/realtime/calls/<call_id>/snapshot` | Current realtime state + events + alerts |
| `GET` | `/api/realtime/calls/<call_id>/audio` | Rolling live WAV audio (`?fallback=1` for uploaded file fallback) |
| `GET` | `/api/realtime/calls/<call_id>/audio/meta` | Live audio buffer metadata and source preference |
| `GET` | `/api/realtime/alerts` | List alerts (`call_id`, `open_only`, `limit`) |
| `POST` | `/api/realtime/alerts/<alert_id>/ack` | Acknowledge one alert |

### Integration Health API
| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/integrations/genesys/health` | Genesys worker health using heartbeat status file |
| `GET` | `/api/integrations/genesys/audiohook/health` | AudioHook listener health using heartbeat status file |

### 9.1 Request and Response Examples
#### `POST /api/realtime/events`
Request:
```json
{
  "provider": "twilio",
  "call_id": "RT-1001",
  "event_type": "transcript",
  "speaker": "customer",
  "text": "i need a supervisor now",
  "sentiment": -0.82,
  "confidence": 0.91,
  "status": "active",
  "timestamp": "2026-02-10T10:15:20Z",
  "metadata": {
    "metrics": {
      "dead_air_seconds": 4
    }
  }
}
```

Response (200):
```json
{
  "ok": true,
  "call_id": "RT-1001",
  "risk_score": 0.84,
  "sentiment_score": -0.62,
  "alerts": [
    {
      "id": 31,
      "call_id": "RT-1001",
      "type": "negative_sentiment",
      "severity": "high",
      "message": "Negative sentiment threshold breached",
      "acknowledged": false,
      "acknowledged_at": null,
      "created_at": "2026-02-10T10:15:20",
      "metadata": {}
    }
  ],
  "snapshot": {
    "call_id": "RT-1001",
    "provider": "twilio",
    "status": "active",
    "risk_score": 0.84,
    "sentiment_score": -0.62,
    "updated_at": "2026-02-10T10:15:20",
    "events": [],
    "alerts": [],
    "live_audio": {
      "call_id": "RT-1001",
      "available": false,
      "duration_seconds": 0.0,
      "sample_rate": null,
      "channels": null,
      "sample_width": null,
      "chunk_count": 0,
      "updated_at": null,
      "last_chunk_id": "",
      "window_seconds": 300
    }
  }
}
```

Response (401):
```json
{
  "detail": "Unauthorized ingest token"
}
```

#### `POST /api/realtime/audio/chunk`
Request:
```json
{
  "provider": "genesys_audiohook",
  "call_id": "RT-1001",
  "audio_encoding": "pcm_s16le",
  "sample_rate": 16000,
  "channels": 1,
  "audio_b64": "AAABAA==",
  "speaker": "customer",
  "transcript": "i need help with my policy",
  "sentiment": -0.31,
  "confidence": 0.89,
  "timestamp": "2026-02-10T10:16:05Z"
}
```

Response (200):
```json
{
  "ok": true,
  "call_id": "RT-1001",
  "audio": {
    "call_id": "RT-1001",
    "available": true,
    "duration_seconds": 0.128,
    "sample_rate": 16000,
    "channels": 1,
    "sample_width": 2,
    "chunk_count": 1,
    "updated_at": "2026-02-10T10:16:05Z",
    "last_chunk_id": "1739182565000_1",
    "window_seconds": 300
  },
  "ingested_events": 1,
  "alerts": [],
  "snapshot": {
    "call_id": "RT-1001",
    "provider": "genesys_audiohook",
    "status": "active",
    "risk_score": 0.41,
    "sentiment_score": -0.31,
    "updated_at": "2026-02-10T10:16:05",
    "events": [],
    "alerts": [],
    "live_audio": {
      "call_id": "RT-1001",
      "available": true,
      "duration_seconds": 0.128,
      "sample_rate": 16000,
      "channels": 1,
      "sample_width": 2,
      "chunk_count": 1,
      "updated_at": "2026-02-10T10:16:05Z",
      "last_chunk_id": "1739182565000_1",
      "window_seconds": 300
    }
  },
  "warnings": []
}
```

Response (400):
```json
{
  "detail": "Missing call_id"
}
```

#### `GET /api/realtime/calls/<call_id>/snapshot`
Request:
```http
GET /api/realtime/calls/RT-1001/snapshot
```

Response (200):
```json
{
  "call_id": "RT-1001",
  "provider": "twilio",
  "status": "active",
  "risk_score": 0.56,
  "sentiment_score": -0.22,
  "updated_at": "2026-02-10T10:20:01",
  "events": [
    {
      "id": 5001,
      "type": "transcript",
      "speaker": "customer",
      "text": "i need help",
      "sentiment": -0.22,
      "confidence": 0.9,
      "occurred_at": "2026-02-10T10:20:00",
      "metadata": {}
    }
  ],
  "alerts": [],
  "live_audio": {
    "call_id": "RT-1001",
    "available": true,
    "duration_seconds": 12.5,
    "sample_rate": 16000,
    "channels": 1,
    "sample_width": 2,
    "chunk_count": 15,
    "updated_at": "2026-02-10T10:20:01Z",
    "last_chunk_id": "1739182801000_15",
    "window_seconds": 300
  }
}
```

#### `GET /api/realtime/alerts?call_id=<id>&open_only=true&limit=20`
Request:
```http
GET /api/realtime/alerts?call_id=RT-1001&open_only=true&limit=20
```

Response (200):
```json
{
  "alerts": [
    {
      "id": 31,
      "call_id": "RT-1001",
      "type": "negative_sentiment",
      "severity": "high",
      "message": "Negative sentiment threshold breached",
      "acknowledged": false,
      "acknowledged_at": null,
      "created_at": "2026-02-10T10:15:20",
      "metadata": {}
    }
  ]
}
```

#### `POST /api/realtime/alerts/<alert_id>/ack`
Request:
```http
POST /api/realtime/alerts/31/ack
```

Response (200):
```json
{
  "ok": true,
  "alert": {
    "id": 31,
    "call_id": "RT-1001",
    "type": "negative_sentiment",
    "severity": "high",
    "message": "Negative sentiment threshold breached",
    "acknowledged": true,
    "acknowledged_at": "2026-02-10T10:22:01",
    "created_at": "2026-02-10T10:15:20",
    "metadata": {}
  }
}
```

Response (404):
```json
{
  "detail": "Alert not found"
}
```

#### `GET /api/integrations/genesys/health`
Request:
```http
GET /api/integrations/genesys/health
```

Response (200):
```json
{
  "healthy": true,
  "state": "running",
  "age_seconds": 2.1,
  "stale_after_seconds": 90,
  "status_path": "data/runtime/genesys_connector_status.json",
  "status": {
    "state": "running",
    "updated_at": "2026-02-10T10:25:00+00:00",
    "topics_count": 12,
    "forwarded_events": 220
  }
}
```

#### `GET /api/integrations/genesys/audiohook/health`
Request:
```http
GET /api/integrations/genesys/audiohook/health
```

Response (200):
```json
{
  "healthy": true,
  "state": "running",
  "age_seconds": 1.5,
  "stale_after_seconds": 90,
  "status_path": "data/runtime/genesys_audiohook_status.json",
  "status": {
    "state": "running",
    "updated_at": "2026-02-10T10:25:01+00:00",
    "active_connections": 1,
    "forwarded_chunks": 95
  }
}
```

### 9.2 Error Response Matrix (400/401/404/500)
| Endpoint | 400 | 401 | 404 | 500 |
|---|---|---|---|---|
| `GET /` | - | - | - | - |
| `GET/POST /upload` | POST missing file | - | - | - |
| `POST /glossary/upload` | missing file | - | - | - |
| `POST /calls/bulk` | no `call_ids`, unsupported `action` | - | - | - |
| `GET /calls/<call_id>` | - | - | call not found | - |
| `GET /calls/<call_id>/audio` | - | - | call/audio file missing | - |
| `GET /calls/<call_id>/export` | unsupported `format` | - | call not found | - |
| `GET /api/metrics` | - | - | - | - |
| `GET /api/calls/<call_id>` | - | - | call not found | - |
| `POST /api/realtime/events` | invalid JSON, validation/ingest failure | ingest token invalid | - | - |
| `POST /api/realtime/audio/chunk` | invalid JSON, missing `call_id`, decode/size/format error | ingest token invalid | - | - |
| `GET /api/realtime/stream` | - | - | - | - |
| `GET /api/realtime/calls/<call_id>/snapshot` | - | - | - (returns idle snapshot) | - |
| `GET /api/realtime/calls/<call_id>/audio` | - | - | no live audio and no fallback file | - |
| `GET /api/realtime/calls/<call_id>/audio/meta` | - | - | - | - |
| `GET /api/realtime/alerts` | - | - | - | - |
| `POST /api/realtime/alerts/<alert_id>/ack` | - | - | alert not found | - |
| `GET /api/integrations/genesys/health` | - | - | - | status file unreadable JSON/IO |
| `GET /api/integrations/genesys/audiohook/health` | - | - | - | status file unreadable JSON/IO |

Notes:
- `-` means there is no explicit handler in current code for that status class on that endpoint.
- Any endpoint can still return `500` for unexpected unhandled runtime failures.

## 10. Realtime Cloud Integration
### 10.1 Expected payload shape
`POST /api/realtime/events`
```json
{
  "provider": "twilio",
  "call_id": "RT-1001",
  "event_type": "transcript",
  "speaker": "customer",
  "text": "i need a supervisor now",
  "sentiment": -0.82,
  "confidence": 0.91,
  "status": "active",
  "timestamp": "2026-02-10T10:15:20Z",
  "agent_id": "agent-12",
  "customer_id": "cust-44",
  "metadata": {
    "metrics": {
      "dead_air_seconds": 4
    }
  }
}
```

### 10.2 Example ingest call
```powershell
curl -X POST "http://127.0.0.1:8009/api/realtime/events" ^
  -H "Content-Type: application/json" ^
  -H "X-Cloud-Token: your-token-if-configured" ^
  -d "{\"provider\":\"twilio\",\"call_id\":\"RT-1001\",\"event_type\":\"transcript\",\"speaker\":\"customer\",\"text\":\"i need a supervisor now\",\"sentiment\":-0.82,\"timestamp\":\"2026-02-10T10:15:20Z\",\"metadata\":{\"metrics\":{\"dead_air_seconds\":4}}}"
```

### 10.3 Alert rules implemented
- Negative sentiment threshold breach
- Escalation keyword hits (configurable list)
- Dead-air threshold breach
- High aggregate risk score threshold

### 10.4 Live audio chunk ingest
`POST /api/realtime/audio/chunk`
```json
{
  "provider": "genesys_cloud",
  "call_id": "RT-1001",
  "audio_encoding": "pcm_s16le",
  "sample_rate": 16000,
  "channels": 1,
  "audio_b64": "<base64-pcm-bytes>",
  "speaker": "customer",
  "transcript": "i need help with my policy",
  "sentiment": -0.31,
  "confidence": 0.89,
  "timestamp": "2026-02-10T10:16:05Z",
  "metadata": {
    "source": "media_stream"
  }
}
```

Notes:
- If `transcript_segments` is provided (array), each segment is ingested as a realtime event.
- If only audio is present, the API still generates an `audio_chunk` event so the realtime call state stays active.
- Supported `audio_encoding`: `pcm_s16le` and `wav` (16-bit PCM WAV).

## 11. Genesys Cloud Integration
### 11.1 Integration checklist
1. Prepare OAuth credentials from your Genesys Cloud org.
2. Configure `.env` for connector and AudioHook values.
3. Start web app (`runserver 8009`) and integration workers.
4. Build topic subscriptions from your queues/users.
5. Validate connector and listener health endpoints.
6. Point Genesys notifications and AudioHook monitor to your exposed HTTPS/WSS endpoints.

### 11.2 Configure OAuth app in Genesys
Create a client-credentials OAuth app in Genesys Cloud and capture:
- `GENESYS_CLIENT_ID`
- `GENESYS_CLIENT_SECRET`

Your OAuth app/role must allow:
- notification channel create + topic subscription
- conversation notification consumption
- queue/user read access (for preset topic builder)

Set regional endpoints correctly for your org:
- `GENESYS_LOGIN_BASE_URL`
- `GENESYS_API_BASE_URL`

### 11.3 Configure `.env`
Minimum connector values:
- `GENESYS_CLIENT_ID`
- `GENESYS_CLIENT_SECRET`
- `GENESYS_API_BASE_URL`
- `GENESYS_LOGIN_BASE_URL`
- `GENESYS_TARGET_INGEST_URL=http://127.0.0.1:8009/api/realtime/events`

Recommended security:
- `GENESYS_TARGET_INGEST_TOKEN=<token>`
- `REALTIME_INGEST_TOKEN=<same-token>`

AudioHook values:
- `GENESYS_AUDIOHOOK_PATH=/audiohook/ws`
- `GENESYS_AUDIOHOOK_TARGET_AUDIO_INGEST_URL=http://127.0.0.1:8009/api/realtime/audio/chunk`
- `GENESYS_AUDIOHOOK_TARGET_EVENT_INGEST_URL=http://127.0.0.1:8009/api/realtime/events`
- `GENESYS_AUDIOHOOK_TARGET_INGEST_TOKEN=<token>`

### 11.4 Build topic presets for your org
Generate `GENESYS_SUBSCRIPTION_TOPICS` automatically:
```powershell
python manage.py build_genesys_topics --as-env
```

Filter by queue/user/email domain:
```powershell
python manage.py build_genesys_topics --mode queues_users --queue-filter support --user-filter qa --email-domain yourcompany.com --max-queues 40 --max-users 80
```

Write full discovery payload:
```powershell
python manage.py build_genesys_topics --output-file data/outputs/genesys_topics_preview.json
```

### 11.5 Start runtime components
Web app:
```powershell
python manage.py runserver 8009
```

Genesys connector:
```powershell
python manage.py run_genesys_connector
```

AudioHook listener:
```powershell
python manage.py run_genesys_audiohook_listener
```

### 11.6 Validate connector and listener health
Connector health:
```powershell
curl "http://127.0.0.1:8009/api/integrations/genesys/health"
```

AudioHook listener health:
```powershell
curl "http://127.0.0.1:8009/api/integrations/genesys/audiohook/health"
```

Expected while healthy:
- `healthy: true`
- state shows `running`, `connecting`, or `subscribed`
- fresh `updated_at` timestamps
- counters (`forwarded_events`, `forwarded_chunks`) rising during traffic

### 11.7 Configure AudioHook monitor (listen-only dual-party audio)
1. Expose listener over public `wss://` (TLS required).
2. Configure Genesys AudioHook monitor target to:
   - `wss://<your-domain><GENESYS_AUDIOHOOK_PATH>`
3. Keep the path exact between Nginx and app config.
4. Start/verify listener worker.
5. Place test calls and confirm:
   - `/api/realtime/calls/<call_id>/audio/meta`
   - `/api/realtime/calls/<call_id>/audio`
   - timeline/event updates in call detail UI

### 11.8 Event and data mapping model
Connector worker (`run_genesys_connector`) performs:
1. OAuth token retrieval/refresh.
2. Notification channel creation.
3. Topic subscriptions (manual + preset-discovered).
4. WebSocket receive and event normalization.
5. Forward mapped payloads to `/api/realtime/events`.

AudioHook listener (`run_genesys_audiohook_listener`) performs:
1. WebSocket receive of AudioHook protocol packets.
2. Command handling (`open`, `ping`, `close`, `event`).
3. Media decode (`PCMU`/`PCMA`/`L16`) to PCM S16LE.
4. Buffered chunk forwarding to `/api/realtime/audio/chunk`.
5. End-of-call event forwarding to `/api/realtime/events`.

## 12. Configuration Reference
All settings are loaded from `.env` through `app/config.py`.

### 12.1 Core paths and DB
| Variable | Default | Description |
|---|---|---|
| `APP_NAME` | `Call Analytics` | App display name |
| `DATA_DIR` | `data` | Root data directory |
| `UPLOADS_DIR` | `data/uploads` | Uploaded audio files |
| `OUTPUTS_DIR` | `data/outputs` | Generated analytics artifacts |
| `DB_PATH` | `data/db/call_analytics.db` | SQLite DB path |
| `DATABASE_URL` | empty | Optional full DB URL override |

### 12.2 Sarvam and analysis
| Variable | Default | Description |
|---|---|---|
| `SARVAM_API_KEY` | empty | Required for batch processing |
| `SARVAM_STT_MODEL` | `saaras:v2.5` | STT model |
| `SARVAM_LLM_MODEL` | `sarvam-m` | LLM model |
| `LANGUAGE_CODE` | `en-IN` | Default language code |
| `DIARIZATION` | `true` | Enable diarization |
| `NUM_SPEAKERS` | empty | Optional fixed speaker count |
| `MAX_TRANSCRIPT_CHARS` | `12000` | Prompt input clipping limit |
| `WORKER_CONCURRENCY` | `2` | Batch worker thread count |
| `CHUNK_MINUTES` | `60` | Audio chunk size for long calls |
| `ENABLE_NOISE_SUPPRESSION` | `true` | SpeexDSP denoise pre-STT |
| `NOISE_FRAME_SIZE` | `256` | Noise suppression frame size |
| `NOISE_SAMPLE_RATE` | `16000` | Noise suppression sample rate |
| `ENABLE_PRE_LLM_CLEANUP` | `true` | Normalize transcript before LLM |
| `FILLER_WORDS` | built-in list | Filler words removed in cleanup |
| `PROMPT_PACK` | `general` | Prompt strategy |
| `GLOSSARY_TERMS` | empty | Inline glossary terms |
| `ENABLE_ROLE_HEURISTICS` | `true` | Agent/customer role heuristics |
| `GLOSSARY_PATH` | `data/glossary.csv` | Glossary CSV file |
| `AUTO_TAGS` | built-in mapping | Auto tag rules |
| `SLA_MINUTES` | `10` | SLA breach threshold |
| `ROLE_CONFIDENCE_THRESHOLD` | `0.5` | Role confidence threshold |
| `SENTIMENT_CONFIDENCE_THRESHOLD` | `0.5` | Sentiment confidence threshold |
| `ENABLE_FALLBACK_PROMPT` | `true` | Enable fallback prompt strategy |

### 12.3 Realtime alerting
| Variable | Default | Description |
|---|---|---|
| `REALTIME_INGEST_TOKEN` | empty | Optional token for `/api/realtime/events` |
| `REALTIME_NEGATIVE_SENTIMENT_THRESHOLD` | `-0.45` | Negative sentiment alert threshold |
| `REALTIME_HIGH_RISK_THRESHOLD` | `0.72` | High risk alert threshold |
| `REALTIME_ALERT_COOLDOWN_SECONDS` | `75` | Duplicate-alert cooldown |
| `REALTIME_SUPERVISOR_KEYWORD_TRIGGERS` | keyword list | Escalation keywords |
| `REALTIME_AUDIO_DIR` | `data/runtime/live_audio` | Rolling live-audio chunk storage path |
| `REALTIME_AUDIO_WINDOW_SECONDS` | `300` | Rolling audio window per call in seconds |
| `REALTIME_AUDIO_DEFAULT_SAMPLE_RATE` | `16000` | Default sample rate when chunk payload omits it |
| `REALTIME_AUDIO_DEFAULT_CHANNELS` | `1` | Default channel count when chunk payload omits it |
| `REALTIME_AUDIO_MAX_CHUNK_BYTES` | `2000000` | Max allowed decoded chunk bytes per request |

### 12.4 Genesys connector
| Variable | Default | Description |
|---|---|---|
| `GENESYS_LOGIN_BASE_URL` | `https://login.mypurecloud.com` | Genesys OAuth base URL |
| `GENESYS_API_BASE_URL` | `https://api.mypurecloud.com` | Genesys API base URL |
| `GENESYS_CLIENT_ID` | empty | OAuth client id |
| `GENESYS_CLIENT_SECRET` | empty | OAuth client secret |
| `GENESYS_SUBSCRIPTION_TOPICS` | empty | Manual topic list |
| `GENESYS_QUEUE_IDS` | empty | Manual queue ids |
| `GENESYS_USER_IDS` | empty | Manual user ids |
| `GENESYS_TARGET_INGEST_URL` | `http://127.0.0.1:8009/api/realtime/events` | Local ingest endpoint |
| `GENESYS_TARGET_INGEST_TOKEN` | empty | Optional ingest token |
| `GENESYS_VERIFY_SSL` | `true` | SSL verification for connector HTTP |
| `GENESYS_HTTP_TIMEOUT_SECONDS` | `20` | HTTP timeout |
| `GENESYS_RETRY_MAX_ATTEMPTS` | `5` | Retry attempts |
| `GENESYS_RETRY_BACKOFF_SECONDS` | `1.5` | Retry backoff |
| `GENESYS_RECONNECT_DELAY_SECONDS` | `5` | Websocket reconnect delay |
| `GENESYS_TOPIC_BUILDER_MODE` | `queues_users` | Topic builder mode |
| `GENESYS_TOPIC_BUILDER_QUEUE_NAME_FILTERS` | empty | Queue name filters |
| `GENESYS_TOPIC_BUILDER_USER_NAME_FILTERS` | empty | User name filters |
| `GENESYS_TOPIC_BUILDER_USER_EMAIL_DOMAIN_FILTERS` | empty | User email domain filters |
| `GENESYS_TOPIC_BUILDER_MAX_QUEUES` | `25` | Queue discovery limit |
| `GENESYS_TOPIC_BUILDER_MAX_USERS` | `50` | User discovery limit |
| `GENESYS_TOPIC_BUILDER_REFRESH_SECONDS` | `900` | Topic refresh interval |
| `GENESYS_CONNECTOR_STATUS_PATH` | `data/runtime/genesys_connector_status.json` | Connector heartbeat file |
| `GENESYS_CONNECTOR_HEALTH_STALE_SECONDS` | `90` | Health stale threshold |

### 12.5 Genesys AudioHook listener
| Variable | Default | Description |
|---|---|---|
| `GENESYS_AUDIOHOOK_HOST` | `0.0.0.0` | Listener bind host |
| `GENESYS_AUDIOHOOK_PORT` | `9011` | Listener bind port |
| `GENESYS_AUDIOHOOK_PATH` | `/audiohook/ws` | WebSocket path for AudioHook |
| `GENESYS_AUDIOHOOK_TARGET_AUDIO_INGEST_URL` | `http://127.0.0.1:8009/api/realtime/audio/chunk` | Local audio-chunk ingest endpoint |
| `GENESYS_AUDIOHOOK_TARGET_EVENT_INGEST_URL` | `http://127.0.0.1:8009/api/realtime/events` | Local event ingest endpoint |
| `GENESYS_AUDIOHOOK_TARGET_INGEST_TOKEN` | empty | Optional ingest token |
| `GENESYS_AUDIOHOOK_VERIFY_SSL` | `true` | SSL verification for listener forwarding HTTP |
| `GENESYS_AUDIOHOOK_HTTP_TIMEOUT_SECONDS` | `20` | Forwarding HTTP timeout |
| `GENESYS_AUDIOHOOK_RETRY_MAX_ATTEMPTS` | `5` | Forwarding retry attempts |
| `GENESYS_AUDIOHOOK_RETRY_BACKOFF_SECONDS` | `1.5` | Forwarding retry backoff |
| `GENESYS_AUDIOHOOK_FLUSH_INTERVAL_MS` | `750` | Flush cadence for buffered media |
| `GENESYS_AUDIOHOOK_MIN_CHUNK_DURATION_MS` | `300` | Minimum buffered duration before flush |
| `GENESYS_AUDIOHOOK_MAX_CHUNK_DURATION_MS` | `2000` | Max duration per forwarded chunk |
| `GENESYS_AUDIOHOOK_STATUS_PATH` | `data/runtime/genesys_audiohook_status.json` | Listener heartbeat file |
| `GENESYS_AUDIOHOOK_HEALTH_STALE_SECONDS` | `90` | Listener health stale threshold |

## 13. Logging and Runtime Files
### Logs
Daily rotating logs:
- `log/application.log` (DEBUG+)
- `log/error.log` (WARNING+)

Retention:
- 30 backups (`TimedRotatingFileHandler`, midnight rotation)

### Runtime files
- `data/runtime/genesys_connector_status.json` contains connector heartbeat and counters
- `data/runtime/genesys_audiohook_status.json` contains AudioHook listener heartbeat and counters
- `data/runtime/live_audio/<call_id>/state.json` and `*.pcm` store rolling live audio chunks

### Data output structure
- `data/uploads/<call_id>_<filename>`
- `data/outputs/<call_id>/transcript.json`
- `data/outputs/<call_id>/analysis.json`
- `data/outputs/<call_id>/summary.json`
- `data/outputs/<call_id>/qa.json`

## 14. Troubleshooting
### App does not start
- Run `python manage.py check`
- Verify `.env` syntax and required values
- Confirm DB path is writable

### Batch call stuck in `failed`
- Check `log/error.log`
- Verify `SARVAM_API_KEY`
- Validate uploaded audio format and ffmpeg availability

### Realtime events accepted but no UI updates
- Check SSE endpoint `/api/realtime/stream?call_id=<id>`
- Verify event `call_id` matches the page call id
- Check browser console/network for stream disconnects

### Live audio not playing/updating
- Check `/api/realtime/calls/<call_id>/audio/meta` and confirm `live_audio.available=true`
- Ensure chunk payload is valid base64 PCM/WAV and sample format is supported
- Verify `REALTIME_AUDIO_MAX_CHUNK_BYTES` is not too low for your media frame size

### Genesys connector unhealthy
- Call `/api/integrations/genesys/health`
- Inspect `status.last_error`
- Confirm OAuth credentials and API regions
- Validate topic configuration (`build_genesys_topics --as-env`)

### AudioHook listener unhealthy
- Call `/api/integrations/genesys/audiohook/health`
- Confirm listener worker is running and path matches `GENESYS_AUDIOHOOK_PATH`
- Verify reverse proxy/TLS exposes `wss://` endpoint to Genesys Cloud
- Check `log/error.log` for packet decode or forwarding errors

### Status shows stale
- Ensure connector worker process is running
- Increase `GENESYS_CONNECTOR_HEALTH_STALE_SECONDS` if needed
- For AudioHook listener, check `GENESYS_AUDIOHOOK_HEALTH_STALE_SECONDS`

## 15. Security and Production Checklist
- Replace Django `SECRET_KEY` and set `DEBUG=false` for production
- Restrict `ALLOWED_HOSTS`
- Use HTTPS and secure reverse proxy
- Set `REALTIME_INGEST_TOKEN` and `GENESYS_TARGET_INGEST_TOKEN`
- Protect `.env` and avoid committing secrets
- Rotate credentials regularly
- Monitor logs and alert on repeated connector failures

## 16. Command Cheat Sheet
```powershell
# Setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py check

# Run app
python manage.py runserver 8009

# Batch usage
# Use UI at /upload

# Realtime test event
curl -X POST "http://127.0.0.1:8009/api/realtime/events" -H "Content-Type: application/json" -d "{\"provider\":\"test\",\"call_id\":\"RT-001\",\"event_type\":\"transcript\",\"speaker\":\"customer\",\"text\":\"need help\",\"sentiment\":-0.4}"

# Realtime audio chunk (example uses short dummy payload)
curl -X POST "http://127.0.0.1:8009/api/realtime/audio/chunk" -H "Content-Type: application/json" -d "{\"provider\":\"test\",\"call_id\":\"RT-001\",\"audio_encoding\":\"pcm_s16le\",\"sample_rate\":16000,\"channels\":1,\"audio_b64\":\"AAABAA==\",\"transcript\":\"hello\"}"

# Genesys topic preset
python manage.py build_genesys_topics --as-env

# Run Genesys connector
python manage.py run_genesys_connector
python manage.py run_genesys_connector --dry-run --log-level DEBUG

# Connector health
curl "http://127.0.0.1:8009/api/integrations/genesys/health"

# Run AudioHook listener
python manage.py run_genesys_audiohook_listener
python manage.py run_genesys_audiohook_listener --dry-run --log-level DEBUG

# AudioHook listener health
curl "http://127.0.0.1:8009/api/integrations/genesys/audiohook/health"

# Deploy automation (Linux)
chmod +x deploy/linux/install.sh deploy/linux/uninstall.sh
sudo ./deploy/linux/install.sh
sudo ./deploy/linux/uninstall.sh

# Deploy automation (Windows)
.\deploy\windows\install.ps1
.\deploy\windows\uninstall.ps1
```

## 17. Deployment Automation
The repository includes one-command automated install/uninstall for both OS families.

Reference:
- `https://github.com/raghu1518/call-analytics/blob/main/deploy/README.md`

Included scripts:
- Linux:
  - `deploy/linux/install.sh`
  - `deploy/linux/uninstall.sh`
- Windows:
  - `deploy/windows/install.ps1`
  - `deploy/windows/uninstall.ps1`
  - `deploy/windows/start-web.ps1`
  - `deploy/windows/start-genesys-connector.ps1`
  - `deploy/windows/start-audiohook.ps1`

Install behavior:
1. Create `.venv`
2. Install dependencies
3. Create `.env` from `.env.example` if missing
4. Create runtime directories
5. Run migrations/checks
6. Register services/tasks for startup

Uninstall behavior:
- Removes services/tasks
- Optional purge flags for `.venv`, `data/`, `log/`, `.env`

## 18. Nginx Single Domain Multi-Path Setup
If you host multiple apps under one domain with path extensions, this app works in the same model.

Example:
- Main web app under: `https://example.com/call-analytics/`
- AudioHook WebSocket under: `wss://example.com/call-analytics/audiohook/ws`

Minimal Nginx pattern:
```nginx
location /call-analytics/ {
    proxy_pass http://127.0.0.1:8009/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location /call-analytics/audiohook/ws {
    proxy_pass http://127.0.0.1:9011/audiohook/ws;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
}
```

Required alignment:
1. Keep `GENESYS_AUDIOHOOK_PATH` exactly equal to proxied websocket path on the listener.
2. In Genesys AudioHook monitor, use the public `wss://` URL exposed by Nginx.
3. Keep `GENESYS_TARGET_INGEST_URL` and `GENESYS_AUDIOHOOK_TARGET_*` pointing to your Django app URLs.
