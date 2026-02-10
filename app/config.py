from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    app_name: str = "Call Analytics"
    data_dir: Path = Path("data")
    uploads_dir: Path = data_dir / "uploads"
    outputs_dir: Path = data_dir / "outputs"
    db_path: Path = data_dir / "db" / "call_analytics.db"

    database_url: str | None = None

    sarvam_api_key: str = ""
    sarvam_stt_model: str = "saaras:v2.5"
    sarvam_llm_model: str = "sarvam-m"
    language_code: str = "en-IN"

    diarization: bool = True
    num_speakers: int | None = None

    max_transcript_chars: int = 12000
    worker_concurrency: int = 2
    chunk_minutes: int = 60
    enable_noise_suppression: bool = True
    noise_frame_size: int = 256
    noise_sample_rate: int = 16000
    enable_pre_llm_cleanup: bool = True
    filler_words: str = (
        "um,uh,erm,ah,like,you know,i mean,hm,hmm,mm,uh-huh,uh huh,okay,ok,yeah"
    )
    prompt_pack: str = "general"
    glossary_terms: str = ""
    enable_role_heuristics: bool = True
    glossary_path: Path = data_dir / "glossary.csv"
    auto_tags: str = "billing:refund,invoice;churn risk:cancel,close account;escalation:manager,supervisor"
    sla_minutes: int = 10
    role_confidence_threshold: float = 0.5
    sentiment_confidence_threshold: float = 0.5
    enable_fallback_prompt: bool = True
    realtime_ingest_token: str = ""
    realtime_negative_sentiment_threshold: float = -0.45
    realtime_high_risk_threshold: float = 0.72
    realtime_alert_cooldown_seconds: int = 75
    realtime_supervisor_keyword_triggers: str = (
        "manager,supervisor,escalate,cancel account,lawyer,legal,complaint,refund now"
    )
    realtime_audio_dir: Path = data_dir / "runtime" / "live_audio"
    realtime_audio_window_seconds: int = 300
    realtime_audio_default_sample_rate: int = 16000
    realtime_audio_default_channels: int = 1
    realtime_audio_max_chunk_bytes: int = 2_000_000
    genesys_audiohook_host: str = "0.0.0.0"
    genesys_audiohook_port: int = 9011
    genesys_audiohook_path: str = "/audiohook/ws"
    genesys_audiohook_target_audio_ingest_url: str = "http://127.0.0.1:8009/api/realtime/audio/chunk"
    genesys_audiohook_target_event_ingest_url: str = "http://127.0.0.1:8009/api/realtime/events"
    genesys_audiohook_target_ingest_token: str = ""
    genesys_audiohook_verify_ssl: bool = True
    genesys_audiohook_http_timeout_seconds: int = 20
    genesys_audiohook_retry_max_attempts: int = 5
    genesys_audiohook_retry_backoff_seconds: float = 1.5
    genesys_audiohook_flush_interval_ms: int = 750
    genesys_audiohook_min_chunk_duration_ms: int = 300
    genesys_audiohook_max_chunk_duration_ms: int = 2000
    genesys_audiohook_status_path: Path = data_dir / "runtime" / "genesys_audiohook_status.json"
    genesys_audiohook_health_stale_seconds: int = 90
    genesys_login_base_url: str = "https://login.mypurecloud.com"
    genesys_api_base_url: str = "https://api.mypurecloud.com"
    genesys_client_id: str = ""
    genesys_client_secret: str = ""
    genesys_subscription_topics: str = ""
    genesys_queue_ids: str = ""
    genesys_user_ids: str = ""
    genesys_target_ingest_url: str = "http://127.0.0.1:8009/api/realtime/events"
    genesys_target_ingest_token: str = ""
    genesys_verify_ssl: bool = True
    genesys_http_timeout_seconds: int = 20
    genesys_retry_max_attempts: int = 5
    genesys_retry_backoff_seconds: float = 1.5
    genesys_reconnect_delay_seconds: int = 5
    genesys_topic_builder_mode: str = "queues_users"
    genesys_topic_builder_queue_name_filters: str = ""
    genesys_topic_builder_user_name_filters: str = ""
    genesys_topic_builder_user_email_domain_filters: str = ""
    genesys_topic_builder_max_queues: int = 25
    genesys_topic_builder_max_users: int = 50
    genesys_topic_builder_refresh_seconds: int = 900
    genesys_connector_status_path: Path = data_dir / "runtime" / "genesys_connector_status.json"
    genesys_connector_health_stale_seconds: int = 90

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path.as_posix()}"


settings = Settings()
