from __future__ import annotations

from django.urls import path

from app import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("upload", views.upload_entry, name="upload_entry"),
    path("glossary/upload", views.upload_glossary, name="upload_glossary"),
    path("calls/bulk", views.bulk_actions, name="bulk_actions"),
    path("calls/<str:call_id>", views.call_detail, name="call_detail"),
    path("calls/<str:call_id>/audio", views.call_audio, name="call_audio"),
    path("calls/<str:call_id>/export", views.export_call, name="export_call"),
    path("api/metrics", views.api_metrics, name="api_metrics"),
    path("api/calls/<str:call_id>", views.api_call, name="api_call"),
    path("api/realtime/events", views.api_realtime_events, name="api_realtime_events"),
    path("api/realtime/audio/chunk", views.api_realtime_audio_chunk, name="api_realtime_audio_chunk"),
    path("api/realtime/stream", views.api_realtime_stream, name="api_realtime_stream"),
    path(
        "api/realtime/calls/<str:call_id>/snapshot",
        views.api_realtime_call_snapshot,
        name="api_realtime_call_snapshot",
    ),
    path(
        "api/realtime/calls/<str:call_id>/audio",
        views.api_realtime_call_audio,
        name="api_realtime_call_audio",
    ),
    path(
        "api/realtime/calls/<str:call_id>/audio/meta",
        views.api_realtime_call_audio_meta,
        name="api_realtime_call_audio_meta",
    ),
    path("api/realtime/alerts", views.api_realtime_alerts, name="api_realtime_alerts"),
    path(
        "api/integrations/genesys/health",
        views.api_genesys_connector_health,
        name="api_genesys_connector_health",
    ),
    path(
        "api/integrations/genesys/audiohook/health",
        views.api_genesys_audiohook_health,
        name="api_genesys_audiohook_health",
    ),
    path(
        "api/realtime/alerts/<int:alert_id>/ack",
        views.api_realtime_alert_ack,
        name="api_realtime_alert_ack",
    ),
]
