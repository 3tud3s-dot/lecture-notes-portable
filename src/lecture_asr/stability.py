"""Read-only post-session metrics. No capture, HTTP, config, or worker changes."""

import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

from .chunk_pipeline import CHUNK_SECONDS, SAMPLE_RATE, chunk_frames


SUMMARY_NAME = 'stability-summary.json'


def expected_requests(duration: float, *, chunk_seconds: float = CHUNK_SECONDS) -> int:
    """Selected zero-overlap cadence, including a nonempty tail."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Duration must be positive and finite.')
    frames = round(duration * SAMPLE_RATE)
    if frames <= 0:
        raise ValueError('Duration must contain at least one frame.')
    size = chunk_frames(chunk_seconds)
    return (frames + size - 1) // size


def directory_usage(directory: Path, *, exclude: tuple[str, ...] = ()) -> dict:
    """Logical file bytes, not filesystem allocated blocks; never follow links."""
    if not directory.is_dir():
        return {'available': False, 'bytes': None, 'wav_bytes': None, 'file_count': 0}
    size = wav_size = count = 0
    skipped_links = 0
    for path in directory.rglob('*'):
        if path.is_symlink():
            skipped_links += 1
            continue
        if path.is_file() and path.name not in exclude:
            value = path.stat().st_size
            size += value
            count += 1
            if path.suffix.lower() == '.wav':
                wav_size += value
    return {'available': True, 'bytes': size, 'wav_bytes': wav_size,
            'file_count': count, 'skipped_symlinks': skipped_links}


def _elapsed(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if a.tzinfo is None or b.tzinfo is None:
            return None
        value = (b-a).total_seconds()
        return value if value >= 0 else None
    except (ValueError, TypeError):
        return None


def segment_integrity(segments, total_frames: int | None, *, chunk_seconds: float = CHUNK_SECONDS) -> dict:
    size = chunk_frames(chunk_seconds)
    expected = ((total_frames + size - 1)//size
                if type(total_frames) is int and total_frames >= 0 else None)
    ids = [s.segment_id for s in segments]
    valid_ids = [i for i in ids if type(i) is int and i >= 0]
    counts = Counter(valid_ids)
    missing = sorted(set(range(expected))-set(valid_ids)) if expected is not None else None
    unexpected = sorted(set(valid_ids)-set(range(expected))) if expected is not None else None
    duplicates = sorted(i for i, count in counts.items() if count > 1)
    gaps, overlaps, invalid = [], [], []
    cursor = 0
    previous_start = -1
    monotonic = True
    frame_sum = 0
    exact_sizes = expected is not None
    for position, s in enumerate(segments):
        start, end = s.start_frame, s.end_frame
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            invalid.append(position)
            exact_sizes = False
            continue
        monotonic &= start >= previous_start
        previous_start = start
        if start > cursor:
            gaps.append({'start_frame': cursor, 'end_frame': start})
        if start < cursor:
            overlaps.append({'start_frame': start, 'end_frame': min(cursor, end)})
        cursor = max(cursor, end)
        frame_sum += end-start
        exact_sizes &= (type(s.segment_id) is int and expected is not None
                        and start == s.segment_id*size
                        and end == min(start+size, total_frames))
    if expected is not None and cursor < total_frames:
        gaps.append({'start_frame': cursor, 'end_frame': total_frames})
    ids_exact = expected is not None and len(valid_ids) == len(ids) and ids == list(range(expected))
    checks = {
        'ids_exactly_0_to_n_minus_1': ids_exact,
        'no_duplicate_id': not duplicates and len(valid_ids) == len(ids),
        'no_missing_id': missing == [],
        'no_unexpected_id': unexpected == [],
        'valid_frame_ranges': not invalid,
        'monotonic_frame_ranges': bool(monotonic),
        'no_frame_gaps': not gaps,
        'no_frame_overlaps': not overlaps,
        f'exact_{chunk_seconds:g}_second_core_and_tail': bool(exact_sizes),
        'frames_cover_actual_capture': expected is not None and frame_sum == cursor == total_frames,
    }
    return {'checks': checks, 'ok': all(checks.values()), 'expected_chunks_from_actual_frames': expected,
            'missing_segment_ids': missing, 'unexpected_segment_ids': unexpected,
            'duplicate_segment_ids': duplicates, 'duplicate_segment_count': sum(n-1 for n in counts.values()),
            'invalid_frame_positions': invalid, 'frame_gaps': gaps, 'frame_overlaps': overlaps,
            'sum_segment_frames': frame_sum}


def build_summary(session, audio_dir: Path, output: Path) -> dict:
    """Derive metrics from existing evidence after the producer/consumer finish.

    A missing capture report is unknown, never silently reported as zero success.
    This does not rerun PCM comparison; the existing runtime summary owns that.
    """
    capture = session.capture_report
    if capture is None and (audio_dir/'capture.json').is_file():
        capture = json.loads((audio_dir/'capture.json').read_text(encoding='utf-8'))
    capture = capture or {}
    segments = session.segments
    total = capture.get('actual_captured_frames')
    rate = capture.get('sample_rate_hz')
    frames_known = type(total) is int and total >= 0
    reads = capture.get('reads', [])
    # On failed capture, session.capture_finished_at may be producer-finally time.
    # Prefer the final recorded read timestamp from the existing capture report.
    finished = (reads[-1].get('completed_at_utc') if reads else None) or session.capture_finished_at
    drain = _elapsed(finished, session.consumer_finished_at)
    size = chunk_frames(session.chunk_seconds)
    integrity = segment_integrity(segments, total, chunk_seconds=session.chunk_seconds)
    succeeded = sum(s.request_count > 0 and s.status == 'completed' and s.http_status == 200
                    and isinstance(s.raw_transcript, str) and bool(s.raw_transcript.strip()) for s in segments)
    failed = sum(s.request_count > 0 and s.status == 'failed' for s in segments)
    started = session.total_request_count
    unresolved = started-succeeded-failed
    latencies = [s.asr_latency_seconds for s in segments
                 if s.request_count > 0 and s.asr_latency_seconds is not None
                 and math.isfinite(s.asr_latency_seconds) and s.asr_latency_seconds >= 0]
    audio_usage = directory_usage(audio_dir)
    evidence_usage = directory_usage(output, exclude=(SUMMARY_NAME, SUMMARY_NAME+'.tmp'))
    runtime = {}
    if (output/'runtime-summary.json').is_file():
        runtime = json.loads((output/'runtime-summary.json').read_text(encoding='utf-8')).get('checks', {})
    request_checks = {
        'request_count_matches_segments': started == sum(s.request_count for s in segments),
        'one_request_per_chunk': all(s.request_count == 1 for s in segments),
        'all_attempts_have_terminal_result': unresolved == 0 and all(s.status in ('completed', 'failed') for s in segments),
        'latency_available_for_each_attempt': len(latencies) == started,
    }
    queue_drained = (session.queue_depth == 0 and session.consumer_finished_at is not None
                     and request_checks['all_attempts_have_terminal_result'])
    events = capture.get('portaudio_status_events')
    requested = capture.get('requested_duration_seconds')
    requested_frames = capture.get('requested_frames')
    checks = {
        'capture_metadata_available': bool(capture),
        'requested_capture_completed': frames_known and type(requested_frames) is int and total == requested_frames and total > 0,
        'approved_format': rate == SAMPLE_RATE and capture.get('channels') == 1 and capture.get('sample_format') == 'int16',
        'approved_cadence': session.chunk_seconds in (6, 12) and session.overlap_seconds == 0,
        'one_stream_open_close': capture.get('stream_open_count') == capture.get('stream_close_count') == 1,
        'no_overflow': events == [] and not any(r.get('overflowed') for r in reads),
        'segments_integrity': integrity['ok'],
        'existing_pcm_integrity_verified': runtime.get('pcm_exact_equality') is True and runtime.get('frame_sum_matches') is True,
        'request_accounting': all(request_checks.values()),
        'no_failed_requests': failed == 0 and not any(s.status == 'failed' for s in segments),
        'queue_drained': queue_drained,
        'drain_timing_available': drain is not None,
        'directories_available': audio_usage['available'] and evidence_usage['available'],
        'session_completed_without_error': session.status == 'completed' and session.error is None,
    }
    return {
        'scope': 'stability instrumentation; not human acceptance or a stage PASS',
        'session_id': session.session_id,
        'source_evidence': {'session': 'session.json', 'capture': str(audio_dir/'capture.json'),
                            'pcm_integrity': 'runtime-summary.json',
                            'chunk_fields': 'session.json:segments; frame_count = end_frame - start_frame; RAW text unchanged'},
        'requested_duration_seconds': requested,
        'actual_capture_duration_seconds': total/rate if frames_known and rate == SAMPLE_RATE else None,
        'capture_read_elapsed_seconds': capture.get('read_elapsed_seconds'),
        'total_frames': total, 'chunk_duration_seconds': session.chunk_seconds, 'overlap_seconds': session.overlap_seconds,
        'total_chunks': len(segments),
        'expected_requests_for_requested_frames': (requested_frames+size-1)//size
            if type(requested_frames) is int and requested_frames > 0 else None,
        'stream_open_count': capture.get('stream_open_count'), 'stream_close_count': capture.get('stream_close_count'),
        'overflow_status_event_count': len(events) if isinstance(events, list) else None,
        'overflow_read_count': sum(bool(r.get('overflowed')) for r in reads),
        'asr_requests_started': started, 'asr_requests_succeeded': succeeded, 'asr_requests_failed': failed,
        'asr_requests_unresolved': unresolved, 'failed_segments': sum(s.status == 'failed' for s in segments),
        'request_checks': request_checks,
        'min_asr_latency_seconds': min(latencies) if latencies else None,
        'mean_asr_latency_seconds': sum(latencies)/len(latencies) if latencies else None,
        'max_asr_latency_seconds': max(latencies) if latencies else None,
        'latency_scope': 'all attempted requests with recorded finite latency, including failures',
        'max_queue_depth': session.max_queue_depth, 'final_queue_depth': session.queue_depth,
        'capture_finished_at': finished, 'consumer_finished_at': session.consumer_finished_at,
        'queue_drain_duration_seconds': drain, 'queue_drained': queue_drained,
        'drain_time_basis': 'UTC consumer finish minus last capture read (fallback session capture finish); includes post-capture saving; negative/unknown is null',
        'integrity': integrity,
        'resources': {'bytes_written_audio': audio_usage['wav_bytes'], 'audio_directory': audio_usage,
                      'evidence_directory': evidence_usage,
                      'byte_count_scope': 'logical file bytes at finalization; WAV bytes include full, chunks and rejoined copies; evidence excludes this summary and its temporary file',
                      'process_memory': 'process memory not instrumented'},
        'checks': checks, 'observation_result': 'OK' if all(checks.values()) else 'ATTENTION',
    }


def write_summary(session, audio_dir: Path, output: Path) -> dict:
    summary = build_summary(session, audio_dir, output)
    target = output/SUMMARY_NAME
    temporary = output/(SUMMARY_NAME+'.tmp')
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    temporary.replace(target)
    return summary


def format_summary(summary: dict) -> str:
    show = lambda value: 'unknown' if value is None else f'{value:g}'
    missing = summary['integrity']['missing_segment_ids']
    return '\n'.join([
        'Stability summary (not stage acceptance):',
        f"Session duration: {show(summary['actual_capture_duration_seconds'])} s (requested {show(summary['requested_duration_seconds'])} s)",
        f"Chunks: {summary['total_chunks']}",
        f"Successful ASR: {summary['asr_requests_succeeded']}/{summary['asr_requests_started']}",
        f"Failed ASR: {summary['asr_requests_failed']}; unresolved: {summary['asr_requests_unresolved']}",
        f"Max queue depth: {summary['max_queue_depth']}; final: {summary['final_queue_depth']}",
        f"API latency min/mean/max: {show(summary['min_asr_latency_seconds'])} / {show(summary['mean_asr_latency_seconds'])} / {show(summary['max_asr_latency_seconds'])} s",
        f"Overflow events: {show(summary['overflow_status_event_count'])}",
        f"Missing chunks: {len(missing) if missing is not None else 'unknown'}; duplicate chunks: {summary['integrity']['duplicate_segment_count']}",
        f"Frame gaps: {len(summary['integrity']['frame_gaps'])}; overlaps: {len(summary['integrity']['frame_overlaps'])}",
        f"Queue drained: {'yes' if summary['queue_drained'] else 'no'}; post-capture drain: {show(summary['queue_drain_duration_seconds'])} s",
        f"Audio bytes: {show(summary['resources']['bytes_written_audio'])}; evidence bytes (excluding summary): {show(summary['resources']['evidence_directory']['bytes'])}",
        'Process memory: not instrumented',
        f"Instrumentation checks: {summary['observation_result']}",
    ])
