from __future__ import annotations

from pathlib import Path
from typing import Any

from sarvamai import SarvamAI


class SarvamService:
    def __init__(self, api_key: str) -> None:
        self.client = SarvamAI(api_subscription_key=api_key)

    def run_batch_transcription(
        self,
        file_paths: list[Path],
        model: str,
        language_code: str,
        with_diarization: bool,
        num_speakers: int | None = None,
        prompt: str | None = None,
        output_dir: Path | None = None,
    ) -> dict[str, Any]:
        job_kwargs: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "with_diarization": with_diarization,
            "num_speakers": num_speakers,
            "language_code": language_code,
        }
        job_kwargs = {key: value for key, value in job_kwargs.items() if value is not None}
        try:
            job = self.client.speech_to_text_translate_job.create_job(**job_kwargs)
        except TypeError:
            job_kwargs.pop("language_code", None)
            job = self.client.speech_to_text_translate_job.create_job(**job_kwargs)
        job.upload_files(file_paths=[str(path) for path in file_paths])
        job.start()
        job.wait_until_complete()
        results = job.get_file_results()
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            job.download_outputs(output_dir=str(output_dir))
        return results

    def chat_completion(self, messages: list[dict[str, str]], model: str) -> str:
        try:
            response = self.client.chat.completions(
                messages=messages,
                model=model,
            )
        except TypeError:
            response = self.client.chat.completions(messages=messages)
        return response.choices[0].message.content
