"""One local PCM WAV, one multipart request. No retries, recording or chunking."""

import hashlib
import io
import json
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .config import Settings
from .evidence import REDACTED, redact


TIMEOUT = (10, 60)  # connect seconds, read inactivity seconds; not a total deadline
MAX_AUDIO_BYTES = 50_000_000  # Conservative interpretation of the generic 50 MB limit.


class InputError(ValueError):
    """A safe, fixed local validation message; contains no credential values."""


class _BearerAuth(requests.auth.AuthBase):
    """Explicit auth prevents requests from replacing the key via a .netrc file."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {self._api_key}"
        return request


@dataclass(frozen=True)
class ASRResult:
    http_status: int | None
    latency_seconds: float
    response_body: str | None = field(default=None, repr=False)
    response_json: object = field(default=None, repr=False)
    response_is_json: bool = False
    response_redacted: bool = False
    transcript: str | None = field(default=None, repr=False)
    error: str | None = None
    content_type: str | None = None
    trace_id: str | None = None


def validate_settings(settings: Settings) -> None:
    if not settings.api_key or not settings.api_key.strip():
        raise InputError("SILICONFLOW_API_KEY is missing; configure it locally.")
    if "\r" in settings.api_key or "\n" in settings.api_key:
        raise InputError("SILICONFLOW_API_KEY must be a single line.")
    try:
        url = urlsplit(settings.transcription_endpoint)
        valid = (url.scheme == "https" and url.hostname and not url.username
                 and not url.password and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise InputError("Base URL must be HTTPS without credentials, query or fragment.")


def read_audio(path: Path) -> tuple[bytes, dict]:
    """Inspect actual PCM WAV bytes; these local checks do not prove API support."""
    if not path.is_file():
        raise InputError("Audio file is missing or is not a regular file.")
    if path.suffix.lower() != ".wav":
        raise InputError("This spike accepts PCM WAV (.wav); no conversion is performed.")
    try:
        with path.open("rb") as source:
            data = source.read(MAX_AUDIO_BYTES + 1)
    except OSError:
        raise InputError("Cannot read the audio file.") from None
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise InputError("Audio must be nonempty and at most 50,000,000 bytes.")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels, width, rate, frames, compression, _ = wav.getparams()
            pcm = wav.readframes(frames)
    except (wave.Error, EOFError):
        raise InputError("Audio is not a supported, readable PCM WAV file.") from None
    if (compression != "NONE" or not frames or rate <= 0
            or len(pcm) != frames * channels * width):
        raise InputError("PCM WAV contains empty, truncated or unsupported audio data.")
    duration = frames / rate
    if duration > 3600:
        raise InputError("Audio exceeds the generic documented one-hour limit.")
    return data, {
        "filename": path.name,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": "audio/wav",
        "container": "WAV",
        "codec": "PCM",
        "sample_rate_hz": rate,
        "channels": channels,
        "bits_per_sample": width * 8,
        "frame_count": frames,
        "duration_seconds": duration,
    }


def parse_response(response: requests.Response, elapsed: float, api_key: str) -> ASRResult:
    body = response.content.decode("utf-8", errors="replace")
    try:
        payload = json.loads(body)
        is_json = True
    except ValueError:
        payload, is_json = None, False
    clean = redact(payload if is_json else body, api_key)
    changed = clean != (payload if is_json else body)
    safe_body = (json.dumps(clean, ensure_ascii=False, indent=2) if is_json else str(clean)) if changed else body
    transcript, error = None, None
    if response.status_code != 200:
        error = "http_error"
    elif not is_json:
        error = "invalid_json_response"
    elif not isinstance(clean, dict) or not isinstance(clean.get("text"), str):
        error = "invalid_transcript_schema"
    elif not clean["text"].strip():
        error = "empty_transcript"
    elif clean["text"] == REDACTED:
        error = "transcript_redacted_for_credentials"
    else:
        transcript = clean["text"]  # Preserve whitespace and disfluencies unchanged.
    return ASRResult(
        http_status=response.status_code, latency_seconds=elapsed,
        response_body=safe_body, response_json=clean if is_json else None,
        response_is_json=is_json, response_redacted=changed,
        transcript=transcript, error=error,
        content_type=redact(response.headers.get("Content-Type"), api_key),
        trace_id=redact(response.headers.get("x-siliconcloud-trace-id"), api_key),
    )


def transcribe(audio: bytes, filename: str, settings: Settings) -> ASRResult:
    """One HTTP POST only; exceptions are classified without exposing their text."""
    validate_settings(settings)
    started = time.perf_counter()
    try:
        with requests.post(
            settings.transcription_endpoint,
            auth=_BearerAuth(settings.api_key),
            data={"model": settings.asr_model},
            files={"file": (filename, audio, "audio/wav")},
            timeout=TIMEOUT,
            allow_redirects=False,
        ) as response:
            return parse_response(response, time.perf_counter() - started, settings.api_key)
    except requests.Timeout:
        error = "timeout_no_automatic_retry"
    except requests.RequestException:
        error = "transport_error_no_automatic_retry"
    return ASRResult(None, time.perf_counter() - started, error=error)
