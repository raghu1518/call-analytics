from __future__ import annotations

import json
import logging
import math
import re
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydub import AudioSegment

from app.config import settings
from app.services.sarvam_client import SarvamService

logger = logging.getLogger(__name__)


@dataclass
class PipelineOutput:
    transcript_text_path: Path
    transcript_json_path: Path
    analysis_json_path: Path
    qa_json_path: Path
    summary_json_path: Path
    raw_llm_path: Path
    duration_seconds: float | None


class CallAnalyticsPipeline:
    def __init__(self, sarvam: SarvamService) -> None:
        self.sarvam = sarvam

    def process(
        self,
        audio_path: Path,
        output_dir: Path,
        language_code: str,
        stt_model: str,
        with_diarization: bool,
        num_speakers: int | None,
        prompt: str | None = None,
        prompt_pack: str | None = None,
        glossary_terms: str | None = None,
        on_progress: Callable[[str, float | None, dict[str, object]], None] | None = None,
    ) -> PipelineOutput:
        output_dir.mkdir(parents=True, exist_ok=True)
        stt_output_dir = output_dir / "stt"
        stt_output_dir.mkdir(parents=True, exist_ok=True)

        chunk_paths, duration_seconds, chunk_durations = _chunk_audio(
            audio_path, output_dir / "chunks", settings.chunk_minutes
        )
        if settings.enable_noise_suppression:
            chunk_paths = _apply_noise_suppression(chunk_paths, output_dir / "denoise")

        total_chunks = max(1, len(chunk_paths))
        if on_progress:
            on_progress("chunking_complete", 5, {"chunks": total_chunks})

        diarized_entries: list[dict[str, Any]] = []
        for index, chunk_path in enumerate(chunk_paths):
            if on_progress:
                on_progress(
                    "transcription_start",
                    10 + (index / total_chunks) * 60,
                    {"chunk": index + 1, "total_chunks": total_chunks},
                )
            chunk_output_dir = stt_output_dir / f"chunk_{index + 1:02d}"
            self.sarvam.run_batch_transcription(
                file_paths=[chunk_path],
                model=stt_model,
                language_code=language_code,
                with_diarization=with_diarization,
                num_speakers=num_speakers,
                prompt=prompt,
                output_dir=chunk_output_dir,
            )
            chunk_entries = _load_diarized_entries(chunk_output_dir)
            diarized_entries.extend(
                _offset_entries(
                    chunk_entries,
                    offset_seconds=sum(chunk_durations[:index]),
                    prefix=f"chunk{index + 1}_",
                )
            )
            if on_progress:
                on_progress(
                    "transcription_progress",
                    10 + ((index + 1) / total_chunks) * 60,
                    {"chunk": index + 1, "total_chunks": total_chunks},
                )

        transcript_text = _format_transcript(diarized_entries)
        cleaned_entries = (
            _cleanup_entries(diarized_entries)
            if settings.enable_pre_llm_cleanup
            else diarized_entries
        )
        cleaned_transcript_text = _format_transcript(cleaned_entries)
        speaker_stats = _compute_speaker_stats(diarized_entries)

        transcript_json_path = output_dir / "transcript.json"
        transcript_text_path = output_dir / "transcript.txt"

        transcript_json_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.utcnow().isoformat() + "Z",
                    "entries": diarized_entries,
                    "speaker_stats": speaker_stats,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        transcript_text_path.write_text(transcript_text, encoding="utf-8")
        (output_dir / "analysis_input.txt").write_text(
            cleaned_transcript_text, encoding="utf-8"
        )

        if on_progress:
            on_progress("analysis_start", 80, {"segments": len(cleaned_entries)})

        glossary_text = _merge_glossary_terms(glossary_terms or "", settings.glossary_path)
        bundle, raw_llm_text = _generate_analysis_bundle(
            self.sarvam,
            cleaned_transcript_text,
            speaker_stats,
            prompt_pack or settings.prompt_pack,
            glossary_text or settings.glossary_terms,
            prompt or "",
            diarized_entries,
            duration_seconds,
        )

        bundle = _apply_auto_tags(bundle, cleaned_transcript_text)
        bundle = _apply_sla_flags(bundle, duration_seconds)
        if on_progress:
            on_progress("analysis_complete", 95, {})

        analysis_json_path = output_dir / "analysis.json"
        qa_json_path = output_dir / "qa.json"
        summary_json_path = output_dir / "summary.json"
        raw_llm_path = output_dir / "analysis_raw.txt"

        analysis_json_path.write_text(
            json.dumps(bundle, indent=2),
            encoding="utf-8",
        )
        qa_json_path.write_text(
            json.dumps(bundle.get("qa_pairs", []), indent=2),
            encoding="utf-8",
        )
        summary_json_path.write_text(
            json.dumps(bundle.get("summary", {}), indent=2),
            encoding="utf-8",
        )
        raw_llm_path.write_text(raw_llm_text, encoding="utf-8")

        return PipelineOutput(
            transcript_text_path=transcript_text_path,
            transcript_json_path=transcript_json_path,
            analysis_json_path=analysis_json_path,
            qa_json_path=qa_json_path,
            summary_json_path=summary_json_path,
            raw_llm_path=raw_llm_path,
            duration_seconds=duration_seconds,
        )


def _chunk_audio(
    audio_path: Path, chunk_dir: Path, chunk_minutes: int
) -> tuple[list[Path], float | None, list[float]]:
    try:
        audio = AudioSegment.from_file(audio_path)
    except Exception:
        return [audio_path], None, [0.0]

    duration_seconds = len(audio) / 1000
    chunk_ms = chunk_minutes * 60 * 1000

    if duration_seconds <= chunk_minutes * 60:
        return [audio_path], duration_seconds, [duration_seconds]

    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_paths: list[Path] = []
    chunk_durations: list[float] = []

    total_chunks = math.ceil(len(audio) / chunk_ms)
    for index in range(total_chunks):
        start_ms = index * chunk_ms
        end_ms = min(len(audio), start_ms + chunk_ms)
        chunk = audio[start_ms:end_ms]
        chunk_path = chunk_dir / f"chunk_{index + 1:02d}.wav"
        chunk.export(chunk_path, format="wav")
        chunk_paths.append(chunk_path)
        chunk_durations.append((end_ms - start_ms) / 1000)

    return chunk_paths, duration_seconds, chunk_durations


def _apply_noise_suppression(chunk_paths: list[Path], work_dir: Path) -> list[Path]:
    try:
        from speexdsp_ns import NoiseSuppression
    except ImportError as exc:
        logger.warning(
            "SpeexDSP noise suppression requested but speexdsp_ns is not available. "
            "Skipping noise suppression."
        )
        return chunk_paths

    work_dir.mkdir(parents=True, exist_ok=True)
    processed_paths: list[Path] = []

    for index, chunk_path in enumerate(chunk_paths):
        wav_path = work_dir / f"{chunk_path.stem}_mono16k.wav"
        ns_path = work_dir / f"{chunk_path.stem}_ns.wav"
        _ensure_pcm_wav(chunk_path, wav_path, settings.noise_sample_rate)
        _run_speexdsp_ns(wav_path, ns_path, settings.noise_frame_size, NoiseSuppression)
        processed_paths.append(ns_path)

    return processed_paths


def _ensure_pcm_wav(source_path: Path, target_path: Path, sample_rate: int) -> None:
    try:
        audio = AudioSegment.from_file(source_path)
    except Exception as exc:
        raise RuntimeError(
            f"Unable to decode audio file for noise suppression: {source_path}"
        ) from exc

    audio = audio.set_channels(1).set_sample_width(2).set_frame_rate(sample_rate)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    audio.export(target_path, format="wav")


def _run_speexdsp_ns(
    input_path: Path, output_path: Path, frame_size: int, ns_cls: type
) -> None:
    with wave.open(str(input_path), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        if channels != 1 or sample_width != 2:
            raise RuntimeError(
                f"Expected mono 16-bit PCM WAV for noise suppression, got "
                f"{channels}ch {sample_width * 8}bit."
            )

        ns = ns_cls.create(frame_size, sample_rate)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)

            frame_bytes = frame_size * sample_width
            while True:
                raw = reader.readframes(frame_size)
                if not raw:
                    break
                raw_len = len(raw)
                if raw_len < frame_bytes:
                    raw = raw + b"\x00" * (frame_bytes - raw_len)
                    processed = ns.process(raw)
                    writer.writeframes(processed[:raw_len])
                    break
                processed = ns.process(raw)
                writer.writeframes(processed)


def _load_diarized_entries(output_dir: Path) -> list[dict[str, Any]]:
    json_files = sorted(output_dir.glob("*.json"))
    if not json_files:
        return []
    merged_entries: list[dict[str, Any]] = []
    for json_file in json_files:
        data = json.loads(json_file.read_text(encoding="utf-8"))
        entries: list[dict[str, Any]] = []
        if isinstance(data, dict):
            if "diarized_transcript" in data:
                entries = data.get("diarized_transcript", {}).get("entries", [])
            elif "entries" in data:
                entries = data.get("entries", [])
        if isinstance(entries, list):
            merged_entries.extend(entries)
    return merged_entries


def _offset_entries(
    entries: list[dict[str, Any]],
    offset_seconds: float,
    prefix: str,
) -> list[dict[str, Any]]:
    adjusted = []
    for entry in entries:
        start = _get_time(entry, "start_time_seconds", "start_time", "start")
        end = _get_time(entry, "end_time_seconds", "end_time", "end")
        speaker = entry.get("speaker_id") or entry.get("speaker") or "speaker"
        adjusted.append(
            {
                "speaker_id": f"{prefix}{speaker}",
                "start_time_seconds": (start or 0) + offset_seconds,
                "end_time_seconds": (end or 0) + offset_seconds,
                "transcript": entry.get("transcript", "").strip(),
            }
        )
    return adjusted


def _get_time(entry: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = entry.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _format_transcript(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in entries:
        start = _format_time(entry.get("start_time_seconds", 0))
        end = _format_time(entry.get("end_time_seconds", 0))
        speaker = entry.get("speaker_id", "speaker")
        transcript = entry.get("transcript", "").strip()
        if not transcript:
            continue
        lines.append(f"[{start} - {end}] {speaker}: {transcript}")
    return "\n".join(lines)


def _format_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    seconds = int(seconds % 60)
    return f"{minutes:02d}:{seconds:02d}"


def _compute_speaker_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, dict[str, float]] = {}
    for entry in entries:
        speaker = entry.get("speaker_id", "speaker")
        start = float(entry.get("start_time_seconds", 0))
        end = float(entry.get("end_time_seconds", 0))
        duration = max(0.0, end - start)
        words = len(entry.get("transcript", "").split())

        speaker_stats = stats.setdefault(speaker, {"duration": 0.0, "words": 0})
        speaker_stats["duration"] += duration
        speaker_stats["words"] += words

    return stats


def _cleanup_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filler_terms = _parse_glossary_terms(settings.filler_words)
    cleaned_entries: list[dict[str, Any]] = []

    for entry in entries:
        transcript = entry.get("transcript", "")
        cleaned = _remove_fillers(str(transcript), filler_terms)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
        if not cleaned:
            continue

        start = _get_time(entry, "start_time_seconds", "start_time", "start") or 0.0
        end = _get_time(entry, "end_time_seconds", "end_time", "end") or start
        start = max(0.0, float(start))
        end = max(start, float(end))

        cleaned_entries.append(
            {
                "speaker_id": entry.get("speaker_id", "speaker"),
                "start_time_seconds": start,
                "end_time_seconds": end,
                "transcript": cleaned,
            }
        )

    return cleaned_entries


def _remove_fillers(text: str, fillers: list[str]) -> str:
    if not fillers:
        return text

    cleaned = text
    for filler in fillers:
        if not filler:
            continue
        pattern = r"\b" + re.escape(filler) + r"\b"
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", cleaned, flags=re.IGNORECASE)
    return cleaned


def _prompt_pack_instructions(pack: str) -> str:
    pack = (pack or "general").strip().lower()
    if pack == "sales":
        return (
            "Focus on lead intent, objections, pricing, competitors, deal stage, "
            "next steps, and close probability.\n"
        )
    if pack == "support":
        return (
            "Focus on issue description, troubleshooting, root cause, SLA impact, "
            "resolution status, and follow-up steps.\n"
        )
    if pack == "collections":
        return (
            "Focus on delinquency status, payment promises, compliance language, "
            "negotiated dates, and risk signals.\n"
        )
    return ""


def _parse_glossary_terms(glossary: str) -> list[str]:
    if not glossary:
        return []
    items = []
    for part in glossary.replace("\n", ",").split(","):
        term = part.strip()
        if term and term not in items:
            items.append(term)
    return items[:50]


def _format_glossary(glossary: str) -> str:
    terms = _parse_glossary_terms(glossary)
    if not terms:
        return ""
    return f"Glossary terms: {', '.join(terms)}\n"


def _format_context_prompt(prompt: str) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        return ""
    return f"Context notes: {prompt}\n"


def _load_glossary_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    terms: list[str] = []
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return []
    for line in content.splitlines():
        parts = [part.strip() for part in line.split(",") if part.strip()]
        if parts:
            if parts[0].lower() == "term":
                continue
            terms.extend(parts)
    return _parse_glossary_terms(",".join(terms))


def _merge_glossary_terms(glossary: str, glossary_path: Path) -> str:
    file_terms = _load_glossary_file(glossary_path)
    combined = _parse_glossary_terms(glossary)
    combined.extend(term for term in file_terms if term not in combined)
    return ", ".join(combined)


def _parse_auto_tags(tag_spec: str) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = {}
    if not tag_spec:
        return tags
    for part in tag_spec.split(";"):
        if ":" not in part:
            continue
        label, keywords = part.split(":", 1)
        label = label.strip()
        terms = _parse_glossary_terms(keywords)
        if label and terms:
            tags[label] = terms
    return tags


def _apply_auto_tags(bundle: dict[str, Any], transcript_text: str) -> dict[str, Any]:
    tag_map = _parse_auto_tags(settings.auto_tags)
    if not tag_map:
        return bundle
    text = transcript_text.lower()
    tags = []
    for label, keywords in tag_map.items():
        if any(keyword.lower() in text for keyword in keywords):
            tags.append(label)
    if tags:
        bundle["auto_tags"] = tags
    return bundle


def _apply_sla_flags(bundle: dict[str, Any], duration_seconds: float | None) -> dict[str, Any]:
    if duration_seconds is None:
        return bundle
    breach = duration_seconds > settings.sla_minutes * 60
    resolution = bundle.get("resolution", {})
    status = ""
    if isinstance(resolution, dict):
        status = str(resolution.get("status") or "").lower()
    bundle["sla"] = {
        "breach": breach,
        "threshold_minutes": settings.sla_minutes,
        "resolution_status": status,
    }
    return bundle


def _average_confidence(confidences: dict[str, Any]) -> float | None:
    values = []
    for value in confidences.values():
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return sum(values) / len(values)


def _maybe_rerun_low_confidence(
    sarvam: SarvamService,
    bundle: dict[str, Any],
    transcript_text: str,
    prompt_pack: str,
    glossary_terms: str,
    context_prompt: str,
    speaker_stats: dict[str, Any],
) -> dict[str, Any]:
    sentiment = bundle.get("sentiment", {})
    sentiment_conf = None
    if isinstance(sentiment, dict):
        try:
            sentiment_conf = float(sentiment.get("confidence"))
        except (TypeError, ValueError):
            sentiment_conf = None

    role_conf = None
    roles_conf = bundle.get("speaker_roles_confidence")
    if isinstance(roles_conf, dict):
        role_conf = _average_confidence(roles_conf)

    if (
        (sentiment_conf is not None and sentiment_conf < settings.sentiment_confidence_threshold)
        or (role_conf is not None and role_conf < settings.role_confidence_threshold)
    ):
        retry_prompt = (
            "You are a call analytics assistant. Provide ONLY JSON with keys: "
            "sentiment, speaker_roles, speaker_roles_confidence. "
            "sentiment should include overall, customer, agent, confidence (0-1). "
            "speaker_roles should map speaker_id to Agent, Customer, or Other. "
            "speaker_roles_confidence should map speaker_id to confidence 0-1.\n\n"
            f"{_prompt_pack_instructions(prompt_pack)}"
            f"{_format_glossary(glossary_terms)}"
            f"{_format_context_prompt(context_prompt)}"
            f"Speaker stats: {json.dumps(speaker_stats)}\n\n"
            "Transcript:\n"
            f"{transcript_text}"
        )
        messages = [
            {"role": "system", "content": "You are a precise call analytics assistant."},
            {"role": "user", "content": retry_prompt},
        ]
        try:
            rerun_text = sarvam.chat_completion(messages=messages, model=settings.sarvam_llm_model)
            rerun_bundle = _safe_json_loads(rerun_text)
        except Exception:
            rerun_bundle = None

        if isinstance(rerun_bundle, dict):
            if rerun_bundle.get("sentiment"):
                bundle["sentiment"] = rerun_bundle.get("sentiment")
            if rerun_bundle.get("speaker_roles"):
                bundle["speaker_roles"] = rerun_bundle.get("speaker_roles")
            if rerun_bundle.get("speaker_roles_confidence"):
                bundle["speaker_roles_confidence"] = rerun_bundle.get("speaker_roles_confidence")

    return bundle


def _force_json_bundle(
    sarvam: SarvamService,
    transcript_text: str,
    speaker_stats: dict[str, Any],
    prompt_pack: str,
    glossary_terms: str,
    context_prompt: str,
) -> dict[str, Any] | None:
    retry_prompt = (
        "Return ONLY JSON with keys: summary, sentiment, topics, action_items, resolution, "
        "qa_pairs, speaker_roles, speaker_roles_confidence, compliance_flags. "
        "summary should include short and bullets. "
        "sentiment should include overall, customer, agent, confidence (0-1). "
        "resolution should include status and next_steps. "
        "qa_pairs should be an array of {question, answer}. "
        "speaker_roles should map speaker_id to Agent, Customer, or Other. "
        "speaker_roles_confidence should map speaker_id to confidence 0-1.\n\n"
        f"{_prompt_pack_instructions(prompt_pack)}"
        f"{_format_glossary(glossary_terms)}"
        f"{_format_context_prompt(context_prompt)}"
        f"Speaker stats: {json.dumps(speaker_stats)}\n\n"
        "Transcript:\n"
        f"{transcript_text}"
    )
    messages = [
        {"role": "system", "content": "You are a precise call analytics assistant."},
        {"role": "user", "content": retry_prompt},
    ]
    try:
        retry_text = sarvam.chat_completion(messages=messages, model=settings.sarvam_llm_model)
        return _safe_json_loads(retry_text)
    except Exception:
        return None


def _infer_roles_from_entries(entries: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, float]]:
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

    roles: dict[str, str] = {}
    confidences: dict[str, float] = {}
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
            role = "Other"
            confidence = 0.2

        roles[speaker_id] = role
        confidences[speaker_id] = round(min(1.0, confidence), 2)

    return roles, confidences


def _infer_names_from_entries(entries: list[dict[str, Any]]) -> dict[str, str]:
    name_patterns = [
        r"\bmy name is\s+([a-z][a-z'-]{1,})(?:\s+([a-z][a-z'-]{1,}))?",
        r"\bthis is\s+([a-z][a-z'-]{1,})(?:\s+([a-z][a-z'-]{1,}))?",
        r"\bi am\s+([a-z][a-z'-]{1,})(?:\s+([a-z][a-z'-]{1,}))?",
        r"\bi'm\s+([a-z][a-z'-]{1,})(?:\s+([a-z][a-z'-]{1,}))?",
        r"\bim\s+([a-z][a-z'-]{1,})(?:\s+([a-z][a-z'-]{1,}))?",
        r"\byou'?re speaking with\s+([a-z][a-z'-]{1,})(?:\s+([a-z][a-z'-]{1,}))?",
        r"\bspeaking with\s+([a-z][a-z'-]{1,})(?:\s+([a-z][a-z'-]{1,}))?",
    ]
    stopwords = {
        "calling",
        "call",
        "speaking",
        "from",
        "with",
        "help",
        "support",
        "service",
        "team",
        "company",
        "account",
        "billing",
        "sales",
        "issue",
        "problem",
        "today",
        "here",
        "there",
        "name",
        "hello",
        "hi",
        "hey",
        "thanks",
        "thank",
        "good",
        "morning",
        "afternoon",
        "evening",
        "sir",
        "maam",
        "madam",
        "miss",
        "mr",
        "mrs",
        "ms",
        "dr",
        "doctor",
        "manager",
        "supervisor",
        "assistant",
        "rep",
        "representative",
    }

    def normalize_token(token: str | None) -> str | None:
        if not token:
            return None
        token = token.strip(" ,.-").lower()
        if not token or token in stopwords:
            return None
        if not token[0].isalpha():
            return None
        if any(char.isdigit() for char in token):
            return None
        if len(token) < 2:
            return None
        return token

    def format_name(tokens: list[str]) -> str:
        return " ".join(part.title() for part in tokens)

    names: dict[str, str] = {}
    name_quality: dict[str, int] = {}

    for entry in entries:
        speaker_id = str(entry.get("speaker_id", "speaker"))
        text = str(entry.get("transcript", "")).lower()
        if not text:
            continue
        for pattern in name_patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            first = normalize_token(match.group(1))
            last = normalize_token(match.group(2))
            if not first:
                continue
            tokens = [first]
            if last:
                tokens.append(last)
            candidate = format_name(tokens)
            quality = len(tokens)
            existing_quality = name_quality.get(speaker_id, 0)
            if speaker_id not in names or quality > existing_quality:
                names[speaker_id] = candidate
                name_quality[speaker_id] = quality
            break

    return names


def _generate_analysis_bundle(
    sarvam: SarvamService,
    transcript_text: str,
    speaker_stats: dict[str, Any],
    prompt_pack: str,
    glossary_terms: str,
    context_prompt: str,
    entries: list[dict[str, Any]],
    duration_seconds: float | None,
) -> tuple[dict[str, Any], str]:
    truncated_transcript = transcript_text
    if len(truncated_transcript) > settings.max_transcript_chars:
        truncated_transcript = (
            truncated_transcript[: settings.max_transcript_chars]
            + "\n[TRUNCATED]"
        )

    pack_instructions = _prompt_pack_instructions(prompt_pack)
    glossary_text = _format_glossary(glossary_terms)
    heuristic_roles, heuristic_confidence = _infer_roles_from_entries(entries)
    heuristic_names = _infer_names_from_entries(entries)

    prompt = (
        "You are a call analytics assistant. Analyze the transcript and speaker stats. "
        "Return ONLY JSON with keys: summary, sentiment, topics, action_items, resolution, "
        "qa_pairs, speaker_roles, speaker_roles_confidence, compliance_flags. "
        "summary should include short and bullets. "
        "sentiment should include overall, customer, agent, confidence (0-1). "
        "resolution should include status and next_steps. "
        "qa_pairs should be an array of {question, answer}. "
        "speaker_roles should map speaker_id to Agent, Customer, or Other. "
        "speaker_roles_confidence should map speaker_id to confidence 0-1. "
        "speaker_names should map speaker_id to a name string or null. "
        "If unsure, use null or empty arrays.\n\n"
        f"{pack_instructions}"
        f"{glossary_text}"
        f"{_format_context_prompt(context_prompt)}"
        f"Heuristic roles (use as fallback): {json.dumps(heuristic_roles)}\n"
        f"Heuristic role confidence: {json.dumps(heuristic_confidence)}\n\n"
        f"Heuristic names (use as fallback): {json.dumps(heuristic_names)}\n\n"
        f"Speaker stats: {json.dumps(speaker_stats)}\n\n"
        "Transcript:\n"
        f"{truncated_transcript}"
    )

    messages = [
        {"role": "system", "content": "You are a precise call analytics assistant."},
        {"role": "user", "content": prompt},
    ]

    raw_text = sarvam.chat_completion(messages=messages, model=settings.sarvam_llm_model)
    bundle = _safe_json_loads(raw_text)
    if bundle is None and settings.enable_fallback_prompt:
        forced_bundle = _force_json_bundle(
            sarvam,
            transcript_text,
            speaker_stats,
            prompt_pack,
            glossary_terms,
            context_prompt,
        )
        if forced_bundle:
            bundle = forced_bundle
    if bundle is None:
        bundle = {"raw_text": raw_text}
    bundle.setdefault("prompt_pack", prompt_pack)
    if glossary_terms:
        bundle.setdefault("glossary_terms", _parse_glossary_terms(glossary_terms))
    if settings.enable_role_heuristics:
        roles = bundle.get("speaker_roles")
        if not isinstance(roles, dict) or not roles:
            bundle["speaker_roles"] = heuristic_roles
        confidences = bundle.get("speaker_roles_confidence")
        if not isinstance(confidences, dict) or not confidences:
            bundle["speaker_roles_confidence"] = heuristic_confidence
    names = bundle.get("speaker_names")
    if not isinstance(names, dict) or not names:
        if heuristic_names:
            bundle["speaker_names"] = heuristic_names
    else:
        for speaker_id, name in heuristic_names.items():
            if speaker_id not in names or not names.get(speaker_id):
                names[speaker_id] = name
    if settings.enable_fallback_prompt:
        bundle = _maybe_rerun_low_confidence(
            sarvam,
            bundle,
            transcript_text,
            prompt_pack,
            glossary_terms,
            context_prompt,
            speaker_stats,
        )
    bundle.setdefault("duration_seconds", duration_seconds)
    return bundle, raw_text


def _safe_json_loads(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()

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
