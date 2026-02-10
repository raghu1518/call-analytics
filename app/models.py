from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from django.db import models


def _default_call_id() -> str:
    return str(uuid4())


class Call(models.Model):
    id = models.CharField(
        primary_key=True,
        max_length=64,
        default=_default_call_id,
        editable=False,
    )
    filename = models.TextField()
    storage_path = models.TextField()

    status = models.CharField(max_length=32, default="queued", db_index=True)
    created_at = models.DateTimeField(default=datetime.utcnow, db_index=True)
    updated_at = models.DateTimeField(default=datetime.utcnow)

    duration_seconds = models.FloatField(null=True, blank=True)
    language_code = models.CharField(max_length=32, null=True, blank=True)
    stt_model = models.CharField(max_length=128, null=True, blank=True)
    with_diarization = models.BooleanField(null=True, blank=True)
    num_speakers = models.IntegerField(null=True, blank=True)
    prompt = models.TextField(null=True, blank=True)
    prompt_pack = models.CharField(max_length=64, null=True, blank=True)
    glossary_terms = models.TextField(null=True, blank=True)

    transcript_text_path = models.TextField(null=True, blank=True)
    transcript_json_path = models.TextField(null=True, blank=True)
    analysis_json_path = models.TextField(null=True, blank=True)
    qa_json_path = models.TextField(null=True, blank=True)
    summary_json_path = models.TextField(null=True, blank=True)
    raw_llm_path = models.TextField(null=True, blank=True)

    error_message = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "call"


class RealtimeCall(models.Model):
    call_id = models.CharField(primary_key=True, max_length=128, editable=False)
    provider = models.CharField(max_length=64, default="generic")
    status = models.CharField(max_length=32, default="active", db_index=True)
    created_at = models.DateTimeField(default=datetime.utcnow, db_index=True)
    updated_at = models.DateTimeField(default=datetime.utcnow, db_index=True)
    agent_id = models.CharField(max_length=128, null=True, blank=True)
    customer_id = models.CharField(max_length=128, null=True, blank=True)
    last_speaker = models.CharField(max_length=32, null=True, blank=True)
    last_text = models.TextField(blank=True, default="")
    sentiment_score = models.FloatField(default=0.0)
    risk_score = models.FloatField(default=0.0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "realtime_call"


class RealtimeEvent(models.Model):
    realtime_call = models.ForeignKey(
        RealtimeCall,
        on_delete=models.CASCADE,
        related_name="events",
    )
    occurred_at = models.DateTimeField(default=datetime.utcnow, db_index=True)
    event_type = models.CharField(max_length=64, db_index=True)
    speaker = models.CharField(max_length=32, null=True, blank=True)
    text = models.TextField(blank=True, default="")
    sentiment = models.FloatField(null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "realtime_event"
        indexes = [
            models.Index(fields=["realtime_call", "-occurred_at"]),
            models.Index(fields=["event_type", "-occurred_at"]),
        ]


class SupervisorAlert(models.Model):
    realtime_call = models.ForeignKey(
        RealtimeCall,
        on_delete=models.CASCADE,
        related_name="alerts",
    )
    created_at = models.DateTimeField(default=datetime.utcnow, db_index=True)
    alert_type = models.CharField(max_length=64, db_index=True)
    severity = models.CharField(max_length=16, db_index=True)
    message = models.TextField()
    acknowledged = models.BooleanField(default=False, db_index=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "supervisor_alert"
        indexes = [
            models.Index(fields=["realtime_call", "acknowledged", "-created_at"]),
            models.Index(fields=["severity", "-created_at"]),
        ]
