from __future__ import annotations

import csv
import io
import json
import shutil
import anyio
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.config import settings
from app.db import get_session, init_db
from app.models import Call
from app.services.pipeline import CallAnalyticsPipeline
from app.services.sarvam_client import SarvamService

app = FastAPI(title=settings.app_name)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, message: dict[str, object]) -> None:
        async with self._lock:
            connections = list(self._connections)
        stale = []
        for websocket in connections:
            try:
                await websocket.send_json(message)
            except Exception:
                stale.append(websocket)
        if stale:
            async with self._lock:
                for websocket in stale:
                    self._connections.discard(websocket)


PROMPT_PACKS = [
    {"value": "general", "label": "General"},
    {"value": "sales", "label": "Sales"},
    {"value": "support", "label": "Support"},
    {"value": "collections", "label": "Collections"},
]


@app.on_event("startup")
def on_startup() -> None:
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db()
    app.state.executor = ThreadPoolExecutor(max_workers=settings.worker_concurrency)
    app.state.pipeline = CallAnalyticsPipeline(SarvamService(settings.sarvam_api_key))
    app.state.ws_manager = WebSocketManager()


@app.on_event("shutdown")
def on_shutdown() -> None:
    executor: ThreadPoolExecutor = app.state.executor
    executor.shutdown(wait=False)


@app.get("/")
def dashboard(request: Request):
    params = request.query_params
    query = params.get("q", "").strip()
    status = params.get("status", "all").strip().lower()
    date_from = _parse_date(params.get("date_from"))
    date_to = _parse_date(params.get("date_to"))
    topic = params.get("topic", "").strip().lower()
    role = params.get("role", "").strip().title()
    page = _parse_int(params.get("page"), default=1)
    page_size = _parse_int(params.get("page_size"), default=10)

    with get_session() as session:
        calls = session.exec(select(Call).order_by(Call.created_at.desc())).all()

    filtered_calls = _filter_calls(
        calls=calls,
        query=query,
        status=status,
        date_from=date_from,
        date_to=date_to,
        topic=topic,
        role=role,
    )

    total_filtered = len(filtered_calls)
    total_pages = max(1, (total_filtered + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    page_calls = filtered_calls[start:end]

    prev_url = (
        str(request.url.include_query_params(page=page - 1))
        if page > 1
        else None
    )
    next_url = (
        str(request.url.include_query_params(page=page + 1))
        if page < total_pages
        else None
    )

    call_items = [_build_call_item(call) for call in page_calls]
    metrics = _build_metrics(filtered_calls)
    metrics["total_all"] = len(calls)

    chart_data = _build_chart_data(filtered_calls)
    insights = _build_insights_data(filtered_calls)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "metrics": metrics,
            "recent_calls": call_items,
            "chart_data": chart_data,
            "insights": insights,
            "filters": {
                "q": query,
                "status": status,
                "date_from": params.get("date_from", ""),
                "date_to": params.get("date_to", ""),
                "topic": topic,
                "role": role,
                "page_size": page_size,
            },
            "pagination": {
                "page": page,
                "total_pages": total_pages,
                "total_filtered": total_filtered,
                "prev_url": prev_url,
                "next_url": next_url,
            },
        },
    )


@app.get("/upload")
def upload_page(request: Request):
    defaults = {
        "language_code": settings.language_code,
        "stt_model": settings.sarvam_stt_model,
        "prompt_pack": settings.prompt_pack,
        "prompt_packs": PROMPT_PACKS,
    }
    return templates.TemplateResponse(
        "upload.html", {"request": request, "defaults": defaults}
    )


@app.websocket("/ws/calls")
async def ws_calls(websocket: WebSocket):
    manager: WebSocketManager = app.state.ws_manager
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(websocket)


@app.post("/glossary/upload")
async def upload_glossary(file: UploadFile = File(...)):
    settings.glossary_path.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    settings.glossary_path.write_bytes(content)
    return RedirectResponse(url="/", status_code=302)


@app.post("/upload")
async def upload_call(
    request: Request,
    file: UploadFile = File(...),
    language_code: str = Form(settings.language_code),
    stt_model: str = Form(settings.sarvam_stt_model),
    with_diarization: bool = Form(False),
    num_speakers: str | None = Form(None),
    prompt: str | None = Form(None),
    prompt_pack: str | None = Form(None),
    glossary_terms: str | None = Form(None),
):
    valid_packs = {pack["value"] for pack in PROMPT_PACKS}
    selected_pack = prompt_pack if prompt_pack in valid_packs else settings.prompt_pack
    call_id = _new_call_id()
    filename = file.filename or f"call_{call_id}.audio"
    storage_path = settings.uploads_dir / f"{call_id}_{filename}"

    with storage_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    parsed_num_speakers = None
    if num_speakers:
        try:
            parsed_num_speakers = int(num_speakers)
        except ValueError:
            parsed_num_speakers = None

    call = Call(
        id=call_id,
        filename=filename,
        storage_path=str(storage_path),
        status="queued",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        language_code=language_code,
        stt_model=stt_model,
        with_diarization=with_diarization,
        num_speakers=parsed_num_speakers,
        prompt=prompt,
        prompt_pack=selected_pack,
        glossary_terms=glossary_terms or "",
    )

    with get_session() as session:
        session.add(call)
        session.commit()

    _enqueue_call(call_id)

    return RedirectResponse(url=f"/calls/{call_id}", status_code=302)


@app.get("/calls/{call_id}")
def call_detail(request: Request, call_id: str):
    with get_session() as session:
        call = session.get(Call, call_id)

    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    transcript_data = _read_json(call.transcript_json_path)
    transcript_text = _read_text(call.transcript_text_path)
    analysis_bundle = _read_json(call.analysis_json_path) or {}
    if analysis_bundle.get("raw_text") and not analysis_bundle.get("summary"):
        parsed = _parse_raw_bundle(str(analysis_bundle.get("raw_text")))
        if parsed:
            analysis_bundle = parsed

    summary = _extract_summary(analysis_bundle, call.summary_json_path)
    qa_pairs = _extract_qa(analysis_bundle, call.qa_json_path)
    analysis_view = _extract_analysis_view(analysis_bundle)
    analysis_meta = _extract_analysis_meta(analysis_bundle)

    speaker_roles = _extract_speaker_roles(analysis_bundle)
    if not speaker_roles:
        speaker_roles = _infer_roles_from_transcript(transcript_data)
    transcript_segments = _build_transcript_segments(transcript_data, speaker_roles)
    transcript_render = _render_transcript(transcript_data, speaker_roles) or transcript_text
    speaker_stats = _render_speaker_stats(transcript_data, speaker_roles)

    return templates.TemplateResponse(
        "call_detail.html",
        {
            "request": request,
            "call": call,
            "transcript": transcript_data,
            "transcript_text": transcript_text,
            "analysis": analysis_view,
            "analysis_meta": analysis_meta,
            "summary": summary,
            "qa_pairs": qa_pairs,
            "transcript_render": transcript_render,
            "speaker_stats": speaker_stats,
            "transcript_segments": transcript_segments,
        },
    )


@app.get("/calls/{call_id}/audio")
def call_audio(call_id: str):
    with get_session() as session:
        call = session.get(Call, call_id)
    if not call or not call.storage_path:
        raise HTTPException(status_code=404, detail="Audio not found")
    path = Path(call.storage_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path, filename=path.name)


@app.post("/calls/bulk")
def bulk_actions(
    action: str = Form(...),
    call_ids: list[str] = Form(...),
):
    action = action.lower()
    with get_session() as session:
        calls = session.exec(select(Call).where(Call.id.in_(call_ids))).all()

    if not calls:
        raise HTTPException(status_code=400, detail="No calls selected")

    if action in {"export_json", "export_csv", "export_transcript_csv"}:
        return _bulk_export(calls, action)

    if action == "reprocess":
        for call in calls:
            _reset_call_for_reprocess(call)
            _enqueue_call(call.id)
        return RedirectResponse(url="/", status_code=302)

    if action == "delete":
        with get_session() as session:
            for call in calls:
                _delete_call_assets(call)
                call_in_db = session.get(Call, call.id)
                if call_in_db:
                    session.delete(call_in_db)
            session.commit()
        return RedirectResponse(url="/", status_code=302)

    raise HTTPException(status_code=400, detail="Unsupported bulk action")


@app.get("/calls/{call_id}/export")
def export_call(call_id: str, format: str = "json", scope: str = "insights"):
    with get_session() as session:
        call = session.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    transcript_data = _read_json(call.transcript_json_path) or {}
    analysis_bundle = _read_json(call.analysis_json_path) or {}
    if analysis_bundle.get("raw_text") and not analysis_bundle.get("summary"):
        parsed = _parse_raw_bundle(str(analysis_bundle.get("raw_text")))
        if parsed:
            analysis_bundle = parsed

    scope = scope.lower()
    format = format.lower()

    if format == "json":
        payload = {
            "call": {
                "id": call.id,
                "filename": call.filename,
                "status": call.status,
                "created_at": call.created_at.isoformat() + "Z",
                "language_code": call.language_code,
                "stt_model": call.stt_model,
            },
            "transcript": transcript_data if scope == "transcript" else None,
            "analysis": analysis_bundle if scope != "transcript" else None,
        }
        content = json.dumps(payload, indent=2)
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f"attachment; filename=call_{call.id}_{scope}.json"
            },
        )

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        if scope == "transcript":
            writer.writerow(["speaker_id", "start_time_seconds", "end_time_seconds", "transcript"])
            entries = transcript_data.get("entries", []) if isinstance(transcript_data, dict) else []
            for entry in entries:
                writer.writerow(
                    [
                        entry.get("speaker_id"),
                        entry.get("start_time_seconds"),
                        entry.get("end_time_seconds"),
                        entry.get("transcript"),
                    ]
                )
        else:
            summary = _extract_summary(analysis_bundle, call.summary_json_path)
            analysis_view = _extract_analysis_view(analysis_bundle)
            writer.writerow(
                [
                    "call_id",
                    "filename",
                    "summary_short",
                    "sentiment_overall",
                    "sentiment_customer",
                    "sentiment_agent",
                    "topics",
                    "action_items",
                    "resolution_status",
                ]
            )
            writer.writerow(
                [
                    call.id,
                    call.filename,
                    summary.get("short", ""),
                    analysis_view.get("sentiment", {}).get("overall"),
                    analysis_view.get("sentiment", {}).get("customer"),
                    analysis_view.get("sentiment", {}).get("agent"),
                    "; ".join(analysis_view.get("topics", [])),
                    "; ".join(analysis_view.get("action_items", [])),
                    analysis_view.get("resolution_status"),
                ]
            )
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=call_{call.id}_{scope}.csv"
            },
        )

    raise HTTPException(status_code=400, detail="Unsupported export format")


@app.get("/api/metrics")
def api_metrics():
    with get_session() as session:
        calls = session.exec(select(Call).order_by(Call.created_at.desc())).all()
    chart_data = _build_chart_data(calls)
    return JSONResponse(chart_data)


@app.get("/api/calls/{call_id}")
def api_call(call_id: str):
    with get_session() as session:
        call = session.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return JSONResponse(
        {
            "id": call.id,
            "status": call.status,
            "error_message": call.error_message,
        }
    )


def _enqueue_call(call_id: str) -> None:
    executor: ThreadPoolExecutor = app.state.executor
    executor.submit(_process_call, call_id)


def _process_call(call_id: str) -> None:
    call_data: dict[str, object] = {}
    with get_session() as session:
        call = session.get(Call, call_id)
        if not call:
            return
        call_data = {
            "storage_path": call.storage_path,
            "language_code": call.language_code,
            "stt_model": call.stt_model,
            "with_diarization": call.with_diarization,
            "num_speakers": call.num_speakers,
            "prompt": call.prompt,
            "prompt_pack": call.prompt_pack,
            "glossary_terms": call.glossary_terms,
        }
        call.status = "processing"
        call.updated_at = datetime.utcnow()
        session.add(call)
        session.commit()

    _emit_ws_event(
        {
            "type": "call_update",
            "call_id": call_id,
            "status": "processing",
            "progress": 10,
            "stage": "processing",
        }
    )

    pipeline: CallAnalyticsPipeline = app.state.pipeline
    try:
        def on_progress(stage: str, progress: float | None, meta: dict[str, object]) -> None:
            _emit_ws_event(
                {
                    "type": "call_update",
                    "call_id": call_id,
                    "status": "processing",
                    "progress": progress,
                    "stage": stage,
                    "meta": meta,
                }
            )

        output = pipeline.process(
            audio_path=Path(str(call_data.get("storage_path"))),
            output_dir=settings.outputs_dir / call_id,
            language_code=str(call_data.get("language_code") or settings.language_code),
            stt_model=str(call_data.get("stt_model") or settings.sarvam_stt_model),
            with_diarization=bool(call_data.get("with_diarization")),
            num_speakers=call_data.get("num_speakers") if call_data.get("num_speakers") else None,
            prompt=call_data.get("prompt") if call_data.get("prompt") else None,
            prompt_pack=str(call_data.get("prompt_pack") or settings.prompt_pack),
            glossary_terms=str(call_data.get("glossary_terms") or ""),
            on_progress=on_progress,
        )
    except Exception as exc:
        with get_session() as session:
            call = session.get(Call, call_id)
            if call:
                call.status = "failed"
                call.updated_at = datetime.utcnow()
                call.error_message = str(exc)
                session.add(call)
                session.commit()
        _emit_ws_event(
            {
                "type": "call_update",
                "call_id": call_id,
                "status": "failed",
                "progress": 100,
                "stage": "failed",
                "error": str(exc),
            }
        )
        return

    with get_session() as session:
        call = session.get(Call, call_id)
        if not call:
            return
        call.status = "completed"
        call.updated_at = datetime.utcnow()
        call.duration_seconds = output.duration_seconds
        call.transcript_text_path = str(output.transcript_text_path)
        call.transcript_json_path = str(output.transcript_json_path)
        call.analysis_json_path = str(output.analysis_json_path)
        call.qa_json_path = str(output.qa_json_path)
        call.summary_json_path = str(output.summary_json_path)
        call.raw_llm_path = str(output.raw_llm_path)
        session.add(call)
        session.commit()

    _emit_ws_event(
        {
            "type": "call_update",
            "call_id": call_id,
            "status": "completed",
            "progress": 100,
            "stage": "completed",
        }
    )


def _build_metrics(calls: list[Call]) -> dict[str, object]:
    total = len(calls)
    completed = len([c for c in calls if c.status == "completed"])
    processing = len([c for c in calls if c.status in {"processing", "queued"}])
    failed = len([c for c in calls if c.status == "failed"])
    durations = [c.duration_seconds for c in calls if c.duration_seconds]
    avg_duration = round(sum(durations) / len(durations), 2) if durations else None
    top_model = _most_common([c.stt_model for c in calls if c.stt_model])
    top_language = _most_common([c.language_code for c in calls if c.language_code])

    return {
        "total": total,
        "completed": completed,
        "processing": processing,
        "failed": failed,
        "avg_duration": avg_duration,
        "top_model": top_model,
        "top_language": top_language,
        "recent_count": min(total, 10),
    }


def _build_chart_data(calls: list[Call]) -> dict[str, list]:
    today = datetime.utcnow().date()
    days = [today - timedelta(days=delta) for delta in range(6, -1, -1)]
    labels = [day.strftime("%b %d") for day in days]
    values = []
    for day in days:
        count = len([c for c in calls if c.created_at.date() == day])
        values.append(count)
    return {"labels": labels, "values": values}


def _build_insights_data(calls: list[Call]) -> dict[str, object]:
    analysis_cache: dict[str, dict] = {}
    sentiment_by_day: dict[str, list[float]] = {}
    topic_counts: dict[str, int] = {}
    topic_by_day: dict[str, dict[str, int]] = {}
    resolved = 0
    total_with_resolution = 0
    sla_breaches = 0

    agent_metrics = []

    for call in calls:
        analysis = _load_analysis_bundle(call, analysis_cache)
        sentiment = analysis.get("sentiment", {})
        if isinstance(sentiment, dict):
            try:
                value = float(sentiment.get("overall"))
            except (TypeError, ValueError):
                value = None
            if value is not None:
                day = call.created_at.strftime("%Y-%m-%d")
                sentiment_by_day.setdefault(day, []).append(value)

        resolution = analysis.get("resolution", {})
        if isinstance(resolution, dict) and resolution.get("status"):
            total_with_resolution += 1
            if str(resolution.get("status")).lower() == "resolved":
                resolved += 1

        topics = analysis.get("topics", [])
        if isinstance(topics, list):
            for topic in topics:
                topic_key = str(topic)
                topic_counts[topic_key] = topic_counts.get(topic_key, 0) + 1
                day_key = call.created_at.strftime("%Y-%m-%d")
                topic_by_day.setdefault(topic_key, {})
                topic_by_day[topic_key][day_key] = topic_by_day[topic_key].get(day_key, 0) + 1

        sla = analysis.get("sla", {})
        if isinstance(sla, dict) and sla.get("breach") is True:
            sla_breaches += 1

        transcript_entries = _load_transcript_entries(call)
        if transcript_entries:
            roles = analysis.get("speaker_roles", {})
            metrics = _compute_agent_metrics(transcript_entries, roles)
            if metrics:
                agent_metrics.append(metrics)

    sentiment_labels = sorted(sentiment_by_day.keys())
    sentiment_values = [
        round(sum(values) / len(values), 2) for values in (sentiment_by_day[label] for label in sentiment_labels)
    ]

    resolution_rate = (
        round((resolved / total_with_resolution) * 100, 1)
        if total_with_resolution
        else 0.0
    )

    top_topics = sorted(topic_counts.items(), key=lambda item: item[1], reverse=True)[:8]
    topics_labels = [label for label, _ in top_topics]
    topics_values = [count for _, count in top_topics]

    heatmap_days = [
        (datetime.utcnow().date() - timedelta(days=delta)) for delta in range(6, -1, -1)
    ]
    heatmap_labels = [day.strftime("%b %d") for day in heatmap_days]
    heatmap_rows = []
    for topic, _ in top_topics:
        counts = [
            topic_by_day.get(topic, {}).get(day.strftime("%Y-%m-%d"), 0)
            for day in heatmap_days
        ]
        max_count = max(counts) if counts else 0
        levels = [
            _heatmap_level(count, max_count) for count in counts
        ]
        heatmap_rows.append({"topic": topic, "levels": levels})

    agent_snapshot = _aggregate_agent_metrics(agent_metrics)

    return {
        "sentiment": {"labels": sentiment_labels, "values": sentiment_values},
        "topics": {"labels": topics_labels, "values": topics_values},
        "topic_heatmap": {"days": heatmap_labels, "rows": heatmap_rows},
        "resolution_rate": resolution_rate,
        "agent": agent_snapshot,
        "sla_breaches": sla_breaches,
    }


def _bulk_export(calls: list[Call], action: str) -> Response:
    if action == "export_json":
        payload = []
        for call in calls:
            analysis_bundle = _read_json(call.analysis_json_path) or {}
            if analysis_bundle.get("raw_text") and not analysis_bundle.get("summary"):
                parsed = _parse_raw_bundle(str(analysis_bundle.get("raw_text")))
                if parsed:
                    analysis_bundle = parsed
            payload.append(
                {
                    "id": call.id,
                    "filename": call.filename,
                    "status": call.status,
                    "created_at": call.created_at.isoformat() + "Z",
                    "analysis": analysis_bundle,
                }
            )
        content = json.dumps(payload, indent=2)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=calls_export.json"},
        )

    output = io.StringIO()
    writer = csv.writer(output)
    if action == "export_transcript_csv":
        writer.writerow(
            ["call_id", "speaker_id", "start_time_seconds", "end_time_seconds", "transcript"]
        )
        for call in calls:
            transcript_data = _read_json(call.transcript_json_path) or {}
            entries = transcript_data.get("entries", []) if isinstance(transcript_data, dict) else []
            for entry in entries:
                writer.writerow(
                    [
                        call.id,
                        entry.get("speaker_id"),
                        entry.get("start_time_seconds"),
                        entry.get("end_time_seconds"),
                        entry.get("transcript"),
                    ]
                )
    else:
        writer.writerow(
            [
                "call_id",
                "filename",
                "status",
                "created_at",
                "summary_short",
                "sentiment_overall",
                "sentiment_customer",
                "sentiment_agent",
                "topics",
                "action_items",
                "resolution_status",
            ]
        )
        for call in calls:
            analysis_bundle = _read_json(call.analysis_json_path) or {}
            if analysis_bundle.get("raw_text") and not analysis_bundle.get("summary"):
                parsed = _parse_raw_bundle(str(analysis_bundle.get("raw_text")))
                if parsed:
                    analysis_bundle = parsed
            summary = _extract_summary(analysis_bundle, call.summary_json_path)
            analysis_view = _extract_analysis_view(analysis_bundle)
            writer.writerow(
                [
                    call.id,
                    call.filename,
                    call.status,
                    call.created_at.isoformat() + "Z",
                    summary.get("short", ""),
                    analysis_view.get("sentiment", {}).get("overall"),
                    analysis_view.get("sentiment", {}).get("customer"),
                    analysis_view.get("sentiment", {}).get("agent"),
                    "; ".join(analysis_view.get("topics", [])),
                    "; ".join(analysis_view.get("action_items", [])),
                    analysis_view.get("resolution_status"),
                ]
            )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=calls_export.csv"},
    )


def _reset_call_for_reprocess(call: Call) -> None:
    with get_session() as session:
        call_in_db = session.get(Call, call.id)
        if not call_in_db:
            return
        _delete_call_assets(call_in_db, keep_upload=True)
        call_in_db.status = "queued"
        call_in_db.error_message = None
        call_in_db.updated_at = datetime.utcnow()
        session.add(call_in_db)
        session.commit()


def _delete_call_assets(call: Call, keep_upload: bool = False) -> None:
    if call.transcript_json_path:
        _safe_unlink(Path(call.transcript_json_path))
    if call.transcript_text_path:
        _safe_unlink(Path(call.transcript_text_path))
    if call.analysis_json_path:
        _safe_unlink(Path(call.analysis_json_path))
    if call.qa_json_path:
        _safe_unlink(Path(call.qa_json_path))
    if call.summary_json_path:
        _safe_unlink(Path(call.summary_json_path))
    if call.raw_llm_path:
        _safe_unlink(Path(call.raw_llm_path))
    output_dir = settings.outputs_dir / call.id
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)
    if not keep_upload and call.storage_path:
        _safe_unlink(Path(call.storage_path))


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        return


def _emit_ws_event(payload: dict[str, object]) -> None:
    try:
        manager: WebSocketManager = app.state.ws_manager
    except Exception:
        return
    try:
        anyio.from_thread.run(manager.broadcast, payload)
    except Exception:
        return


def _load_transcript_entries(call: Call) -> list[dict]:
    transcript_data = _read_json(call.transcript_json_path)
    if not isinstance(transcript_data, dict):
        return []
    entries = transcript_data.get("entries", [])
    if isinstance(entries, list):
        return entries
    return []


def _compute_agent_metrics(entries: list[dict], roles: dict) -> dict[str, float] | None:
    if not entries:
        return None

    role_map = {str(k): str(v).lower() for k, v in roles.items()} if isinstance(roles, dict) else {}
    agent_duration = 0.0
    customer_duration = 0.0
    interruptions = 0
    empathy_hits = 0
    agent_turns = 0

    empathy_phrases = [
        "i understand",
        "i'm sorry",
        "apologize",
        "sorry for",
        "i can imagine",
        "that sounds",
        "i know this is",
    ]

    last_end = None
    last_role = None

    for entry in entries:
        speaker_id = str(entry.get("speaker_id", "speaker"))
        role = role_map.get(speaker_id, "other")
        start = float(entry.get("start_time_seconds", 0) or 0)
        end = float(entry.get("end_time_seconds", start) or start)
        duration = max(0.0, end - start)

        if role == "agent":
            agent_duration += duration
            agent_turns += 1
            text = str(entry.get("transcript", "")).lower()
            if any(phrase in text for phrase in empathy_phrases):
                empathy_hits += 1
        elif role == "customer":
            customer_duration += duration

        if last_role == "customer" and role == "agent" and last_end is not None:
            gap = start - last_end
            if gap <= 0.2:
                interruptions += 1
        last_end = end
        last_role = role

    total = agent_duration + customer_duration
    talk_ratio = agent_duration / total if total > 0 else 0.0
    empathy_score = empathy_hits / agent_turns if agent_turns else 0.0

    return {
        "talk_ratio": round(talk_ratio, 2),
        "interruptions": interruptions,
        "empathy": round(min(1.0, empathy_score), 2),
    }


def _aggregate_agent_metrics(metrics: list[dict[str, float]]) -> dict[str, float]:
    if not metrics:
        return {"talk_ratio": 0.0, "interruptions": 0.0, "empathy": 0.0}
    talk_ratio = sum(item.get("talk_ratio", 0) for item in metrics) / len(metrics)
    interruptions = sum(item.get("interruptions", 0) for item in metrics) / len(metrics)
    empathy = sum(item.get("empathy", 0) for item in metrics) / len(metrics)
    return {
        "talk_ratio": round(talk_ratio, 2),
        "interruptions": round(interruptions, 2),
        "empathy": round(empathy, 2),
    }


def _heatmap_level(value: int, max_value: int) -> int:
    if max_value <= 0:
        return 0
    ratio = value / max_value
    if ratio >= 0.8:
        return 4
    if ratio >= 0.6:
        return 3
    if ratio >= 0.4:
        return 2
    if ratio >= 0.2:
        return 1
    return 0


def _filter_calls(
    calls: list[Call],
    query: str,
    status: str,
    date_from: datetime | None,
    date_to: datetime | None,
    topic: str,
    role: str,
) -> list[Call]:
    analysis_cache: dict[str, dict] = {}
    filtered: list[Call] = []
    for call in calls:
        if status != "all" and call.status != status:
            continue
        if date_from and call.created_at.date() < date_from.date():
            continue
        if date_to and call.created_at.date() > date_to.date():
            continue
        if query:
            haystack = " ".join(
                [
                    call.id,
                    call.filename or "",
                    call.language_code or "",
                    call.stt_model or "",
                    call.status or "",
                ]
            ).lower()
            if query.lower() not in haystack:
                continue
        if topic or role:
            analysis = _load_analysis_bundle(call, analysis_cache)
            if topic and not _analysis_has_topic(analysis, topic):
                continue
            if role and not _analysis_has_role(analysis, role):
                continue
        filtered.append(call)
    return filtered


def _analysis_has_topic(analysis: dict, topic: str) -> bool:
    topics = analysis.get("topics", [])
    if not isinstance(topics, list):
        return False
    return any(topic in str(item).lower() for item in topics)


def _analysis_has_role(analysis: dict, role: str) -> bool:
    roles = analysis.get("speaker_roles", {})
    if not isinstance(roles, dict):
        return False
    role_lower = role.lower()
    return any(str(value).lower() == role_lower for value in roles.values())


def _load_analysis_bundle(call: Call, cache: dict[str, dict]) -> dict:
    if call.id in cache:
        return cache[call.id]
    bundle = _read_json(call.analysis_json_path) or {}
    if bundle.get("raw_text") and not bundle.get("summary"):
        parsed = _parse_raw_bundle(str(bundle.get("raw_text")))
        if parsed:
            bundle = parsed
    cache[call.id] = bundle
    return bundle


def _build_call_item(call: Call) -> dict[str, object]:
    analysis = _load_analysis_bundle(call, {})
    topics = analysis.get("topics", [])
    if not isinstance(topics, list):
        topics = []
    topics = [str(topic) for topic in topics if topic]
    roles = analysis.get("speaker_roles", {})
    if isinstance(roles, dict):
        role_summary = sorted({str(role) for role in roles.values()})
    else:
        role_summary = []
    sla = analysis.get("sla", {})
    sla_breach = isinstance(sla, dict) and sla.get("breach") is True
    return {
        "call": call,
        "progress": _status_progress(call.status),
        "topics": topics[:3],
        "roles": role_summary,
        "sla_breach": sla_breach,
    }


def _status_progress(status: str) -> int:
    if status == "completed":
        return 100
    if status == "failed":
        return 100
    if status == "processing":
        return 60
    if status == "queued":
        return 15
    return 0


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _read_json(path_str: str | None) -> dict | list | None:
    if not path_str:
        return None
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _read_text(path_str: str | None) -> str | None:
    if not path_str:
        return None
    path = Path(path_str)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _extract_summary(bundle: dict, summary_path: str | None) -> dict:
    summary = bundle.get("summary")
    if summary is None:
        summary = _read_json(summary_path)

    if isinstance(summary, str):
        return {"short": summary, "bullets": []}
    if isinstance(summary, dict):
        return {
            "short": summary.get("short") or summary.get("summary") or "",
            "bullets": summary.get("bullets", []) or [],
        }
    return {"short": "", "bullets": []}


def _extract_qa(bundle: dict, qa_path: str | None) -> list:
    if bundle.get("qa_pairs"):
        return bundle.get("qa_pairs")
    qa = _read_json(qa_path)
    if isinstance(qa, list):
        return qa
    return []


def _extract_analysis_view(bundle: dict) -> dict:
    sentiment = bundle.get("sentiment", {}) if isinstance(bundle.get("sentiment"), dict) else {}
    resolution = bundle.get("resolution", {}) if isinstance(bundle.get("resolution"), dict) else {}
    resolution_steps = resolution.get("next_steps", [])
    if isinstance(resolution_steps, str):
        resolution_steps = [resolution_steps]
    action_items = bundle.get("action_items", []) or []
    normalized_items: list[str] = []
    if isinstance(action_items, list):
        for item in action_items:
            if isinstance(item, dict):
                normalized_items.append(str(item.get("description") or item.get("text") or item))
            else:
                normalized_items.append(str(item))
    else:
        normalized_items = [str(action_items)]
    return {
        "sentiment": sentiment,
        "topics": bundle.get("topics", []) or [],
        "action_items": normalized_items,
        "resolution_status": resolution.get("status"),
        "resolution_steps": resolution_steps or [],
    }


def _extract_analysis_meta(bundle: dict) -> dict:
    sentiment = bundle.get("sentiment", {}) if isinstance(bundle.get("sentiment"), dict) else {}
    try:
        sentiment_conf = float(sentiment.get("confidence"))
    except (TypeError, ValueError):
        sentiment_conf = None
    auto_tags = bundle.get("auto_tags", [])
    if not isinstance(auto_tags, list):
        auto_tags = []
    sla = bundle.get("sla", {})
    if not isinstance(sla, dict):
        sla = {}
    return {
        "sentiment_confidence": sentiment_conf,
        "auto_tags": auto_tags,
        "sla": sla,
    }


def _extract_speaker_roles(bundle: dict) -> dict[str, dict[str, object]]:
    roles = bundle.get("speaker_roles", {})
    confidences = bundle.get("speaker_roles_confidence", {})
    results: dict[str, dict[str, object]] = {}
    if isinstance(roles, dict):
        for speaker_id, role in roles.items():
            conf_value = None
            if isinstance(confidences, dict):
                conf_value = confidences.get(speaker_id)
            try:
                conf_value = float(conf_value) if conf_value is not None else None
            except (TypeError, ValueError):
                conf_value = None
            results[str(speaker_id)] = {"role": str(role), "confidence": conf_value}
    return results


def _most_common(values: list[str]) -> str | None:
    if not values:
        return None
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=counts.get)


def _new_call_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")


def _parse_raw_bundle(raw_text: str) -> dict | None:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _render_transcript(
    transcript_data: dict | list | None, speaker_roles: dict[str, dict[str, object]]
) -> str | None:
    if not isinstance(transcript_data, dict):
        return None
    entries = transcript_data.get("entries")
    if not isinstance(entries, list):
        return None

    label_map = _build_label_map(entries, speaker_roles)
    lines: list[str] = []
    for entry in entries:
        speaker_id = str(entry.get("speaker_id", "speaker"))
        label = label_map.get(speaker_id, speaker_id)
        transcript = str(entry.get("transcript", "")).strip()
        if not transcript:
            continue
        start = _format_time(entry.get("start_time_seconds", 0))
        end = _format_time(entry.get("end_time_seconds", 0))
        lines.append(f"[{start} - {end}] {label}: {transcript}")
    return "\n".join(lines)


def _render_speaker_stats(
    transcript_data: dict | list | None, speaker_roles: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    if not isinstance(transcript_data, dict):
        return []
    stats = transcript_data.get("speaker_stats")
    if not isinstance(stats, dict):
        return []

    label_map = _build_label_map(
        [{"speaker_id": key} for key in stats.keys()], speaker_roles
    )
    rows = []
    for speaker_id, values in stats.items():
        if not isinstance(values, dict):
            continue
        rows.append(
            {
                "speaker": label_map.get(str(speaker_id), str(speaker_id)),
                "duration": values.get("duration", 0),
                "words": values.get("words", 0),
            }
        )
    return rows


def _build_label_map(
    entries: list[dict], speaker_roles: dict[str, dict[str, object]]
) -> dict[str, str]:
    speaker_ids = [str(entry.get("speaker_id", "speaker")) for entry in entries]
    base_labels: dict[str, str] = {}
    for speaker_id in speaker_ids:
        role_info = speaker_roles.get(speaker_id)
        if role_info and role_info.get("role"):
            role_label = str(role_info.get("role"))
            confidence = role_info.get("confidence")
            if confidence is not None:
                role_label = f"{role_label} ({confidence:.2f})"
            base_labels[speaker_id] = role_label
        else:
            base_labels[speaker_id] = speaker_id

    counts: dict[str, int] = {}
    label_map: dict[str, str] = {}
    for speaker_id in speaker_ids:
        label = base_labels.get(speaker_id, speaker_id)
        counts[label] = counts.get(label, 0) + 1
        if counts[label] > 1 and label in {"Agent", "Customer", "Other"}:
            label_map[speaker_id] = f"{label} #{counts[label]}"
        else:
            label_map[speaker_id] = label
    return label_map


def _format_time(value: object) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 0.0
    minutes = int(seconds // 60)
    seconds = int(seconds % 60)
    return f"{minutes:02d}:{seconds:02d}"


def _build_transcript_segments(
    transcript_data: dict | list | None, speaker_roles: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    if not isinstance(transcript_data, dict):
        return []
    entries = transcript_data.get("entries")
    if not isinstance(entries, list):
        return []
    label_map = _build_label_map(entries, speaker_roles)
    segments: list[dict[str, object]] = []
    for entry in entries:
        start = entry.get("start_time_seconds", 0)
        end = entry.get("end_time_seconds", 0)
        try:
            start_val = float(start)
        except (TypeError, ValueError):
            start_val = 0.0
        try:
            end_val = float(end)
        except (TypeError, ValueError):
            end_val = start_val
        segments.append(
            {
                "speaker": label_map.get(str(entry.get("speaker_id", "speaker")), "speaker"),
                "start": start_val,
                "end": end_val,
                "time": f"{_format_time(start_val)} - {_format_time(end_val)}",
                "text": str(entry.get("transcript", "")).strip(),
            }
        )
    return [segment for segment in segments if segment["text"]]


def _infer_roles_from_transcript(transcript_data: dict | list | None) -> dict[str, dict[str, object]]:
    if not isinstance(transcript_data, dict):
        return {}
    entries = transcript_data.get("entries")
    if not isinstance(entries, list):
        return {}

    agent_cues = [
        "thank you for calling",
        "how can i help",
        "how may i help",
        "i will",
        "i can help",
        "let me",
        "ticket",
        "reference number",
        "policy",
        "account number",
        "apologies",
        "sorry for the inconvenience",
        "our company",
    ]
    customer_cues = [
        "i need",
        "i want",
        "my issue",
        "my problem",
        "refund",
        "complaint",
        "not working",
        "charged",
        "why",
        "when will",
        "i was",
        "i paid",
    ]

    scores: dict[str, dict[str, float]] = {}
    for entry in entries:
        speaker_id = str(entry.get("speaker_id", "speaker"))
        text = str(entry.get("transcript", "")).lower()
        start = float(entry.get("start_time_seconds", 0))
        end = float(entry.get("end_time_seconds", start))
        duration = max(0.0, end - start)

        speaker_scores = scores.setdefault(
            speaker_id, {"agent": 0.0, "customer": 0.0, "duration": 0.0}
        )
        speaker_scores["duration"] += duration
        for cue in agent_cues:
            if cue in text:
                speaker_scores["agent"] += 1.0
        for cue in customer_cues:
            if cue in text:
                speaker_scores["customer"] += 1.0

    roles: dict[str, dict[str, object]] = {}
    for speaker_id, score in scores.items():
        agent_score = score["agent"]
        customer_score = score["customer"]
        diff = abs(agent_score - customer_score)
        total = agent_score + customer_score

        role = "Other"
        confidence = 0.0
        if total > 0:
            if agent_score > customer_score + 1:
                role = "Agent"
                confidence = diff / total
            elif customer_score > agent_score + 1:
                role = "Customer"
                confidence = diff / total
        elif score["duration"] > 0:
            confidence = 0.2

        roles[speaker_id] = {"role": role, "confidence": round(min(1.0, confidence), 2)}

    return roles
