from __future__ import annotations

import atexit
import base64
import binascii
import csv
import io
import json
import logging
import queue
import shutil
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.db import close_old_connections
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import redirect, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from app.config import settings
from app.models import Call, RealtimeCall, RealtimeEvent, SupervisorAlert
from app.realtime import event_bus
from app.services.live_audio import LiveAudioBufferService
from app.services.pipeline import CallAnalyticsPipeline
from app.services.sarvam_client import SarvamService


PROMPT_PACKS = [
    {"value": "general", "label": "General"},
    {"value": "sales", "label": "Sales"},
    {"value": "support", "label": "Support"},
    {"value": "collections", "label": "Collections"},
]

PAGE_SIZES = [10, 20, 50]


@dataclass
class RuntimeState:
    executor: ThreadPoolExecutor
    pipeline: CallAnalyticsPipeline


_runtime_lock = threading.Lock()
_runtime: RuntimeState | None = None
_live_audio_lock = threading.Lock()
_live_audio_service: LiveAudioBufferService | None = None
logger = logging.getLogger(__name__)


def _ensure_runtime() -> RuntimeState:
    global _runtime
    if _runtime is not None:
        return _runtime
    with _runtime_lock:
        if _runtime is not None:
            return _runtime
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        settings.outputs_dir.mkdir(parents=True, exist_ok=True)
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        _runtime = RuntimeState(
            executor=ThreadPoolExecutor(max_workers=settings.worker_concurrency),
            pipeline=CallAnalyticsPipeline(SarvamService(settings.sarvam_api_key)),
        )
    return _runtime


def _get_live_audio_service() -> LiveAudioBufferService:
    global _live_audio_service
    if _live_audio_service is not None:
        return _live_audio_service
    with _live_audio_lock:
        if _live_audio_service is not None:
            return _live_audio_service
        _live_audio_service = LiveAudioBufferService(
            base_dir=settings.realtime_audio_dir,
            window_seconds=settings.realtime_audio_window_seconds,
            max_chunk_bytes=settings.realtime_audio_max_chunk_bytes,
        )
    return _live_audio_service


@atexit.register
def _shutdown_runtime() -> None:
    global _runtime
    if _runtime is None:
        return
    _runtime.executor.shutdown(wait=False)
    _runtime = None


@require_GET
def dashboard(request):
    params = request.GET
    query = params.get("q", "").strip()
    status = params.get("status", "all").strip().lower()
    date_from = _parse_date(params.get("date_from"))
    date_to = _parse_date(params.get("date_to"))
    topic = params.get("topic", "").strip().lower()
    role = params.get("role", "").strip().title()
    page = _parse_int(params.get("page"), default=1)
    page_size = _parse_int(params.get("page_size"), default=10)
    if page_size not in PAGE_SIZES:
        page_size = 10

    calls = list(Call.objects.all().order_by("-created_at"))

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

    prev_url = _build_page_url(request, page - 1) if page > 1 else None
    next_url = _build_page_url(request, page + 1) if page < total_pages else None

    call_items = [_build_call_item(call) for call in page_calls]
    metrics = _build_metrics(filtered_calls)
    metrics["total_all"] = len(calls)

    chart_data = _build_chart_data(filtered_calls)
    insights = _build_insights_data(filtered_calls)

    return render(
        request,
        "dashboard.html",
        {
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
            "page_sizes": PAGE_SIZES,
            "pagination": {
                "page": page,
                "total_pages": total_pages,
                "total_filtered": total_filtered,
                "prev_url": prev_url,
                "next_url": next_url,
            },
        },
    )


@require_GET
def upload_page(request):
    defaults = {
        "language_code": settings.language_code,
        "stt_model": settings.sarvam_stt_model,
        "prompt_pack": settings.prompt_pack,
        "prompt_packs": PROMPT_PACKS,
    }
    return render(request, "upload.html", {"defaults": defaults})


@require_http_methods(["GET", "POST"])
def upload_entry(request):
    if request.method == "POST":
        return upload_call(request)
    return upload_page(request)


@require_POST
def upload_glossary(request):
    uploaded = request.FILES.get("file")
    if not uploaded:
        return HttpResponseBadRequest("Missing file")
    settings.glossary_path.parent.mkdir(parents=True, exist_ok=True)
    settings.glossary_path.write_bytes(uploaded.read())
    return redirect("/")


@require_POST
def upload_call(request):
    _ensure_runtime()

    uploaded = request.FILES.get("file")
    if not uploaded:
        return HttpResponseBadRequest("Missing file")

    language_code = request.POST.get("language_code", settings.language_code)
    stt_model = request.POST.get("stt_model", settings.sarvam_stt_model)
    with_diarization = _parse_bool(request.POST.get("with_diarization"), default=False)
    num_speakers = request.POST.get("num_speakers")
    prompt = request.POST.get("prompt")
    prompt_pack = request.POST.get("prompt_pack")
    glossary_terms = request.POST.get("glossary_terms")

    valid_packs = {pack["value"] for pack in PROMPT_PACKS}
    selected_pack = prompt_pack if prompt_pack in valid_packs else settings.prompt_pack
    call_id = _new_call_id()
    filename = uploaded.name or f"call_{call_id}.audio"
    storage_path = settings.uploads_dir / f"{call_id}_{filename}"

    with storage_path.open("wb") as buffer:
        for chunk in uploaded.chunks():
            buffer.write(chunk)

    parsed_num_speakers = None
    if num_speakers:
        try:
            parsed_num_speakers = int(num_speakers)
        except ValueError:
            parsed_num_speakers = None

    Call.objects.create(
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

    _enqueue_call(call_id)

    return redirect(f"/calls/{call_id}")


@require_GET
def call_detail(request, call_id: str):
    call = Call.objects.filter(pk=call_id).first()
    if not call:
        raise Http404("Call not found")

    transcript_data = _read_json(call.transcript_json_path)
    transcript_text = _read_text(call.transcript_text_path)
    analysis_bundle = _read_json(call.analysis_json_path) or {}
    if analysis_bundle.get("raw_text") and not analysis_bundle.get("summary"):
        parsed = _parse_raw_bundle(str(analysis_bundle.get("raw_text")))
        if parsed:
            analysis_bundle = parsed

    summary = _extract_summary(analysis_bundle, call.summary_json_path)
    qa_pairs = _normalize_qa_pairs(_extract_qa(analysis_bundle, call.qa_json_path))
    analysis_view = _extract_analysis_view(analysis_bundle)
    analysis_meta = _extract_analysis_meta(analysis_bundle)
    executive_summary = _build_executive_summary(analysis_view, summary)

    speaker_roles = _extract_speaker_roles(analysis_bundle)
    if not speaker_roles:
        speaker_roles = _infer_roles_from_transcript(transcript_data)
    transcript_segments = _build_transcript_segments(transcript_data, speaker_roles)
    transcript_render = _render_transcript(transcript_data, speaker_roles) or transcript_text
    speaker_stats = _render_speaker_stats(transcript_data, speaker_roles)
    call_experience = _build_call_experience(
        call=call,
        transcript_segments=transcript_segments,
        analysis_view=analysis_view,
        qa_pairs=qa_pairs,
    )
    logger.info(
        "call_detail_rendered call_id=%s status=%s segments=%s events=%s emotions=%s",
        call.id,
        call.status,
        len(transcript_segments),
        len(call_experience.get("events", [])),
        len(call_experience.get("player_emotions", [])),
    )

    return render(
        request,
        "call_detail.html",
        {
            "call": call,
            "transcript": transcript_data,
            "transcript_text": transcript_text,
            "analysis": analysis_view,
            "analysis_meta": analysis_meta,
            "summary": summary,
            "executive_summary": executive_summary,
            "qa_pairs": qa_pairs,
            "transcript_render": transcript_render,
            "speaker_stats": speaker_stats,
            "transcript_segments": transcript_segments,
            "call_experience": call_experience,
        },
    )


@require_GET
def call_audio(request, call_id: str):
    call = Call.objects.filter(pk=call_id).first()
    if not call or not call.storage_path:
        raise Http404("Audio not found")
    path = Path(call.storage_path)
    if not path.exists():
        raise Http404("Audio not found")
    return FileResponse(path.open("rb"), filename=path.name)


@require_POST
def bulk_actions(request):
    action = request.POST.get("action", "").strip().lower()
    call_ids = [call_id for call_id in request.POST.getlist("call_ids") if call_id]
    calls = list(Call.objects.filter(id__in=call_ids))

    if not calls:
        return HttpResponseBadRequest("No calls selected")

    if action in {"export_json", "export_csv", "export_transcript_csv"}:
        return _bulk_export(calls, action)

    if action == "reprocess":
        for call in calls:
            _reset_call_for_reprocess(call)
            _enqueue_call(call.id)
        return redirect("/")

    if action == "delete":
        for call in calls:
            _delete_call_assets(call)
            Call.objects.filter(pk=call.id).delete()
        return redirect("/")

    return HttpResponseBadRequest("Unsupported bulk action")


@require_GET
def export_call(request, call_id: str):
    call = Call.objects.filter(pk=call_id).first()
    if not call:
        raise Http404("Call not found")

    format_name = request.GET.get("format", "json").lower()
    scope = request.GET.get("scope", "insights").lower()

    transcript_data = _read_json(call.transcript_json_path) or {}
    analysis_bundle = _read_json(call.analysis_json_path) or {}
    if analysis_bundle.get("raw_text") and not analysis_bundle.get("summary"):
        parsed = _parse_raw_bundle(str(analysis_bundle.get("raw_text")))
        if parsed:
            analysis_bundle = parsed

    if format_name == "json":
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
        response = HttpResponse(
            json.dumps(payload, indent=2),
            content_type="application/json",
        )
        response["Content-Disposition"] = (
            f"attachment; filename=call_{call.id}_{scope}.json"
        )
        return response

    if format_name == "csv":
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
        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            f"attachment; filename=call_{call.id}_{scope}.csv"
        )
        return response

    return HttpResponseBadRequest("Unsupported export format")


@require_GET
def api_metrics(request):
    calls = list(Call.objects.all().order_by("-created_at"))
    chart_data = _build_chart_data(calls)
    return JsonResponse(chart_data)


@require_GET
def api_call(request, call_id: str):
    call = Call.objects.filter(pk=call_id).first()
    if not call:
        return JsonResponse({"detail": "Call not found"}, status=404)
    return JsonResponse(
        {
            "id": call.id,
            "status": call.status,
            "error_message": call.error_message,
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def api_realtime_events(request):
    if not _is_realtime_ingest_authorized(request):
        return JsonResponse({"detail": "Unauthorized ingest token"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"detail": "Invalid JSON body"}, status=400)

    result, error = _ingest_realtime_payload(payload)
    if error or not result:
        return JsonResponse({"detail": error or "Failed to ingest event"}, status=400)

    return JsonResponse(
        {
            "ok": True,
            "call_id": result["call_id"],
            "risk_score": result["risk_score"],
            "sentiment_score": result["sentiment_score"],
            "alerts": result["alerts"],
            "snapshot": result["snapshot"],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def api_realtime_audio_chunk(request):
    if not _is_realtime_ingest_authorized(request):
        return JsonResponse({"detail": "Unauthorized ingest token"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"detail": "Invalid JSON body"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"detail": "JSON payload must be an object"}, status=400)

    call_id = _extract_realtime_call_id(payload)
    if not call_id:
        return JsonResponse({"detail": "Missing call_id"}, status=400)

    decoded_audio, decode_error = _decode_realtime_audio_chunk(payload)
    if decode_error:
        return JsonResponse({"detail": decode_error}, status=400)

    try:
        live_audio_state = _get_live_audio_service().append_pcm_chunk(
            call_id=call_id,
            pcm_bytes=decoded_audio["pcm_bytes"],
            sample_rate=decoded_audio["sample_rate"],
            channels=decoded_audio["channels"],
            sample_width=decoded_audio["sample_width"],
            chunk_id=decoded_audio["chunk_id"],
            occurred_at=decoded_audio["occurred_at"],
        )
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    event_payloads = _build_realtime_events_from_audio_payload(
        payload=payload,
        call_id=call_id,
        live_audio_state=live_audio_state,
    )
    ingested_results: list[dict[str, object]] = []
    warnings: list[str] = []
    for event_payload in event_payloads:
        result, error = _ingest_realtime_payload(event_payload)
        if error or not result:
            warnings.append(error or "event_ingest_failed")
            continue
        ingested_results.append(result)

    if not ingested_results:
        return JsonResponse(
            {
                "detail": "No realtime events were ingested from audio payload",
                "audio": live_audio_state,
                "warnings": warnings,
            },
            status=400,
        )

    alert_map: dict[int, dict[str, object]] = {}
    latest_snapshot = ingested_results[-1]["snapshot"]
    for result in ingested_results:
        for alert in result["alerts"]:
            alert_id = int(alert.get("id") or 0)
            if alert_id:
                alert_map[alert_id] = alert

    return JsonResponse(
        {
            "ok": True,
            "call_id": call_id,
            "audio": live_audio_state,
            "ingested_events": len(ingested_results),
            "alerts": list(alert_map.values()),
            "snapshot": latest_snapshot,
            "warnings": warnings,
        }
    )


@require_GET
def api_realtime_call_audio(request, call_id: str):
    max_seconds = _parse_int(str(request.GET.get("max_seconds") or ""), default=0)
    wav_bytes = _get_live_audio_service().get_wav_bytes(
        call_id,
        max_seconds=max_seconds if max_seconds > 0 else None,
    )
    if wav_bytes:
        response = HttpResponse(wav_bytes, content_type="audio/wav")
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Content-Disposition"] = f'inline; filename="{call_id}_live.wav"'
        response["X-Live-Audio"] = "1"
        return response

    fallback_to_uploaded = _parse_bool(request.GET.get("fallback"), default=False)
    if fallback_to_uploaded:
        call = Call.objects.filter(pk=call_id).first()
        if call and call.storage_path:
            path = Path(call.storage_path)
            if path.exists():
                response = FileResponse(path.open("rb"), filename=path.name)
                response["X-Live-Audio"] = "0"
                return response

    raise Http404("Live audio not found")


@require_GET
def api_realtime_call_audio_meta(request, call_id: str):
    live_audio = _get_live_audio_service().get_state(call_id)
    fallback_audio_available = False
    call = Call.objects.filter(pk=call_id).first()
    if call and call.storage_path:
        fallback_audio_available = Path(call.storage_path).exists()

    return JsonResponse(
        {
            "call_id": call_id,
            "live_audio": live_audio,
            "fallback_audio_available": fallback_audio_available,
            "preferred_source": "live" if live_audio.get("available") else "fallback",
        }
    )


@require_GET
def api_realtime_call_snapshot(request, call_id: str):
    realtime_call = RealtimeCall.objects.filter(pk=call_id).first()
    if not realtime_call:
        return JsonResponse(
            {
                "call_id": call_id,
                "provider": "generic",
                "status": "idle",
                "risk_score": 0.0,
                "sentiment_score": 0.0,
                "events": [],
                "alerts": [],
                "live_audio": _get_live_audio_service().get_state(call_id),
            }
        )
    return JsonResponse(_serialize_realtime_snapshot(realtime_call))


@require_GET
def api_realtime_alerts(request):
    call_id = str(request.GET.get("call_id") or "").strip()
    open_only = str(request.GET.get("open_only") or "true").strip().lower() != "false"
    limit = _parse_int(str(request.GET.get("limit") or "50"), default=50)
    limit = max(1, min(limit, 200))

    query = SupervisorAlert.objects.select_related("realtime_call").order_by("-created_at")
    if call_id:
        query = query.filter(realtime_call__call_id=call_id)
    if open_only:
        query = query.filter(acknowledged=False)

    return JsonResponse(
        {
            "alerts": [_serialize_supervisor_alert(alert) for alert in query[:limit]],
        }
    )


@require_GET
def api_genesys_connector_health(request):
    stale_after = max(
        10,
        _parse_int(
            str(request.GET.get("stale_after") or ""),
            default=int(settings.genesys_connector_health_stale_seconds),
        ),
    )
    status_path = Path(settings.genesys_connector_status_path)
    if not status_path.exists():
        return JsonResponse(
            {
                "healthy": False,
                "state": "not_running",
                "reason": "status_file_missing",
                "status_path": str(status_path),
                "stale_after_seconds": stale_after,
            }
        )

    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return JsonResponse(
            {
                "healthy": False,
                "state": "unknown",
                "reason": "status_file_unreadable",
                "status_path": str(status_path),
                "stale_after_seconds": stale_after,
            },
            status=500,
        )

    state = str(payload.get("state") or "unknown").strip().lower()
    updated_at_raw = str(payload.get("updated_at") or "").strip()
    updated_at = _parse_realtime_datetime(updated_at_raw)
    age_seconds = max(0.0, (datetime.utcnow() - updated_at).total_seconds())

    running_states = {"running", "subscribed", "connecting", "reconnecting", "starting"}
    healthy = state in running_states and age_seconds <= stale_after and state != "error"

    return JsonResponse(
        {
            "healthy": healthy,
            "state": state,
            "age_seconds": round(age_seconds, 2),
            "stale_after_seconds": stale_after,
            "status_path": str(status_path),
            "status": payload,
        }
    )


@require_GET
def api_genesys_audiohook_health(request):
    stale_after = max(
        10,
        _parse_int(
            str(request.GET.get("stale_after") or ""),
            default=int(settings.genesys_audiohook_health_stale_seconds),
        ),
    )
    status_path = Path(settings.genesys_audiohook_status_path)
    if not status_path.exists():
        return JsonResponse(
            {
                "healthy": False,
                "state": "not_running",
                "reason": "status_file_missing",
                "status_path": str(status_path),
                "stale_after_seconds": stale_after,
            }
        )

    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return JsonResponse(
            {
                "healthy": False,
                "state": "unknown",
                "reason": "status_file_unreadable",
                "status_path": str(status_path),
                "stale_after_seconds": stale_after,
            },
            status=500,
        )

    state = str(payload.get("state") or "unknown").strip().lower()
    updated_at_raw = str(payload.get("updated_at") or "").strip()
    updated_at = _parse_realtime_datetime(updated_at_raw)
    age_seconds = max(0.0, (datetime.utcnow() - updated_at).total_seconds())

    running_states = {"running", "starting", "stopping"}
    healthy = state in running_states and age_seconds <= stale_after and state != "error"

    return JsonResponse(
        {
            "healthy": healthy,
            "state": state,
            "age_seconds": round(age_seconds, 2),
            "stale_after_seconds": stale_after,
            "status_path": str(status_path),
            "status": payload,
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def api_realtime_alert_ack(request, alert_id: int):
    alert = SupervisorAlert.objects.filter(pk=alert_id).first()
    if not alert:
        return JsonResponse({"detail": "Alert not found"}, status=404)

    if not alert.acknowledged:
        alert.acknowledged = True
        alert.acknowledged_at = datetime.utcnow()
        alert.save(update_fields=["acknowledged", "acknowledged_at"])
        event_bus.publish(
            {
                "type": "supervisor_alert_ack",
                "call_id": alert.realtime_call.call_id,
                "alert": _serialize_supervisor_alert(alert),
            }
        )
    return JsonResponse({"ok": True, "alert": _serialize_supervisor_alert(alert)})


@require_GET
def api_realtime_stream(request):
    call_filter = str(request.GET.get("call_id") or "").strip()
    subscriber_id, subscriber_queue = event_bus.subscribe()

    logger.info(
        "realtime_stream_connected subscriber=%s call_filter=%s",
        subscriber_id,
        call_filter or "all",
    )

    def stream():
        try:
            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "connected",
                        "call_id": call_filter or None,
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                )
                + "\n\n"
            )
            while True:
                try:
                    payload = subscriber_queue.get(timeout=15)
                except queue.Empty:
                    yield (
                        "event: ping\n"
                        "data: "
                        + json.dumps({"type": "ping", "timestamp": datetime.utcnow().isoformat()})
                        + "\n\n"
                    )
                    continue

                if call_filter:
                    try:
                        decoded = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if str(decoded.get("call_id") or "").strip() != call_filter:
                        continue

                yield f"data: {payload}\n\n"
        finally:
            event_bus.unsubscribe(subscriber_id)
            logger.info("realtime_stream_disconnected subscriber=%s", subscriber_id)

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _enqueue_call(call_id: str) -> None:
    runtime = _ensure_runtime()
    runtime.executor.submit(_process_call, call_id)


def _process_call(call_id: str) -> None:
    close_old_connections()

    call = Call.objects.filter(pk=call_id).first()
    if not call:
        close_old_connections()
        return

    logger.info("call_processing_started call_id=%s status=%s", call_id, call.status)

    call_data: dict[str, object] = {
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
    call.save(update_fields=["status", "updated_at"])

    _emit_ws_event(
        {
            "type": "call_update",
            "call_id": call_id,
            "status": "processing",
            "progress": 10,
            "stage": "processing",
        }
    )

    pipeline = _ensure_runtime().pipeline
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
        logger.exception("call_processing_failed call_id=%s error=%s", call_id, exc)
        failed_call = Call.objects.filter(pk=call_id).first()
        if failed_call:
            failed_call.status = "failed"
            failed_call.updated_at = datetime.utcnow()
            failed_call.error_message = str(exc)
            failed_call.save(update_fields=["status", "updated_at", "error_message"])
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
        close_old_connections()
        return

    completed_call = Call.objects.filter(pk=call_id).first()
    if not completed_call:
        close_old_connections()
        return
    completed_call.status = "completed"
    completed_call.updated_at = datetime.utcnow()
    completed_call.duration_seconds = output.duration_seconds
    completed_call.transcript_text_path = str(output.transcript_text_path)
    completed_call.transcript_json_path = str(output.transcript_json_path)
    completed_call.analysis_json_path = str(output.analysis_json_path)
    completed_call.qa_json_path = str(output.qa_json_path)
    completed_call.summary_json_path = str(output.summary_json_path)
    completed_call.raw_llm_path = str(output.raw_llm_path)
    completed_call.save(
        update_fields=[
            "status",
            "updated_at",
            "duration_seconds",
            "transcript_text_path",
            "transcript_json_path",
            "analysis_json_path",
            "qa_json_path",
            "summary_json_path",
            "raw_llm_path",
        ]
    )

    logger.info(
        "call_processing_completed call_id=%s duration_seconds=%s",
        call_id,
        output.duration_seconds,
    )

    _emit_ws_event(
        {
            "type": "call_update",
            "call_id": call_id,
            "status": "completed",
            "progress": 100,
            "stage": "completed",
        }
    )
    close_old_connections()


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


def _bulk_export(calls: list[Call], action: str) -> HttpResponse:
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
        response = HttpResponse(
            json.dumps(payload, indent=2),
            content_type="application/json",
        )
        response["Content-Disposition"] = "attachment; filename=calls_export.json"
        return response

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
    response = HttpResponse(output.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = "attachment; filename=calls_export.csv"
    return response


def _reset_call_for_reprocess(call: Call) -> None:
    call_in_db = Call.objects.filter(pk=call.id).first()
    if not call_in_db:
        return
    _delete_call_assets(call_in_db, keep_upload=True)
    call_in_db.status = "queued"
    call_in_db.error_message = None
    call_in_db.updated_at = datetime.utcnow()
    call_in_db.save(update_fields=["status", "error_message", "updated_at"])


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
        event_bus.publish(payload)
    except Exception:
        return


def _is_realtime_ingest_authorized(request: HttpRequest) -> bool:
    expected_token = settings.realtime_ingest_token.strip()
    if not expected_token:
        return True

    header_token = str(request.headers.get("X-Cloud-Token") or "").strip()
    if header_token and header_token == expected_token:
        return True

    authorization = str(request.headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        bearer_token = authorization[7:].strip()
        if bearer_token == expected_token:
            return True

    return False


def _extract_realtime_call_id(payload: dict[str, object]) -> str:
    return str(
        payload.get("call_id")
        or payload.get("conversation_id")
        or payload.get("session_id")
        or ""
    ).strip()


def _decode_realtime_audio_chunk(
    payload: dict[str, object],
) -> tuple[dict[str, object] | None, str | None]:
    chunk_b64 = str(
        payload.get("audio_b64")
        or payload.get("chunk_b64")
        or payload.get("audio_chunk_b64")
        or payload.get("audio_chunk")
        or ""
    ).strip()
    if not chunk_b64:
        return None, "Missing audio chunk base64 (audio_b64)"

    try:
        raw_bytes = base64.b64decode(chunk_b64, validate=False)
    except (ValueError, binascii.Error):
        return None, "Invalid base64 audio payload"
    if not raw_bytes:
        return None, "Empty decoded audio payload"

    encoding = str(payload.get("audio_encoding") or payload.get("encoding") or "pcm_s16le").strip().lower()
    occurred_at = _parse_realtime_datetime(payload.get("timestamp") or payload.get("occurred_at"))
    chunk_id = str(payload.get("chunk_id") or payload.get("sequence_id") or "").strip() or None

    sample_rate = int(
        _parse_optional_float(payload.get("sample_rate")) or settings.realtime_audio_default_sample_rate
    )
    channels = int(
        _parse_optional_float(payload.get("channels")) or settings.realtime_audio_default_channels
    )
    sample_width = 2
    pcm_bytes = raw_bytes

    if encoding in {"wav", "wave", "audio/wav", "audio/x-wav"}:
        try:
            with wave.open(io.BytesIO(raw_bytes), "rb") as wav_file:
                sample_rate = int(wav_file.getframerate())
                channels = int(wav_file.getnchannels())
                sample_width = int(wav_file.getsampwidth())
                pcm_bytes = wav_file.readframes(wav_file.getnframes())
        except wave.Error:
            return None, "Unable to parse WAV audio chunk"
        if sample_width != 2:
            return None, "WAV chunk must use 16-bit PCM (sample_width=2)"
    elif encoding not in {"pcm_s16le", "pcm16", "s16le", "linear16", "l16"}:
        return None, f"Unsupported audio_encoding: {encoding}"

    if sample_rate <= 0:
        return None, "Invalid sample_rate"
    if channels <= 0:
        return None, "Invalid channels"
    if sample_width <= 0:
        return None, "Invalid sample_width"
    if not pcm_bytes:
        return None, "Audio payload has no PCM frames"

    return (
        {
            "pcm_bytes": pcm_bytes,
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
            "occurred_at": occurred_at,
            "chunk_id": chunk_id,
        },
        None,
    )


def _build_realtime_events_from_audio_payload(
    payload: dict[str, object],
    call_id: str,
    live_audio_state: dict[str, object],
) -> list[dict[str, object]]:
    provider = str(payload.get("provider") or "generic").strip() or "generic"
    status = str(payload.get("status") or "active").strip().lower() or "active"
    agent_id = str(payload.get("agent_id") or "").strip()
    customer_id = str(payload.get("customer_id") or "").strip()
    fallback_speaker = str(payload.get("speaker") or "").strip().lower()
    fallback_timestamp = payload.get("timestamp") or payload.get("occurred_at")
    base_metadata = dict(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {})
    base_metadata["audio"] = live_audio_state

    event_payloads: list[dict[str, object]] = []
    segments = payload.get("transcript_segments")
    if not isinstance(segments, list):
        maybe_segments = payload.get("segments")
        segments = maybe_segments if isinstance(maybe_segments, list) else []

    for segment in segments[:50]:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or segment.get("transcript") or "").strip()
        if not text:
            continue
        segment_metadata = dict(segment.get("metadata") if isinstance(segment.get("metadata"), dict) else {})
        merged_metadata = dict(base_metadata)
        merged_metadata.update(segment_metadata)
        event_payloads.append(
            {
                "provider": provider,
                "call_id": call_id,
                "event_type": str(segment.get("event_type") or "transcript").strip().lower() or "transcript",
                "speaker": str(segment.get("speaker") or fallback_speaker).strip().lower(),
                "text": text,
                "sentiment": _parse_optional_float(segment.get("sentiment")),
                "confidence": _parse_optional_float(segment.get("confidence")),
                "status": str(segment.get("status") or status).strip().lower() or status,
                "timestamp": segment.get("timestamp") or segment.get("occurred_at") or fallback_timestamp,
                "agent_id": str(segment.get("agent_id") or agent_id).strip(),
                "customer_id": str(segment.get("customer_id") or customer_id).strip(),
                "metadata": merged_metadata,
            }
        )

    if event_payloads:
        return event_payloads

    text = str(payload.get("text") or payload.get("transcript") or "").strip()
    if text:
        return [
            {
                "provider": provider,
                "call_id": call_id,
                "event_type": "transcript",
                "speaker": fallback_speaker,
                "text": text,
                "sentiment": _parse_optional_float(payload.get("sentiment")),
                "confidence": _parse_optional_float(payload.get("confidence")),
                "status": status,
                "timestamp": fallback_timestamp,
                "agent_id": agent_id,
                "customer_id": customer_id,
                "metadata": base_metadata,
            }
        ]

    # Keep pipeline moving even when only audio arrives.
    return [
        {
            "provider": provider,
            "call_id": call_id,
            "event_type": "audio_chunk",
            "speaker": fallback_speaker,
            "text": "",
            "sentiment": _parse_optional_float(payload.get("sentiment")),
            "confidence": _parse_optional_float(payload.get("confidence")),
            "status": status,
            "timestamp": fallback_timestamp,
            "agent_id": agent_id,
            "customer_id": customer_id,
            "metadata": base_metadata,
        }
    ]


def _ingest_realtime_payload(
    payload: object,
) -> tuple[dict[str, object] | None, str | None]:
    normalized, error = _normalize_realtime_payload(payload)
    if error:
        return None, error

    realtime_call = _upsert_realtime_call_state(normalized)
    event = RealtimeEvent.objects.create(
        realtime_call=realtime_call,
        occurred_at=normalized["occurred_at"],
        event_type=normalized["event_type"],
        speaker=normalized["speaker"] or None,
        text=normalized["text"],
        sentiment=normalized["sentiment"],
        confidence=normalized["confidence"],
        metadata=normalized["metadata"],
    )
    alerts = _evaluate_supervisor_alerts(realtime_call, event)
    snapshot = _serialize_realtime_snapshot(realtime_call)

    realtime_event_payload = {
        "type": "realtime_event",
        "call_id": realtime_call.call_id,
        "provider": realtime_call.provider,
        "status": realtime_call.status,
        "event": _serialize_realtime_event(event),
        "risk_score": realtime_call.risk_score,
        "sentiment_score": realtime_call.sentiment_score,
    }
    event_bus.publish(realtime_event_payload)

    serialized_alerts = [_serialize_supervisor_alert(alert) for alert in alerts]
    for serialized_alert in serialized_alerts:
        event_bus.publish(
            {
                "type": "supervisor_alert",
                "call_id": realtime_call.call_id,
                "provider": realtime_call.provider,
                "risk_score": realtime_call.risk_score,
                "alert": serialized_alert,
            }
        )

    logger.info(
        "realtime_event_ingested call_id=%s event_type=%s alerts=%s risk_score=%.2f",
        realtime_call.call_id,
        event.event_type,
        len(alerts),
        realtime_call.risk_score,
    )

    return (
        {
            "call_id": realtime_call.call_id,
            "risk_score": realtime_call.risk_score,
            "sentiment_score": realtime_call.sentiment_score,
            "alerts": serialized_alerts,
            "snapshot": snapshot,
            "event": _serialize_realtime_event(event),
        },
        None,
    )


def _normalize_realtime_payload(payload: object) -> tuple[dict[str, object] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "JSON payload must be an object"

    call_id = _extract_realtime_call_id(payload)
    if not call_id:
        return None, "Missing call_id"

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        metadata["metrics"] = metrics

    normalized: dict[str, object] = {
        "call_id": call_id,
        "provider": str(payload.get("provider") or "generic").strip() or "generic",
        "event_type": str(payload.get("event_type") or "transcript").strip().lower() or "transcript",
        "speaker": str(payload.get("speaker") or "").strip().lower(),
        "text": str(payload.get("text") or payload.get("transcript") or "").strip(),
        "sentiment": _parse_optional_float(payload.get("sentiment")),
        "confidence": _parse_optional_float(payload.get("confidence")),
        "status": str(payload.get("status") or "").strip().lower(),
        "agent_id": str(payload.get("agent_id") or "").strip(),
        "customer_id": str(payload.get("customer_id") or "").strip(),
        "occurred_at": _parse_realtime_datetime(payload.get("timestamp") or payload.get("occurred_at")),
        "metadata": metadata,
    }
    return normalized, None


def _parse_optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_realtime_datetime(value: object) -> datetime:
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(value))
        except (TypeError, ValueError, OSError):
            return datetime.utcnow()

    if isinstance(value, str) and value.strip():
        parsed = parse_datetime(value.strip().replace("Z", "+00:00"))
        if parsed is not None:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed

    return datetime.utcnow()


def _upsert_realtime_call_state(normalized: dict[str, object]) -> RealtimeCall:
    call_id = str(normalized.get("call_id") or "")
    now = datetime.utcnow()
    defaults = {
        "provider": str(normalized.get("provider") or "generic"),
        "status": str(normalized.get("status") or "active") or "active",
        "created_at": now,
        "updated_at": now,
        "agent_id": str(normalized.get("agent_id") or "") or None,
        "customer_id": str(normalized.get("customer_id") or "") or None,
        "last_speaker": str(normalized.get("speaker") or "") or None,
        "last_text": str(normalized.get("text") or ""),
        "sentiment_score": float(normalized.get("sentiment") or 0.0),
        "metadata": dict(normalized.get("metadata") or {}),
    }
    realtime_call, created = RealtimeCall.objects.get_or_create(
        call_id=call_id,
        defaults=defaults,
    )

    if created:
        return realtime_call

    realtime_call.provider = str(normalized.get("provider") or realtime_call.provider or "generic")
    status = str(normalized.get("status") or "").strip().lower()
    if status:
        realtime_call.status = status
    realtime_call.updated_at = now

    agent_id = str(normalized.get("agent_id") or "").strip()
    customer_id = str(normalized.get("customer_id") or "").strip()
    speaker = str(normalized.get("speaker") or "").strip().lower()
    text = str(normalized.get("text") or "").strip()
    sentiment = normalized.get("sentiment")

    if agent_id:
        realtime_call.agent_id = agent_id
    if customer_id:
        realtime_call.customer_id = customer_id
    if speaker:
        realtime_call.last_speaker = speaker
    if text:
        realtime_call.last_text = text[:2400]
    if isinstance(sentiment, (int, float)):
        previous = float(realtime_call.sentiment_score or 0.0)
        realtime_call.sentiment_score = round((previous * 0.72) + (float(sentiment) * 0.28), 3)

    merged_metadata = dict(realtime_call.metadata or {})
    merged_metadata.update(dict(normalized.get("metadata") or {}))
    realtime_call.metadata = merged_metadata
    realtime_call.save(
        update_fields=[
            "provider",
            "status",
            "updated_at",
            "agent_id",
            "customer_id",
            "last_speaker",
            "last_text",
            "sentiment_score",
            "metadata",
        ]
    )
    return realtime_call


def _evaluate_supervisor_alerts(
    realtime_call: RealtimeCall,
    event: RealtimeEvent,
) -> list[SupervisorAlert]:
    alerts: list[SupervisorAlert] = []
    text = str(event.text or "").lower()
    sentiment = event.sentiment
    threshold = settings.realtime_negative_sentiment_threshold
    keyword_hits = [term for term in _supervisor_keyword_triggers() if term in text]
    dead_air_seconds = _extract_dead_air_seconds(event.metadata)

    if sentiment is not None and sentiment <= threshold:
        severity = "high" if sentiment <= threshold - 0.2 else "medium"
        message = f"Negative sentiment detected ({sentiment:.2f}) in live call."
        alert = _create_supervisor_alert(
            realtime_call=realtime_call,
            alert_type="negative_sentiment",
            severity=severity,
            message=message,
            metadata={
                "sentiment": sentiment,
                "threshold": threshold,
                "event_id": event.id,
            },
        )
        if alert:
            alerts.append(alert)

    if keyword_hits:
        severity = "high" if any(term in {"supervisor", "lawyer", "legal"} for term in keyword_hits) else "medium"
        message = "Escalation keywords detected: " + ", ".join(keyword_hits[:4])
        alert = _create_supervisor_alert(
            realtime_call=realtime_call,
            alert_type="escalation_keyword",
            severity=severity,
            message=message,
            metadata={
                "keywords": keyword_hits,
                "event_id": event.id,
            },
        )
        if alert:
            alerts.append(alert)

    if dead_air_seconds is not None and dead_air_seconds >= 20:
        severity = "high" if dead_air_seconds >= 35 else "medium"
        message = f"Extended dead air detected ({dead_air_seconds:.1f}s)."
        alert = _create_supervisor_alert(
            realtime_call=realtime_call,
            alert_type="dead_air",
            severity=severity,
            message=message,
            metadata={
                "dead_air_seconds": dead_air_seconds,
                "event_id": event.id,
            },
        )
        if alert:
            alerts.append(alert)

    _update_realtime_risk_score(
        realtime_call=realtime_call,
        sentiment=sentiment,
        keyword_hit=bool(keyword_hits),
        dead_air_seconds=dead_air_seconds,
        severity_hits=[alert.severity for alert in alerts],
    )

    if (
        realtime_call.risk_score >= settings.realtime_high_risk_threshold
        and _can_emit_alert(realtime_call, "high_risk_score")
    ):
        high_risk_alert = SupervisorAlert.objects.create(
            realtime_call=realtime_call,
            alert_type="high_risk_score",
            severity="critical",
            message=f"Live risk score crossed threshold ({realtime_call.risk_score:.2f}).",
            metadata={
                "risk_score": realtime_call.risk_score,
                "threshold": settings.realtime_high_risk_threshold,
                "event_id": event.id,
            },
        )
        alerts.append(high_risk_alert)

    return alerts


def _create_supervisor_alert(
    realtime_call: RealtimeCall,
    alert_type: str,
    severity: str,
    message: str,
    metadata: dict[str, object] | None = None,
) -> SupervisorAlert | None:
    if not _can_emit_alert(realtime_call, alert_type):
        return None
    return SupervisorAlert.objects.create(
        realtime_call=realtime_call,
        alert_type=alert_type,
        severity=severity,
        message=message,
        metadata=metadata or {},
    )


def _can_emit_alert(realtime_call: RealtimeCall, alert_type: str) -> bool:
    cooldown_seconds = max(5, int(settings.realtime_alert_cooldown_seconds))
    cutoff = datetime.utcnow() - timedelta(seconds=cooldown_seconds)
    recent = SupervisorAlert.objects.filter(
        realtime_call=realtime_call,
        alert_type=alert_type,
        created_at__gte=cutoff,
    ).exists()
    return not recent


def _supervisor_keyword_triggers() -> list[str]:
    raw = settings.realtime_supervisor_keyword_triggers
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _extract_dead_air_seconds(metadata: object) -> float | None:
    if not isinstance(metadata, dict):
        return None
    metrics = metadata.get("metrics")
    candidate_sources = [metadata]
    if isinstance(metrics, dict):
        candidate_sources.append(metrics)
    for source in candidate_sources:
        for key in ("dead_air_seconds", "silence_seconds", "silence_duration"):
            if key in source:
                value = _parse_optional_float(source.get(key))
                if value is not None:
                    return max(0.0, value)
    return None


def _update_realtime_risk_score(
    realtime_call: RealtimeCall,
    sentiment: float | None,
    keyword_hit: bool,
    dead_air_seconds: float | None,
    severity_hits: list[str],
) -> None:
    score = float(realtime_call.risk_score or 0.0) * 0.88

    if sentiment is not None and sentiment < 0:
        score += min(0.46, abs(float(sentiment)) * 0.42)
    if keyword_hit:
        score += 0.24
    if dead_air_seconds is not None:
        score += min(0.25, max(0.0, dead_air_seconds - 10) / 100)
    if "high" in severity_hits:
        score += 0.16
    if "critical" in severity_hits:
        score += 0.2
    if realtime_call.status in {"ended", "completed", "closed"}:
        score *= 0.6

    realtime_call.risk_score = round(max(0.0, min(1.0, score)), 2)
    realtime_call.updated_at = datetime.utcnow()
    realtime_call.save(update_fields=["risk_score", "updated_at"])


def _serialize_realtime_event(event: RealtimeEvent) -> dict[str, object]:
    return {
        "id": event.id,
        "type": event.event_type,
        "speaker": event.speaker or "",
        "text": event.text,
        "sentiment": event.sentiment,
        "confidence": event.confidence,
        "occurred_at": event.occurred_at.isoformat(),
        "metadata": event.metadata or {},
    }


def _serialize_supervisor_alert(alert: SupervisorAlert) -> dict[str, object]:
    return {
        "id": alert.id,
        "call_id": alert.realtime_call.call_id,
        "type": alert.alert_type,
        "severity": alert.severity,
        "message": alert.message,
        "acknowledged": alert.acknowledged,
        "acknowledged_at": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
        "created_at": alert.created_at.isoformat(),
        "metadata": alert.metadata or {},
    }


def _serialize_realtime_snapshot(realtime_call: RealtimeCall) -> dict[str, object]:
    events = list(
        RealtimeEvent.objects.filter(realtime_call=realtime_call)
        .order_by("-occurred_at")[:40]
    )
    alerts = list(
        SupervisorAlert.objects.filter(realtime_call=realtime_call)
        .order_by("-created_at")[:30]
    )
    events.reverse()

    return {
        "call_id": realtime_call.call_id,
        "provider": realtime_call.provider,
        "status": realtime_call.status,
        "risk_score": realtime_call.risk_score,
        "sentiment_score": realtime_call.sentiment_score,
        "updated_at": realtime_call.updated_at.isoformat(),
        "events": [_serialize_realtime_event(event) for event in events],
        "alerts": [_serialize_supervisor_alert(alert) for alert in alerts],
        "live_audio": _get_live_audio_service().get_state(realtime_call.call_id),
    }


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


def _build_page_url(request, page: int) -> str:
    query = request.GET.copy()
    query["page"] = str(page)
    encoded = query.urlencode()
    if encoded:
        return f"{request.path}?{encoded}"
    return request.path


def _parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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


def _normalize_qa_pairs(items: list | object) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            question = str(item.get("question") or "")
            answer = str(item.get("answer") or "")
        else:
            question = ""
            answer = str(item)
        normalized.append({"question": question, "answer": answer})
    return normalized


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


def _build_executive_summary(analysis_view: dict[str, object], raw_summary: dict[str, object]) -> dict[str, object]:
    topics_raw = analysis_view.get("topics", [])
    action_raw = analysis_view.get("action_items", [])
    sentiment = analysis_view.get("sentiment", {}) if isinstance(analysis_view.get("sentiment"), dict) else {}

    topics = [str(item).strip() for item in topics_raw if str(item).strip()] if isinstance(topics_raw, list) else []
    actions = [str(item).strip() for item in action_raw if str(item).strip()] if isinstance(action_raw, list) else []

    overall = str(sentiment.get("overall") or "Unspecified").strip()
    resolution = str(analysis_view.get("resolution_status") or "Open").strip()
    primary_topics = topics[:3]

    short = (
        f"High-level summary: {overall} sentiment, {resolution} resolution status, "
        f"{len(topics)} key topic(s), and {len(actions)} action item(s)."
    )
    bullets: list[str] = []
    if primary_topics:
        bullets.append(f"Primary topics: {', '.join(primary_topics)}.")
    if actions:
        bullets.append(f"Action planning is in progress with {len(actions)} tracked follow-up task(s).")
    else:
        bullets.append("No explicit follow-up actions were identified by the model.")

    if not short and str(raw_summary.get("short") or "").strip():
        short = "High-level summary available; details are intentionally withheld."

    return {"short": short, "bullets": bullets}


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


def _build_call_experience(
    call: Call,
    transcript_segments: list[dict[str, object]],
    analysis_view: dict[str, object],
    qa_pairs: list[dict[str, str]],
) -> dict[str, object]:
    topics_raw = analysis_view.get("topics", [])
    topics = [str(topic) for topic in topics_raw if topic] if isinstance(topics_raw, list) else []

    duration_seconds = _resolve_call_duration(call, transcript_segments)
    start_dt = call.created_at
    end_dt = (
        start_dt + timedelta(seconds=duration_seconds)
        if duration_seconds > 0
        else None
    )

    events = _build_timeline_events(transcript_segments, topics)
    if not events:
        events = _events_from_qa_pairs(qa_pairs, duration_seconds)
    ai_insights = _build_ai_insights(analysis_view, qa_pairs, events)
    player_emotions = _build_player_emotions(ai_insights.get("events", []), duration_seconds)
    logger.debug(
        "call_experience_built call_id=%s duration=%.2f transcript_segments=%s events=%s emotions=%s",
        call.id,
        duration_seconds,
        len(transcript_segments),
        len(ai_insights.get("events", [])),
        len(player_emotions),
    )

    return {
        "recording_start_iso": start_dt.isoformat() if start_dt else None,
        "recording_start_display": (
            start_dt.strftime("%a, %B %d, %Y at %I:%M:%S %p")
            if start_dt
            else "--"
        ),
        "recording_end_iso": end_dt.isoformat() if end_dt else None,
        "recording_end_display": (
            end_dt.strftime("%a, %B %d, %Y at %I:%M:%S %p")
            if end_dt
            else "--"
        ),
        "duration_seconds": round(duration_seconds, 2),
        "transcript_segments": transcript_segments,
        "events": ai_insights.get("events", []),
        "ai_insights": ai_insights,
        "player_emotions": player_emotions,
    }


def _resolve_call_duration(call: Call, transcript_segments: list[dict[str, object]]) -> float:
    if call.duration_seconds is not None:
        try:
            return max(0.0, float(call.duration_seconds))
        except (TypeError, ValueError):
            pass

    max_end = 0.0
    for segment in transcript_segments:
        try:
            end_val = float(segment.get("end", 0) or 0)
        except (TypeError, ValueError):
            end_val = 0.0
        if end_val > max_end:
            max_end = end_val
    return max_end


def _build_timeline_events(
    transcript_segments: list[dict[str, object]],
    topics: list[str],
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    max_events = 120

    for index, segment in enumerate(transcript_segments[:max_events], start=1):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = _to_float(segment.get("start"))
        tone = _infer_event_tone(text)
        topic, evidence = _match_event_topic_with_evidence(text, topics)
        confidence = _infer_event_confidence(text, topic, tone, evidence)
        events.append(
            {
                "index": index,
                "start": start,
                "time": _format_time(start),
                "topic": topic,
                "title": topic,
                "tone": tone,
                "speaker": str(segment.get("speaker", "")),
                "excerpt": text[:180],
                "confidence": confidence,
                "evidence": evidence,
                "kind": "transcript",
            }
        )

    return events


def _events_from_qa_pairs(
    qa_pairs: list[dict[str, str]],
    duration_seconds: float = 0.0,
) -> list[dict[str, object]]:
    fallback: list[dict[str, object]] = []
    step = duration_seconds / (len(qa_pairs) + 1) if duration_seconds > 0 and qa_pairs else 0.0
    for index, pair in enumerate(qa_pairs[:40], start=1):
        question = str(pair.get("question") or "").strip()
        answer = str(pair.get("answer") or "").strip()
        text = f"{question} {answer}".strip()
        if not text:
            continue
        start = step * index if step > 0 else 0.0
        tone = _infer_event_tone(text)
        fallback.append(
            {
                "index": index,
                "start": round(start, 2),
                "time": _format_time(start),
                "topic": "Q&A",
                "title": "Q&A",
                "tone": tone,
                "speaker": "",
                "excerpt": text[:180],
                "confidence": _infer_event_confidence(text, "Q&A", tone, "qa_fallback"),
                "evidence": "qa_fallback",
                "kind": "qa",
            }
        )
    return fallback


def _build_ai_insights(
    analysis_view: dict[str, object],
    qa_pairs: list[dict[str, str]],
    events: list[dict[str, object]],
) -> dict[str, object]:
    topics_raw = analysis_view.get("topics", [])
    actions_raw = analysis_view.get("action_items", [])

    normalized_topics = (
        [str(topic).strip() for topic in topics_raw if str(topic).strip()]
        if isinstance(topics_raw, list)
        else []
    )
    normalized_actions = (
        [str(item).strip() for item in actions_raw if str(item).strip()]
        if isinstance(actions_raw, list)
        else []
    )

    topic_counts: dict[str, int] = {}
    for topic in normalized_topics:
        topic_counts[topic] = topic_counts.get(topic, 0) + 1

    normalized_events: list[dict[str, object]] = []
    for index, event in enumerate(events[:120], start=1):
        if not isinstance(event, dict):
            continue
        topic = str(event.get("topic") or event.get("title") or "General").strip()
        excerpt = str(event.get("excerpt") or "").strip()
        speaker = str(event.get("speaker") or "").strip()
        tone = str(event.get("tone") or "positive").lower()
        start = _to_float(event.get("start"))
        evidence = str(event.get("evidence") or "context").strip()
        confidence = _to_float(event.get("confidence"))
        if confidence <= 0:
            confidence = _infer_event_confidence(excerpt, topic, tone, evidence)

        topic_counts[topic] = topic_counts.get(topic, 0) + 1
        normalized_events.append(
            {
                "index": index,
                "start": round(start, 2),
                "time": _format_time(start),
                "topic": topic,
                "title": topic,
                "tone": tone,
                "speaker": speaker,
                "excerpt": excerpt[:220],
                "confidence": round(min(0.99, max(0.0, confidence)), 2),
                "evidence": evidence,
                "kind": str(event.get("kind") or "event"),
            }
        )

    topic_payload: list[dict[str, object]] = []
    ordered_topics = normalized_topics or [
        topic for topic in topic_counts.keys() if topic and topic.lower() != "general"
    ]
    for topic in ordered_topics[:16]:
        topic_payload.append({"name": topic, "count": topic_counts.get(topic, 0)})
    if not topic_payload and topic_counts:
        for topic, count in list(topic_counts.items())[:10]:
            topic_payload.append({"name": topic, "count": count})

    qa_payload: list[dict[str, str]] = []
    for pair in qa_pairs[:40]:
        question = str(pair.get("question") or "").strip()
        answer = str(pair.get("answer") or "").strip()
        if not question and not answer:
            continue
        qa_payload.append({"question": question, "answer": answer})

    if not qa_payload:
        for event in normalized_events:
            excerpt = str(event.get("excerpt") or "").strip()
            if "?" not in excerpt:
                continue
            question_head, _, answer_tail = excerpt.partition("?")
            question = f"{question_head.strip()}?".strip()
            if not question:
                continue
            answer = answer_tail.strip() or "Response captured in transcript events."
            qa_payload.append({"question": question[:140], "answer": answer[:220]})
            if len(qa_payload) >= 8:
                break

    if not normalized_actions:
        action_fallback: list[str] = []
        for event in normalized_events:
            topic = str(event.get("topic") or "conversation").strip()
            tone = str(event.get("tone") or "positive").lower()
            if tone in {"negative", "unhelpful"}:
                action_fallback.append(f"Follow up on {topic.lower()} and confirm a clear resolution.")
            elif tone == "empathetic":
                action_fallback.append(f"Continue empathetic handling for {topic.lower()}.")
            if len(action_fallback) >= 8:
                break
        if action_fallback:
            deduped = list(dict.fromkeys(action_fallback))
            normalized_actions = deduped
        elif normalized_events:
            normalized_actions = [
                "Review the conversation timeline and confirm closure with the customer."
            ]

    return {
        "topics": topic_payload,
        "actions": normalized_actions[:20],
        "qa": qa_payload,
        "events": normalized_events,
    }


def _build_player_emotions(
    events: list[dict[str, object]],
    duration_seconds: float,
) -> list[dict[str, object]]:
    if not events:
        return []

    tone_to_emoji = {
        "positive": "🙂",
        "negative": "😟",
        "empathetic": "🤝",
        "unhelpful": "🙅",
    }

    min_spacing = max(3.0, duration_seconds / 14) if duration_seconds > 0 else 3.5
    markers: list[dict[str, object]] = []
    last_time = -9999.0
    for event in events:
        if not isinstance(event, dict):
            continue
        start = _to_float(event.get("start"))
        tone = str(event.get("tone") or "positive").lower()
        emoji = tone_to_emoji.get(tone, "🙂")
        label = str(event.get("topic") or event.get("title") or "Conversation")
        if markers and start - last_time < min_spacing:
            continue
        markers.append(
            {
                "time": round(start, 2),
                "tone": tone,
                "emoji": emoji,
                "label": label,
            }
        )
        last_time = start
        if len(markers) >= 18:
            break

    return markers


def _to_float(value: object) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _match_event_topic_with_evidence(text: str, topics: list[str]) -> tuple[str, str]:
    lower_text = text.lower()
    for topic in topics:
        normalized = topic.lower().strip()
        if not normalized:
            continue
        if normalized in lower_text:
            return topic, f"topic_match:{normalized}"
        first_token = normalized.split()[0]
        if first_token and first_token in lower_text:
            return topic, f"topic_token:{first_token}"

    if "refund" in lower_text or "credit" in lower_text:
        return "Credits or Refunds", "keyword:refund_credit"
    if "policy" in lower_text:
        return "Policy Clarification", "keyword:policy"
    if "schedule" in lower_text or "callback" in lower_text:
        return "Follow-up", "keyword:schedule_callback"
    if "billing" in lower_text or "invoice" in lower_text:
        return "Billing", "keyword:billing_invoice"
    return "General", "context"


def _infer_event_confidence(text: str, topic: str, tone: str, evidence: str) -> float:
    lower_text = str(text or "").lower()
    normalized_topic = str(topic or "").strip().lower()
    normalized_evidence = str(evidence or "").strip().lower()
    normalized_tone = str(tone or "positive").lower()

    score = 0.52
    if normalized_topic and normalized_topic != "general":
        score += 0.08
    if normalized_tone in {"negative", "empathetic", "unhelpful"}:
        score += 0.07
    if len(lower_text) > 60:
        score += 0.05
    if "?" in lower_text:
        score += 0.03
    if normalized_evidence.startswith("topic_match"):
        score += 0.22
    elif normalized_evidence.startswith("topic_token"):
        score += 0.17
    elif normalized_evidence.startswith("keyword"):
        score += 0.14
    elif normalized_evidence == "qa_fallback":
        score += 0.05

    return round(min(0.98, max(0.35, score)), 2)


def _infer_event_tone(text: str) -> str:
    lower_text = text.lower()
    empathetic = [
        "sorry",
        "i understand",
        "i can imagine",
        "apologize",
        "thanks for your patience",
    ]
    unhelpful = [
        "can't",
        "cannot",
        "not possible",
        "no option",
        "can't help",
        "policy does not allow",
    ]
    negative = [
        "issue",
        "problem",
        "complaint",
        "delay",
        "angry",
        "refund",
        "escalate",
    ]
    positive = [
        "resolved",
        "great",
        "perfect",
        "happy",
        "thank you",
        "done",
    ]

    if any(word in lower_text for word in unhelpful):
        return "unhelpful"
    if any(word in lower_text for word in empathetic):
        return "empathetic"
    if any(word in lower_text for word in negative):
        return "negative"
    if any(word in lower_text for word in positive):
        return "positive"
    return "positive"
