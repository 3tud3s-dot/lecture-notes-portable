"""V0.4a sequential orchestration. No microphone, HTTP, or config imports.

An injected source owns acquisition and supplies session-relative sample offsets.
This module does NOT establish gap-free real capture while synchronous ASR waits.
"""

import json
import wave
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


SAMPLE_RATE = 44100
CHUNK_SECONDS = 6


def chunk_frames(chunk_seconds: float = CHUNK_SECONDS) -> int:
    """Validated baseline or explicitly selected V0.5d experiment cadence."""
    if chunk_seconds not in (6, 12):
        raise ValueError('Chunk duration must be 6 or 12 seconds, overlap 0.')
    return int(SAMPLE_RATE * chunk_seconds)


@dataclass(frozen=True)
class AudioChunk:
    audio_path: Path
    start_frame: int
    frame_count: int


@dataclass
class Segment:
    segment_id: int
    start_time: float
    end_time: float
    audio_path: str
    raw_transcript: str | None = None
    status: str = "pending"
    error: str | None = None


@dataclass
class Session:
    session_id: str
    started_at: str
    sample_rate: int = SAMPLE_RATE
    chunk_seconds: int = CHUNK_SECONDS
    overlap_seconds: int = 0
    segments: list[Segment] = field(default_factory=list)
    status: str = "running"
    error: str | None = None


def _persist(session: Session, directory: Path) -> None:
    # Each completed segment survives subsequent ASR failures. Replace atomically.
    temporary = directory / "session.json.tmp"
    temporary.write_text(json.dumps(asdict(session), ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(directory / "session.json")


def _validate_chunk(chunk: AudioChunk, expected_frame: int, seen_paths: set[Path],
                    *, chunk_seconds: float = CHUNK_SECONDS) -> Path:
    if not isinstance(chunk, AudioChunk):
        raise ValueError("invalid_chunk")
    if (type(chunk.start_frame) is not int or type(chunk.frame_count) is not int
            or chunk.start_frame != expected_frame
            or not 0 < chunk.frame_count <= chunk_frames(chunk_seconds)):
        raise ValueError("invalid_chunk_frames")
    path = Path(chunk.audio_path).resolve(strict=True)
    if path in seen_paths:
        raise ValueError("duplicate_chunk_path")
    with wave.open(str(path), "rb") as wav:
        if (wav.getcomptype() != "NONE" or wav.getsampwidth() != 2
                or wav.getnchannels() != 1 or wav.getframerate() != SAMPLE_RATE
                or wav.getnframes() != chunk.frame_count
                or len(wav.readframes(chunk.frame_count)) != chunk.frame_count * 2):
            raise ValueError("chunk_wav_mismatch")
    return path


def run_session(chunks: Iterable[AudioChunk], transcribe: Callable[[Path], str],
                output_root: Path, *, should_stop: Callable[[], bool] = lambda: False) -> Session:
    """Consume one chunk, transcribe once, persist, then obtain the next chunk.

    ASR exceptions create failed segments and continue without retry. Source or
    chunk errors end the session. Stop is checked between chunks; Ctrl+C during
    ASR preserves the current WAV as an interrupted segment. Source iterators
    exposing close() are closed on every exit. Only a final chunk may be short.

    The injected transcribe function must return credential-sanitized RAW text
    (as the existing ASR client does). Exception messages are never serialized.
    No real-source or ASR adapter is supplied in this preparation stage.
    """
    now = datetime.now(timezone.utc)
    session = Session(now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8], now.isoformat())
    directory = Path(output_root) / session.session_id
    directory.mkdir(parents=True, exist_ok=False)
    source = None
    expected_frame = 0
    seen_paths: set[Path] = set()
    short_tail_seen = False
    try:
        _persist(session, directory)
        try:
            source = iter(chunks)
        except Exception:
            session.status, session.error = "failed", "source_failed"
        while session.status == "running":
            try:
                if should_stop():
                    session.status = "stopped"
                    break
                chunk = next(source)
            except StopIteration:
                session.status = "completed"
                break
            except KeyboardInterrupt:
                session.status = "stopped"
                break
            except Exception:
                session.status, session.error = "failed", "source_failed"
                break
            try:
                if short_tail_seen:
                    raise ValueError("chunk_after_short_tail")
                path = _validate_chunk(chunk, expected_frame, seen_paths)
            except Exception:
                session.status, session.error = "failed", "invalid_chunk"
                break
            segment = Segment(len(session.segments), chunk.start_frame / SAMPLE_RATE,
                              (chunk.start_frame + chunk.frame_count) / SAMPLE_RATE, str(path))
            session.segments.append(segment)
            seen_paths.add(path)
            expected_frame += chunk.frame_count
            short_tail_seen = chunk.frame_count < SAMPLE_RATE * CHUNK_SECONDS
            # Save association before invoking ASR, including its pending status.
            _persist(session, directory)
            try:
                text = transcribe(path)
                if not isinstance(text, str):
                    segment.status, segment.error = "failed", "invalid_asr_result"
                else:
                    segment.raw_transcript, segment.status = text, "completed"
            except KeyboardInterrupt:
                segment.status, segment.error = "interrupted", "asr_interrupted"
                session.status = "stopped"
            except Exception:
                segment.status, segment.error = "failed", "asr_failed"
            _persist(session, directory)
    except KeyboardInterrupt:
        session.status = "stopped"
        if session.segments and session.segments[-1].status == "pending":
            session.segments[-1].status = "interrupted"
            session.segments[-1].error = "session_interrupted"
    finally:
        try:
            close = getattr(source, "close", None)
            if close is not None:
                close()
        except Exception:
            session.status, session.error = "failed", "source_close_failed"
        if session.status == "completed" and any(s.status == "failed" for s in session.segments):
            session.status = "completed_with_errors"
        # An I/O failure propagates; it must not cause another ASR request.
        if session.status != "running":
            _persist(session, directory)
    return session
