"""Bounded one-stream PCM acquisition only; no ASR, credentials or network.

Keep at most 25 seconds in memory. Write files after closing the stream so disk
work cannot pause acquisition at chunk boundaries. This is not an ASR worker.
"""

import array
import hashlib
import json
import sys
import time
import wave
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .chunk_pipeline import CHUNK_SECONDS, SAMPLE_RATE, chunk_frames
from .recording import RecordingError, inspect_pcm_wav, prepare_recording, validate_recording


READ_FRAMES = 4410  # 100 ms; boundaries use frame counts, never elapsed time.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save(path: Path, report: dict) -> None:
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                         encoding='utf-8')
    temporary.replace(path)


def _write_wav(path: Path, pcm: bytes) -> None:
    with path.open('xb') as raw, wave.open(raw, 'wb') as wav:
        wav.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
        wav.writeframes(pcm)


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, SAMPLE_RATE, 'NONE'):
            raise RecordingError('Unexpected WAV format.')
        pcm = wav.readframes(wav.getnframes())
        if len(pcm) != wav.getnframes() * 2:
            raise RecordingError('Truncated PCM payload.')
        return pcm


def save_audio(pcm: bytes, audio_dir: Path) -> list[dict]:
    """Derive full/chunk/rejoined files from the same captured bytes, no DSP."""
    if not pcm or len(pcm) % 2:
        raise RecordingError('Empty or incomplete PCM samples.')
    _write_wav(audio_dir / 'full.wav', pcm)
    segments = []
    for segment_id, start in enumerate(range(0, len(pcm) // 2, CHUNK_SECONDS * SAMPLE_RATE)):
        part = pcm[start * 2:(start + CHUNK_SECONDS * SAMPLE_RATE) * 2]
        count = len(part) // 2
        path = audio_dir / f'chunk-{segment_id:03d}.wav'
        _write_wav(path, part)
        segments.append({'segment_id': segment_id, 'start_frame': start, 'frame_count': count,
                         'start_time': start / SAMPLE_RATE, 'end_time': (start + count) / SAMPLE_RATE,
                         'audio_path': str(path.resolve())})
    # Read actual chunk files back, rather than writing a second copy of pcm.
    _write_wav(audio_dir / 'chunks-rejoined.wav', b''.join(_read_pcm(Path(s['audio_path'])) for s in segments))
    return segments


def inspect_capture(audio_dir: Path, segments: list[dict], *, chunk_seconds: float = CHUNK_SECONDS) -> dict:
    """Read-only integrity checks; no sounddevice import or playback."""
    frames_per_chunk = chunk_frames(chunk_seconds)
    full = _read_pcm(audio_dir / 'full.wav')
    parts = [_read_pcm(Path(s['audio_path'])) for s in segments]
    joined = b''.join(parts)
    cursor = 0
    boundaries = True
    for index, (segment, pcm) in enumerate(zip(segments, parts)):
        count = len(pcm) // 2
        boundaries &= (segment['segment_id'] == index and segment['start_frame'] == cursor
                       and segment['frame_count'] == count and 0 < count <= frames_per_chunk
                       and segment['start_time'] == cursor / SAMPLE_RATE
                       and segment['end_time'] == (cursor + count) / SAMPLE_RATE
                       and (index == len(parts) - 1 or count == frames_per_chunk))
        cursor += count
    rejoined = _read_pcm(audio_dir / 'chunks-rejoined.wav')
    return {
        'full': inspect_pcm_wav(audio_dir / 'full.wav'),
        'chunks': [inspect_pcm_wav(Path(s['audio_path'])) for s in segments],
        'sum_chunk_frames': sum(len(p) // 2 for p in parts),
        'frame_sum_matches_full': cursor == len(full) // 2,
        'contiguous_frame_metadata': bool(segments) and bool(boundaries),
        'chunk_pcm_equals_full': joined == full,
        'rejoined_pcm_equals_full': rejoined == full,
        'full_pcm_sha256': hashlib.sha256(full).hexdigest(),
        'chunk_concat_pcm_sha256': hashlib.sha256(joined).hexdigest(),
        'rejoined_pcm_sha256': hashlib.sha256(rejoined).hexdigest(),
    }


def _acquire(config: dict, report: dict, blocks: list[bytes],
             on_pcm: Callable[[bytes], None] | None = None, *,
             stop_event=None, on_started: Callable[[], None] | None = None) -> None:
    import sounddevice as sd

    stream = None
    started = False
    if stop_event is not None and stop_event.is_set():
        return
    report['stream_open_attempts'] += 1
    try:
        stream = sd.RawInputStream(device=config['device_index'], samplerate=SAMPLE_RATE,
                                   channels=1, dtype='int16', latency='high', dither_off=True)
        report['stream_open_count'] += 1
        report['stream_parameters'] = {'sample_rate': stream.samplerate, 'channels': stream.channels,
                                       'sample_format': stream.dtype, 'input_latency_seconds': stream.latency}
        if (stream.samplerate, stream.channels, stream.dtype) != (SAMPLE_RATE, 1, 'int16'):
            raise RecordingError('Opened stream parameters do not match; no fallback.')
        stream.start()
        started = True
        report['recording_started_at_utc'] = _now()
        if on_started is not None:
            on_started()
        if config.get('collect_timing'):
            report['capture_started_monotonic_ns'] = time.perf_counter_ns()
        print(f"RECORDING NOW — keep speaking for {config['requested_duration_seconds']:g} seconds.", flush=True)
        begin = time.perf_counter()
        remaining = config['requested_frames']
        while remaining and not (stop_event is not None and stop_event.is_set()):
            count = min(READ_FRAMES, remaining)
            timing = {}
            if config.get('collect_timing'):
                timing = {'started_at_utc': _now(), 'started_monotonic_ns': time.perf_counter_ns()}
            data, overflowed = stream.read(count)
            if config.get('collect_timing'):
                timing.update(completed_monotonic_ns=time.perf_counter_ns(), completed_at_utc=_now())
            pcm = bytes(data)
            read_index = len(report['reads'])
            report['reads'].append({'read_index': read_index,
                                    'start_frame': report['actual_captured_frames'],
                                    'requested_frames': count, 'returned_bytes': len(pcm),
                                    'overflowed': bool(overflowed), **timing})
            if overflowed:
                report['portaudio_status_events'].append({'read_index': read_index,
                    'start_frame': report['actual_captured_frames'], 'overflowed': bool(overflowed),
                    'backend_report': 'RawInputStream.read returned overflowed=True'})
            if len(pcm) != count * 2:
                raise RecordingError(f'Incomplete read: requested {count * 2} bytes, got {len(pcm)}.')
            blocks.append(pcm)
            report['actual_captured_frames'] += count
            remaining -= count
            if on_pcm is not None:
                on_pcm(pcm)  # Ordinary blocking-read observer, NOT a PortAudio callback.
        report['read_elapsed_seconds'] = time.perf_counter() - begin
    finally:
        if stream is not None:
            try:
                if started:
                    stream.stop(ignore_errors=False)
            finally:
                report['stream_close_attempts'] += 1
                stream.close(ignore_errors=False)
                report['stream_close_count'] += 1
                report['stream_closed_at_utc'] = _now()
                print('Recording finished; microphone closed. Saving WAV files...', flush=True)


def capture_chunks(device: int, duration: float = 20, chunk_duration: float = 6,
                   recordings_root: Path | None = None, output_root: Path | None = None,
                   expected_name: str | None = None, expected_host_api: str | None = None) -> int:
    """Interactive bounded capture; Enter is mandatory before stream creation."""
    root = Path(__file__).resolve().parents[2]
    metadata = None
    report = {}
    try:
        validate_recording(device, duration)  # Reuse the frozen short-duration check.
        if chunk_duration != CHUNK_SECONDS:
            raise RecordingError('V0.4b requires --chunk-duration 6; overlap is zero.')
        if not sys.stdin.isatty():
            raise RecordingError('An interactive terminal is required; run this command yourself.')
        config = prepare_recording(device, duration, expected_name, expected_host_api)
        session_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid4().hex[:8]
        audio_dir = (recordings_root if recordings_root is not None else root / 'recordings/v0.4') / session_id
        evidence_dir = (output_root if output_root is not None else root / 'outputs/v0.4') / session_id
        audio_dir.mkdir(parents=True, exist_ok=False)
        evidence_dir.mkdir(parents=True, exist_ok=False)
        metadata = evidence_dir / 'capture-session.json'
        report = {'stage': 'V0.4b', 'session_id': session_id, 'created_at_utc': _now(), **config,
                  'chunk_duration_seconds': CHUNK_SECONDS, 'chunk_frames': CHUNK_SECONDS * SAMPLE_RATE,
                  'overlap_seconds': 0, 'state': 'waiting_for_user', 'user_confirmed_start': False,
                  'stream_open_attempts': 0, 'stream_open_count': 0, 'stream_close_attempts': 0,
                  'stream_close_count': 0, 'actual_captured_frames': 0, 'reads': [],
                  'portaudio_status_events': [], 'status_reporting': 'Blocking reads: overflowed boolean; no callback used.',
                  'audio_directory': str(audio_dir.resolve()), 'full_audio_path': str((audio_dir / 'full.wav').resolve()),
                  'rejoined_audio_path': str((audio_dir / 'chunks-rejoined.wav').resolve()), 'segments': [],
                  'processing': 'none; original PCM; all WAV files saved after single stream closes',
                  'asr_calls': 0, 'siliconflow_requests': 0, 'technical_result': 'NOT RUN',
                  'v04b_acceptance': 'WAITING FOR USER AUDIO REVIEW', 'v04_overall': 'NOT YET PASS'}
        _save(metadata, report)
        for label, value in [('Selected device index', device), ('Selected device name', config['device_name']),
                             ('Host API', config['host_api']), ('Sample rate', '44100 Hz'), ('Channels', 1),
                             ('Sample format', 'PCM 16-bit'), ('Requested duration', f'{duration:g} seconds'),
                             ('Chunk duration', '6 seconds / 264600 frames; overlap 0'),
                             ('Audio directory', audio_dir.resolve()), ('Session evidence', metadata.resolve())]:
            print(f'{label}: {value}', flush=True)
        try:
            answer = input('Press Enter when ready to start continuous capture (Ctrl+C to cancel). ')
        except (EOFError, KeyboardInterrupt):
            answer = 'cancel'
        if answer != '':
            report['state'] = 'cancelled'
            _save(metadata, report)
            print('Cancelled; microphone stream was not opened.')
            return 130
        report.update(user_confirmed_start=True, confirmed_at_utc=_now(), state='recording')
        _save(metadata, report)
        blocks = []
        acquisition_error = None
        try:
            _acquire(config, report, blocks)
        except (Exception, KeyboardInterrupt) as error:
            acquisition_error = str(error) or type(error).__name__
            report['capture_error'] = acquisition_error  # No credentials or HTTP objects in this path.
        # Persist stream status even if writing WAVs or later integrity checks fail.
        report['state'] = 'capture_failed' if acquisition_error else 'saving_audio'
        _save(metadata, report)
        if blocks:
            pcm = b''.join(blocks)
            if sys.byteorder != 'little':
                samples = array.array('h')
                samples.frombytes(pcm)
                samples.byteswap()
                pcm = samples.tobytes()
            report['segments'] = save_audio(pcm, audio_dir)
            report['integrity'] = inspect_capture(audio_dir, report['segments'])
            integrity = report['integrity']
            report['technical_checks'] = {
                'one_open_one_close': report['stream_open_count'] == report['stream_close_count'] == 1,
                'capture_completed': acquisition_error is None,
                'requested_frames_captured': report['actual_captured_frames'] == config['requested_frames'] == integrity['full']['total_frames'],
                'frame_sum_matches_full': integrity['frame_sum_matches_full'],
                'contiguous_frame_metadata': integrity['contiguous_frame_metadata'],
                'chunk_pcm_equals_full': integrity['chunk_pcm_equals_full'],
                'rejoined_pcm_equals_full': integrity['rejoined_pcm_equals_full'],
                'no_overflow_reported': not report['portaudio_status_events'],
                'no_clipping': integrity['full']['clipping_count'] == 0,
                'non_silent': integrity['full']['digital_non_silence'],
            }
        passed = bool(report.get('technical_checks')) and all(report['technical_checks'].values())
        report.update(state='completed' if passed else 'failed', technical_result='PASS' if passed else 'FAIL',
                      completed_at_utc=_now())
        _save(metadata, report)
        print('Capture finished. No ASR was called.', flush=True)
        for event in report['portaudio_status_events']:
            print('PortAudio status: ' + json.dumps(event, ensure_ascii=False))
        if acquisition_error:
            print(f'Capture error: {acquisition_error}', file=sys.stderr)
        print(f"Stream open/close count: {report['stream_open_count']}/{report['stream_close_count']}")
        print(f"Captured frames: {report['actual_captured_frames']}")
        for segment in report['segments']:
            print(f"Chunk {segment['segment_id']}: {segment['start_time']:g}–{segment['end_time']:g} s / {segment['frame_count']} frames / {segment['audio_path']}")
        if 'integrity' in report:
            print(f"Chunk PCM equals full PCM: {report['integrity']['chunk_pcm_equals_full']}")
        print(f"Full WAV: {report['full_audio_path']}")
        print(f"Technical capture result: {report['technical_result']}")
        print('V0.4b: WAITING FOR USER AUDIO REVIEW; V0.4: NOT YET PASS')
        return 0 if passed else 1
    except (Exception, KeyboardInterrupt) as error:
        if metadata is not None:
            report.update(state='failed', technical_result='FAIL', error=str(error) or type(error).__name__)
            try:
                _save(metadata, report)
            except OSError:
                print('Cannot update metadata; retain existing files. No automatic retry.', file=sys.stderr)
        print(f'Capture stopped: {str(error) or type(error).__name__}. No automatic retry.', file=sys.stderr)
        return 1
