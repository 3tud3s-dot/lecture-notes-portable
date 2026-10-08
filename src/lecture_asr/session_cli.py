"""V0.4c2 user-started CLI and runtime evidence; no automatic stage acceptance."""

import json
import sys
from pathlib import Path

from . import asr, chunk_capture, evidence, recording
from .concurrent_pipeline import continuous_producer, run_concurrent, siliconflow_asr
from .config import Settings
from .chunk_pipeline import chunk_frames


def inspect_session(session, audio_dir: Path, output: Path, *, stage: str = 'V0.4c2') -> dict:
    capture = session.capture_report
    if capture is None and (audio_dir / 'capture.json').exists():
        capture = json.loads((audio_dir / 'capture.json').read_text(encoding='utf-8'))
    capture = capture or {}
    reads = capture.get('reads', [])
    segments = sorted(session.segments, key=lambda s: s.segment_id)
    chunks = [{'segment_id': s.segment_id, 'start_frame': s.start_frame,
               'frame_count': s.end_frame-s.start_frame, 'start_time': s.start_time,
               'end_time': s.end_time, 'audio_path': s.audio_path} for s in segments]
    size = chunk_frames(session.chunk_seconds)
    integrity = chunk_capture.inspect_capture(audio_dir, chunks, chunk_seconds=session.chunk_seconds)
    start = capture.get('capture_started_monotonic_ns')
    end = reads[-1].get('completed_monotonic_ns') if reads else None
    relative = lambda t: (t-start)/1e9 if t is not None and start is not None else None
    rows = []
    for s in segments:
        overlapping_reads = [r['read_index'] for r in reads
            if s.asr_started_monotonic_ns is not None and s.asr_finished_monotonic_ns is not None
            and r.get('started_monotonic_ns') is not None and r.get('completed_monotonic_ns') is not None
            and r['start_frame'] >= s.end_frame
            and s.asr_started_monotonic_ns <= r['started_monotonic_ns']
            and r['completed_monotonic_ns'] <= s.asr_finished_monotonic_ns]
        rows.append({'segment_id': s.segment_id, 'start_frame': s.start_frame, 'end_frame': s.end_frame,
                     'capture_done_seconds': relative(s.capture_completed_monotonic_ns),
                     'asr_start_seconds': relative(s.asr_started_monotonic_ns),
                     'asr_done_seconds': relative(s.asr_finished_monotonic_ns),
                     'http_status': s.http_status, 'asr_latency_seconds': s.asr_latency_seconds,
                     'queue_depth_at_enqueue': s.queue_depth_when_queued, 'request_count': s.request_count,
                     'raw_transcript': s.raw_transcript, 'status': s.status,
                     'later_audio_reads_completed_during_asr': overlapping_reads})
    early = [s.segment_id for s in segments if s.status == 'completed' and end is not None
             and s.asr_finished_monotonic_ns is not None and s.asr_finished_monotonic_ns < end]
    live = [s.segment_id for s in segments if s.status == 'completed' and end is not None
            and s.transcript_printed_monotonic_ns is not None and s.transcript_printed_monotonic_ns < end]
    expected = capture.get('requested_frames', 0)
    checks = {
        'one_open_one_close': capture.get('stream_open_count') == capture.get('stream_close_count') == 1,
        'requested_frames_captured': expected > 0 and capture.get('actual_captured_frames') == integrity['full']['total_frames'] == expected,
        'exact_chunk_count': len(segments) == (expected+size-1)//size,
        'ordered_unique_ids': [s.segment_id for s in segments] == list(range(len(segments))),
        'unique_audio_paths': len({s.audio_path for s in segments}) == len(segments),
        'frame_metadata_contiguous': integrity['contiguous_frame_metadata'],
        'frame_sum_matches': integrity['frame_sum_matches_full'],
        'pcm_exact_equality': integrity['chunk_pcm_equals_full'] and integrity['rejoined_pcm_equals_full'],
        'no_overflow': not capture.get('portaudio_status_events') and not any(r['overflowed'] for r in reads),
        'no_clipping': integrity['full']['clipping_count'] == 0,
        'non_silent': integrity['full']['digital_non_silence'],
        'one_request_per_chunk': bool(segments) and all(s.request_count == 1 for s in segments),
        'all_asr_completed': bool(segments) and all(s.status == 'completed' and s.http_status == 200
                             and s.raw_transcript and s.raw_transcript.strip() for s in segments),
        'queue_drained': session.queue_depth == 0 and session.consumer_finished_at is not None,
        'session_no_error': session.status == 'completed' and session.error is None,
    }
    concurrent = any(r['later_audio_reads_completed_during_asr'] for r in rows)
    technical = 'FAIL' if not all(checks.values()) else ('PASS' if concurrent and early and live else 'BLOCKED')
    latencies = [s.asr_latency_seconds for s in segments if s.asr_latency_seconds is not None]
    summary = {'stage': 'V0.4c2', 'session_id': session.session_id, 'checks': checks, 'integrity': integrity,
               'timing_table': rows, 'timing_origin': 'capture_started_monotonic_ns; seconds relative to stream start',
               'capture_finished_seconds': relative(end), 'recording_continued_while_asr_running': bool(concurrent),
               'early_returned_segment_ids': early, 'live_printed_before_capture_end_segment_ids': live,
               'mean_asr_latency_seconds': sum(latencies)/len(latencies) if latencies else None,
               'maximum_queue_depth': session.max_queue_depth, 'final_queue_depth': session.queue_depth,
               'request_attempts': session.total_request_count, 'automatic_retries': 0,
               'technical_result': technical, 'v04_final_acceptance': 'WAITING FOR USER / CHATGPT REVIEW',
               'v04_overall': 'NOT YET PASS', 'v05_started': False,
               'limitation': 'Timing brackets the existing ASR client invocation, not packet-level tracing; read completion evidence is not an independent hardware sample audit.'}
    if stage in ('V0.5b', 'V0.5c', 'V0.5d'):
        # Preserve every legacy check; one API failure is not a stability verdict.
        summary.update(stage=stage, strict_all_success_diagnostic=technical,
                       technical_result='WAITING FOR EVIDENCE REVIEW', v05_overall='NOT YET PASS',
                       user_sanity_review='pending')
        for name in ('v04_final_acceptance', 'v04_overall', 'v05_started'):
            summary.pop(name)
    evidence.write_json(output / 'runtime-summary.json', summary)
    table = '| Segment | Capture done | ASR start | ASR done | Latency | Queue depth | HTTP |\n|---|---:|---:|---:|---:|---:|---:|\n'
    fmt = lambda value: f'{value:.3f}' if value is not None else 'unknown'
    for r in rows:
        table += f"| {r['segment_id']} | {fmt(r['capture_done_seconds'])} | {fmt(r['asr_start_seconds'])} | {fmt(r['asr_done_seconds'])} | {fmt(r['asr_latency_seconds'])} | {r['queue_depth_at_enqueue']} | {r['http_status']} |\n"
    (output / 'timing-table.md').write_text('Times are seconds relative to capture start.\n\n'+table, encoding='utf-8')
    return summary


def transcribe_session(args, settings: Settings) -> int:
    root = Path(__file__).resolve().parents[2]
    stability_run = args.duration > 30 or args.chunk_duration == 12
    stability_stage = 'V0.5d' if args.chunk_duration == 12 else ('V0.5c' if args.duration > 120 else 'V0.5b')
    stage_directory = stability_stage.lower() if stability_run else 'v0.4'
    try:
        asr.validate_settings(settings)
        if settings.transcription_endpoint != 'https://api.siliconflow.cn/v1/audio/transcriptions' or settings.asr_model != 'Qwen/Qwen3-ASR-1.7B':
            raise ValueError('This session requires the approved SiliconFlow endpoint and model.')
        recording.validate_recording(args.device, args.duration, max_duration=300)
        size = chunk_frames(args.chunk_duration)
        if not sys.stdin.isatty():
            raise ValueError('Run in an interactive terminal and press Enter yourself.')
        config = recording.prepare_recording(args.device, args.duration, args.expect_name, args.expect_host_api, max_duration=300)
        if args.chunk_duration != 6:
            config['chunk_duration_seconds'] = int(args.chunk_duration)
        print(f"Microphone: {config['device_name']} (index {config['device_index']}) / {config['host_api']}", flush=True)
        print(f'Duration: {args.duration:g} seconds; chunk duration: {args.chunk_duration:g} seconds; overlap: 0', flush=True)
        print('Audio: 44100 Hz / mono / PCM16; no resampling', flush=True)
        print(f'ASR model: {settings.asr_model}\nEndpoint: {settings.transcription_endpoint}', flush=True)
        print(f'Expected requests: {(config["requested_frames"]+size-1)//size}; one per chunk, no retry.', flush=True)
        try:
            answer = input('Press Enter when ready (Ctrl+C to cancel). ')
        except (EOFError, KeyboardInterrupt):
            answer = 'cancel'
        if answer != '':
            print('Cancelled; no recording or ASR request.')
            return 130
        locations = {}
        def started(session_id, audio_dir, output):
            locations.update(audio=audio_dir, output=output)
            print(f'Session: {session_id}\nAudio directory: {audio_dir.resolve()}\nSession evidence: {output.resolve()}', flush=True)
        def display(segment):
            stamp = lambda seconds: f'{int(seconds)//60:02d}:{int(seconds)%60:02d}'
            header = f'[{stamp(segment.start_time)}–{stamp(segment.end_time)}]'
            if segment.status == 'completed':
                # A single write preserves RAW content; only framing adds newlines.
                sys.stdout.write('\n'+header+'\n'+segment.raw_transcript+'\n')
            else:
                sys.stdout.write(f'\n{header}\nASR failed: {segment.error}; WAV retained.\n')
            sys.stdout.flush()
        session = run_concurrent(continuous_producer(config, collect_timing=True), siliconflow_asr(settings),
                                 args.recordings_dir or root/'recordings'/stage_directory, args.output_dir or root/'outputs'/stage_directory,
                                 on_segment=display, on_session=started, chunk_seconds=args.chunk_duration)
        try:
            if stability_run:
                summary = inspect_session(session, locations['audio'], locations['output'], stage=stability_stage)
            else:
                summary = inspect_session(session, locations['audio'], locations['output'])
        finally:
            # Post-session observation only; never changes capture, ASR or exit status.
            try:
                from .stability import format_summary, write_summary
                print(format_summary(write_summary(session, locations['audio'], locations['output'])), flush=True)
            except Exception:
                print('Stability summary unavailable; original session evidence retained.', file=sys.stderr)
        print(f"Request attempts: {summary['request_attempts']}; max queue depth: {summary['maximum_queue_depth']}; final depth: {summary['final_queue_depth']}")
        print(f"Recording continued during ASR: {summary['recording_continued_while_asr_running']}")
        print(f"Early returned segments: {summary['early_returned_segment_ids']}")
        print(f"Live printed before capture ended: {summary['live_printed_before_capture_end_segment_ids']}")
        print((locations['output']/'timing-table.md').read_text(encoding='utf-8'))
        if stability_run:
            print(f"Strict all-success diagnostic: {summary['strict_all_success_diagnostic']}; review failures and continuity together.")
            print(f'{stability_stage} technical result: WAITING FOR EVIDENCE REVIEW')
            next_stage = 'V0.6' if stability_stage == 'V0.5d' else ('V0.5d' if stability_stage == 'V0.5c' else 'V0.5c')
            print(f'V0.5 overall: NOT YET PASS; user sanity review pending; {next_stage} not started.')
            return 0 if session.status in ('completed', 'completed_with_errors') else 1
        print(f"V0.4c2 technical result: {summary['technical_result']}")
        print('V0.4 final acceptance: WAITING FOR USER / CHATGPT REVIEW; V0.5 not started.')
        return 0 if summary['technical_result'] == 'PASS' else 1
    except (Exception, KeyboardInterrupt) as error:
        print(evidence.redact(f'Session stopped: {str(error) or type(error).__name__}. Preserve files; no automatic retry.', settings.api_key or ''), file=sys.stderr)
        return 1
