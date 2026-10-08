"""V0.4c1: one producer, one FIFO, one ASR worker. No live CLI in this stage."""

import array
import copy
import sys
import threading
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
from uuid import uuid4

from . import asr, chunk_capture, evidence
from .chunk_pipeline import AudioChunk, Segment, Session, SAMPLE_RATE, CHUNK_SECONDS, chunk_frames, _persist, _validate_chunk
from .config import Settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RuntimeSegment(Segment):
    start_frame: int = 0
    end_frame: int = 0
    capture_completed_at: str | None = None
    capture_completed_monotonic_ns: int | None = None
    captured_at: str | None = None
    queued_at: str | None = None
    asr_started_at: str | None = None
    asr_finished_at: str | None = None
    queue_depth_when_queued: int = 0
    transitions: list[dict] = field(default_factory=list)
    asr_started_monotonic_ns: int | None = None
    asr_finished_monotonic_ns: int | None = None
    http_status: int | None = None
    asr_latency_seconds: float | None = None
    request_count: int = 0
    transcript_printed_at: str | None = None
    transcript_printed_monotonic_ns: int | None = None


@dataclass
class RuntimeSession(Session):
    queue_depth: int = 0  # Queued but not yet marked transcribing; excludes active ASR.
    max_queue_depth: int = 0
    capture_finished_at: str | None = None
    consumer_finished_at: str | None = None
    finalized_at: str | None = None
    capture_report: dict | None = None
    capture_started_at: str | None = None
    producer_finished_at: str | None = None
    total_request_count: int = 0


def ordered_transcript(session: Session) -> list[dict]:
    """Keep failures visible and each raw string unchanged; no text merging."""
    return [{k: getattr(s, k) for k in ('segment_id', 'start_time', 'end_time',
                                       'raw_transcript', 'status', 'error')}
            for s in sorted(session.segments, key=lambda item: item.segment_id)]


def siliconflow_asr(settings: Settings) -> Callable[[Path, Path], asr.ASRResult]:
    """Adapter to the frozen client and evidence writer; no new HTTP code."""
    def invoke(path: Path, directory: Path) -> asr.ASRResult:
        asr.validate_settings(settings)
        audio, metadata = asr.read_audio(path)
        evidence.write_json(directory / 'request.json', evidence.redact({
            'created_at_utc': utc_now(), 'method': 'POST', 'endpoint': settings.transcription_endpoint,
            'model': settings.asr_model, 'audio': metadata, 'automatic_retries': 0,
        }, settings.api_key or ''))
        timing = {'request_count': 1, 'asr_started_at': utc_now(),
                  'asr_started_monotonic_ns': time.perf_counter_ns()}
        try:
            return asr.transcribe(audio, path.name, settings)
        finally:
            timing.update(asr_finished_monotonic_ns=time.perf_counter_ns(), asr_finished_at=utc_now())
            evidence.write_json(directory / 'timing.json', timing)
    return invoke


def continuous_producer(config: dict, *, collect_timing: bool = False,
                        stop_event=None, on_started: Callable[[], None] | None = None) -> Callable:
    """Adapt the existing bounded blocking recorder; callers own the HUMAN GATE.

    Completed WAVs are persisted on the ordinary producer thread, never on a
    PortAudio callback. Disk latency remains a real-runtime risk to inspect.
    """
    frames_per_chunk = chunk_frames(config.get('chunk_duration_seconds', CHUNK_SECONDS))
    def produce(publish: Callable[[AudioChunk], None], audio_dir: Path) -> dict:
        blocks: list[bytes] = []
        pending = bytearray()
        chunks: list[AudioChunk] = []
        report = dict(config, stream_open_attempts=0, stream_open_count=0, stream_close_attempts=0,
                      stream_close_count=0, actual_captured_frames=0, reads=[], portaudio_status_events=[])

        def little_endian(pcm: bytes) -> bytes:
            if sys.byteorder == 'little':
                return pcm
            samples = array.array('h'); samples.frombytes(pcm); samples.byteswap()
            return samples.tobytes()

        def emit(pcm: bytes) -> None:
            path = audio_dir / f'chunk-{len(chunks):03d}.wav'
            chunk_capture._write_wav(path, pcm)
            start = sum(c.frame_count for c in chunks)
            chunk = AudioChunk(path, start, len(pcm) // 2)
            chunks.append(chunk)
            if collect_timing:
                final_read = report['reads'][-1]
                publish(chunk, capture_completed_at=final_read['completed_at_utc'],
                        capture_completed_monotonic_ns=final_read['completed_monotonic_ns'])
            else:
                publish(chunk)  # Unbounded FIFO handoff; never waits for an ASR result.

        def receive(pcm: bytes) -> None:
            pending.extend(little_endian(pcm))
            size = frames_per_chunk * 2
            while len(pending) >= size:
                part = bytes(pending[:size]); del pending[:size]
                emit(part)

        try:
            control = {}
            if stop_event is not None:
                control['stop_event'] = stop_event
            if on_started is not None:
                control['on_started'] = on_started
            chunk_capture._acquire(dict(config, collect_timing=collect_timing), report, blocks,
                                   on_pcm=receive, **control)
        finally:
            # On capture failure preserve all complete reads; caller marks failure.
            if pending:
                emit(bytes(pending))
            if blocks:
                chunk_capture._write_wav(audio_dir / 'full.wav', little_endian(b''.join(blocks)))
                chunk_capture._write_wav(audio_dir / 'chunks-rejoined.wav',
                    b''.join(chunk_capture._read_pcm(c.audio_path) for c in chunks))
            evidence.write_json(audio_dir / 'capture.json', report)
        if report['portaudio_status_events']:
            raise RuntimeError('capture_overflow')
        return report
    return produce


def run_concurrent(produce: Callable, transcribe: Callable[[Path, Path], asr.ASRResult],
                   recordings_root: Path, output_root: Path, *, clock: Callable[[], str] = utc_now,
                   on_segment: Callable[[RuntimeSegment], None] | None = None,
                   on_session: Callable[[str, Path, Path], None] | None = None,
                   chunk_seconds: float = CHUNK_SECONDS, allow_partial_chunks: bool = False) -> RuntimeSession:
    """Run producer here and a single worker thread; finish capture then drain.

    Scope is a bounded short session. Queue has no silent eviction or backpressure;
    sustained overload is not production-safe. Only brief state mutations share a
    lock: HTTP, WAV/JSON writes and queue waits never hold the producer state lock.
    Normal drain is guaranteed for returning/failing calls, not a hung HTTP call
    or hard process termination. Real Ctrl+C semantics are a later-stage test.
    """
    frames_per_chunk = chunk_frames(chunk_seconds)
    session = RuntimeSession(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid4().hex[:8], clock(),
                             chunk_seconds=int(chunk_seconds))
    audio_dir = Path(recordings_root) / session.session_id
    output = Path(output_root) / session.session_id
    audio_dir.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    jobs: Queue = Queue()  # Intentionally unbounded in this short-session spike.
    sentinel = object()
    state_lock = threading.Lock()
    disk_lock = threading.Lock()
    producer_seen: set[Path] = set()
    expected_frame = 0
    tail_seen = False

    def save() -> None:
        with disk_lock:
            with state_lock:
                snapshot = copy.deepcopy(session)
            _persist(snapshot, output)  # Reuse existing session persistence.

    def transition(segment: RuntimeSegment, status: str, at: str) -> None:
        segment.status = status
        segment.transitions.append({'status': status, 'at': at})

    def publish(chunk: AudioChunk, *, capture_completed_at=None, capture_completed_monotonic_ns=None) -> None:
        nonlocal expected_frame, tail_seen
        path = Path(chunk.audio_path).resolve(strict=True)
        if ((tail_seen and not allow_partial_chunks) or type(chunk.start_frame) is not int or type(chunk.frame_count) is not int
                or chunk.start_frame != expected_frame or not 0 < chunk.frame_count <= frames_per_chunk
                or path in producer_seen):
            raise ValueError('invalid_chunk_sequence')
        captured = clock()
        with state_lock:
            segment = RuntimeSegment(len(session.segments), chunk.start_frame / SAMPLE_RATE,
                                     (chunk.start_frame + chunk.frame_count) / SAMPLE_RATE, str(path), captured_at=captured,
                                     start_frame=chunk.start_frame, end_frame=chunk.start_frame+chunk.frame_count,
                                     capture_completed_at=capture_completed_at or captured,
                                     capture_completed_monotonic_ns=capture_completed_monotonic_ns)
            transition(segment, 'captured', captured)
            session.queue_depth += 1
            segment.queued_at = clock()
            segment.queue_depth_when_queued = session.queue_depth
            session.max_queue_depth = max(session.max_queue_depth, session.queue_depth)
            transition(segment, 'queued', segment.queued_at)
            session.segments.append(segment)
            jobs.put_nowait((chunk, segment))
        producer_seen.add(path)
        expected_frame += chunk.frame_count
        tail_seen = chunk.frame_count < frames_per_chunk

    def consume() -> None:
        seen: set[Path] = set()
        frame = 0
        while True:
            job = jobs.get()
            try:
                if job is sentinel:
                    break
                chunk, segment = job
                with state_lock:
                    session.queue_depth -= 1
                    segment.asr_started_at = clock()
                    transition(segment, 'transcribing', segment.asr_started_at)
                result = None
                failure = None
                directory = output / f'segment-{segment.segment_id:03d}'
                try:
                    path = _validate_chunk(chunk, frame, seen, chunk_seconds=session.chunk_seconds)
                    seen.add(path)
                    directory.mkdir(exist_ok=False)
                    save()
                    try:
                        result = transcribe(path, directory)
                    finally:
                        with state_lock:
                            segment.asr_finished_at = clock()
                    if not isinstance(result, asr.ASRResult):
                        raise TypeError('invalid_asr_result')
                    evidence.finish_run(directory, result)
                    failure = result.error
                    if not failure and (result.http_status != 200 or not isinstance(result.transcript, str)
                                        or not result.transcript.strip()):
                        failure = 'invalid_asr_result'
                except BaseException:
                    # Never log exception contents: client errors may carry secrets.
                    failure = 'asr_or_evidence_failed'
                frame = chunk.start_frame + chunk.frame_count
                timing = {}
                try:
                    if (directory / 'timing.json').exists():
                        timing = json.loads((directory / 'timing.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    failure = 'request_timing_unreadable'
                with state_lock:
                    for name in ('asr_started_at', 'asr_finished_at', 'asr_started_monotonic_ns', 'asr_finished_monotonic_ns', 'request_count'):
                        if name in timing:
                            setattr(segment, name, timing[name])
                    segment.asr_finished_at = segment.asr_finished_at or clock()
                    if isinstance(result, asr.ASRResult):
                        segment.http_status = result.http_status
                        segment.asr_latency_seconds = result.latency_seconds
                    segment.error = failure
                    segment.raw_transcript = result.transcript if result is not None and not failure else None
                    transition(segment, 'failed' if failure else 'completed', segment.asr_finished_at)
                if on_segment is not None:
                    try:
                        on_segment(copy.deepcopy(segment))
                        with state_lock:
                            segment.transcript_printed_at = clock()
                            segment.transcript_printed_monotonic_ns = time.perf_counter_ns()
                    except Exception:
                        with state_lock:
                            session.error = 'live_output_failed'
                try:
                    save()
                except OSError:
                    with state_lock:
                        session.error = 'session_persistence_failed'
            finally:
                jobs.task_done()
        with state_lock:
            session.consumer_finished_at = clock()

    save()
    if on_session is not None:
        on_session(session.session_id, audio_dir, output)
    worker = threading.Thread(target=consume, name='asr-consumer', daemon=False)
    worker.start()
    try:
        capture_report = produce(publish, audio_dir)
        with state_lock:
            session.capture_report = capture_report
    except BaseException:
        with state_lock:
            session.error = 'producer_failed'
    finally:
        with state_lock:
            session.producer_finished_at = clock()
            capture = session.capture_report or {}
            session.capture_started_at = capture.get('recording_started_at_utc')
            session.capture_finished_at = (capture.get('reads') or [{}])[-1].get('completed_at_utc') or session.producer_finished_at
            session.status = 'draining'
        jobs.put_nowait(sentinel)
        # Persist draining state without retaining capture resources or blocking producer.
        try:
            save()
        finally:
            jobs.join()
            worker.join()
    with state_lock:
        session.segments.sort(key=lambda segment: segment.segment_id)
        session.finalized_at = clock()
        session.total_request_count = sum(s.request_count for s in session.segments)
        session.status = 'failed' if session.error else ('completed_with_errors'
            if any(s.status == 'failed' for s in session.segments) else 'completed')
    save()
    evidence.write_json(output / 'ordered-transcript.json', ordered_transcript(session))
    return session
