"""Browser-only bounded ASR recovery. Frozen CLI/transport remain unchanged."""
import json
import time
from dataclasses import replace

from . import evidence
from .concurrent_pipeline import siliconflow_asr, utc_now
from .stability import build_summary, SUMMARY_NAME

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1, 2)


def retryable(result):
    return (result.error in ('timeout_no_automatic_retry', 'transport_error_no_automatic_retry')
            or result.http_status in (408, 429, 500, 502, 503, 504))


def recovering_asr(settings, on_progress, *, sleep=time.sleep):
    invoke = siliconflow_asr(settings)

    def transcribe(path, directory):
        begin = time.perf_counter()
        timing = {'asr_started_at': utc_now(), 'asr_started_monotonic_ns': time.perf_counter_ns(),
                  'request_count': 0}
        attempts = []
        segment_id = int(path.stem.split('-')[-1])
        try:
            for number in range(1, MAX_ATTEMPTS+1):
                on_progress(asr_progress={'segment_id': segment_id, 'attempt': number,
                                          'max_attempts': MAX_ATTEMPTS, 'status': 'requesting'})
                folder = directory / f'attempt-{number:02d}'
                folder.mkdir()
                attempt_begin = time.perf_counter()
                try:
                    result = invoke(path, folder)
                except Exception:
                    # Never serialize arbitrary exceptions (may contain credentials).
                    if not (folder/'timing.json').exists():
                        raise
                    from .asr import ASRResult
                    result = ASRResult(None, time.perf_counter()-attempt_begin, error='local_asr_attempt_failed')
                finally:
                    if (folder/'timing.json').exists():
                        timing['request_count'] += 1
                evidence.finish_run(folder, result)
                attempts.append({'attempt': number, 'http_status': result.http_status,
                                 'latency_seconds': result.latency_seconds, 'error': result.error})
                if result.error is None or not retryable(result) or number == MAX_ATTEMPTS:
                    break
                on_progress(asr_progress={'segment_id': segment_id, 'attempt': number+1,
                                          'max_attempts': MAX_ATTEMPTS, 'status': 'backoff'})
                sleep(BACKOFF_SECONDS[number-1])
            return replace(result, latency_seconds=time.perf_counter()-begin)
        finally:
            timing.update(asr_finished_at=utc_now(), asr_finished_monotonic_ns=time.perf_counter_ns())
            evidence.write_json(directory/'timing.json', timing)
            evidence.write_json(directory/'retry.json', {'max_attempts': MAX_ATTEMPTS,
                'backoff_seconds': list(BACKOFF_SECONDS), 'attempts': attempts,
                'request_count': timing['request_count'], 'segment_elapsed_seconds': time.perf_counter()-begin})
            on_progress(asr_progress=None)
    return transcribe


def write_live_summary(session, audio_dir, output, *, capture_integrity=None):
    """Extend frozen metrics with actual attempts, not one-request-per-chunk assumptions."""
    summary = build_summary(session, audio_dir, output)
    results = [json.loads(p.read_text(encoding='utf-8'))
               for p in output.glob('segment-*/attempt-*/result.json')]
    count = session.total_request_count
    succeeded = sum(r['error'] is None and r['http_status'] == 200 for r in results)
    failed = len(results)-succeeded
    latencies = [r['latency_seconds'] for r in results]
    checks = {'request_count_matches_segments': count == sum(s.request_count for s in session.segments),
              'bounded_attempts_per_chunk': all(1 <= s.request_count <= MAX_ATTEMPTS for s in session.segments),
              'all_attempts_have_terminal_result': count == len(results),
              'latency_available_for_each_attempt': len(latencies) == count}
    summary.update(asr_requests_started=count, asr_requests_succeeded=succeeded,
                   asr_requests_failed=failed, asr_requests_unresolved=count-len(results),
                   request_checks=checks, automatic_retry_limit=MAX_ATTEMPTS-1,
                   recovered_segments=sum(s.status == 'completed' and s.request_count > 1 for s in session.segments),
                   min_asr_latency_seconds=min(latencies) if latencies else None,
                   mean_asr_latency_seconds=sum(latencies)/len(latencies) if latencies else None,
                   max_asr_latency_seconds=max(latencies) if latencies else None,
                   queue_drained=session.queue_depth == 0 and session.consumer_finished_at is not None
                                 and all(s.status in ('completed', 'failed') for s in session.segments))
    summary['checks'].update(request_accounting=all(checks.values()), no_failed_requests=failed == 0,
                             queue_drained=summary['queue_drained'])
    report = session.capture_report or {}
    if 'pause_events' in report:
        from .live_capture import pause_integrity
        summary['integrity'] = pause_integrity(session.segments, report['actual_captured_frames'], report)
        summary['pause_count'] = len(report['pause_events'])
        summary['time_basis'] = 'captured audio seconds; paused time excluded'
        summary['expected_requests_for_requested_frames'] = None
        summary['request_estimate_note'] = 'One initial request per actual chunk, plus bounded retries; pauses can add short chunks.'
        summary['checks'].pop('one_stream_open_close', None)
        summary['checks'].update(
            every_opened_stream_closed=report['stream_open_count'] == report['stream_close_count'],
            segments_integrity=summary['integrity']['ok'],
            existing_pcm_integrity_verified=bool(capture_integrity and capture_integrity['chunk_pcm_equals_full']
                                                  and capture_integrity['frame_sum_matches_full']))
    summary['observation_result'] = 'OK' if all(summary['checks'].values()) else 'ATTENTION'
    evidence.write_json(output/SUMMARY_NAME, summary)
    return summary
