"""Browser pause/resume capture using the existing PortAudio acquisition primitive.

Only a chunk-sized buffer is retained. Paused time is excluded from frame time.
The frozen CLI continues to use its original continuous_producer.
"""
import array
import hashlib
import math
import sys
import wave

from . import chunk_capture, evidence
from .chunk_pipeline import AudioChunk, SAMPLE_RATE, chunk_frames
from .recording import RecordingError

MAX_DURATION = 6000


class DiscardBlocks:
    def append(self, _pcm):
        pass  # _acquire's on_pcm writes the same samples to disk instead.


class CaptureControl:
    def __init__(self, stop, pause):
        self.stop, self.pause = stop, pause

    def is_set(self):
        return self.stop.is_set() or self.pause.is_set()


def pausable_producer(config, controller):
    size = chunk_frames(config['chunk_duration_seconds'])

    def produce(publish, directory):
        report = dict(config, stream_open_attempts=0, stream_open_count=0,
                      stream_close_attempts=0, stream_close_count=0, actual_captured_frames=0,
                      reads=[], portaudio_status_events=[], pause_events=[], intervals=[],
                      chunk_boundaries=[], time_basis='captured audio seconds; pauses excluded')
        pending = bytearray()
        cursor = segment_id = 0
        squared = peak = clipping = nonzero = 0
        full_hash = hashlib.sha256()
        last_progress = -1
        interval = None

        def emit(reason):
            nonlocal cursor, segment_id
            if not pending:
                return
            pcm = bytes(pending); pending.clear()
            path = directory/f'chunk-{segment_id:03d}.wav'
            chunk_capture._write_wav(path, pcm)
            full.writeframes(b'')  # Keep full.wav header current at every committed boundary.
            frames = len(pcm)//2
            report['chunk_boundaries'].append({'segment_id': segment_id, 'end_frame':cursor+frames,
                                               'reason': reason})
            timing = interval['reads'][-1]
            publish(AudioChunk(path,cursor,frames), capture_completed_at=timing['completed_at_utc'],
                    capture_completed_monotonic_ns=timing['completed_monotonic_ns'])
            cursor += frames; segment_id += 1

        def accept(pcm):
            nonlocal squared, peak, clipping, nonzero, last_progress
            samples = array.array('h'); samples.frombytes(pcm)
            squared += sum(x*x for x in samples)
            peak = max(peak, max(abs(x) for x in samples))
            clipping += sum(x in (-32768,32767) for x in samples)
            nonzero += sum(x != 0 for x in samples)
            if sys.byteorder != 'little':
                samples.byteswap(); pcm = samples.tobytes()
            full.writeframesraw(pcm); full_hash.update(pcm)
            report['actual_captured_frames'] += len(pcm)//2
            offset = 0
            while offset < len(pcm):
                take = min(size*2-len(pending),len(pcm)-offset)
                pending.extend(pcm[offset:offset+take]); offset += take
                if len(pending) == size*2:
                    emit('full')
            seconds = report['actual_captured_frames']/SAMPLE_RATE
            if int(seconds) != last_progress:
                last_progress = int(seconds)
                controller.update(captured_duration=seconds)

        try:
            with (directory/'full.wav').open('xb') as raw, wave.open(raw,'wb') as full:
                full.setparams((1,2,SAMPLE_RATE,0,'NONE','not compressed'))
                while report['actual_captured_frames'] < config['requested_frames'] and not controller.stop_event.is_set():
                    start_frame = report['actual_captured_frames']
                    interval = dict(stream_open_attempts=0,stream_open_count=0,stream_close_attempts=0,
                                    stream_close_count=0,actual_captured_frames=0,reads=[],portaudio_status_events=[])
                    remaining = config['requested_frames']-start_frame
                    current = dict(config,requested_frames=remaining,requested_duration_seconds=remaining/SAMPLE_RATE,
                                   collect_timing=True)
                    try:
                        chunk_capture._acquire(current,interval,DiscardBlocks(),accept,
                            stop_event=CaptureControl(controller.stop_event,controller.pause_event),
                            on_started=controller.recording_started)
                    finally:
                        paused = controller.pause_event.is_set() and not controller.stop_event.is_set()
                        emit('pause' if paused else 'end')
                        for key in ('stream_open_attempts','stream_open_count','stream_close_attempts','stream_close_count'):
                            report[key] += interval[key]
                        base_index = len(report['reads'])
                        for read in interval['reads']:
                            report['reads'].append(dict(read,read_index=base_index+read['read_index'],
                                                        start_frame=start_frame+read['start_frame']))
                        for event in interval['portaudio_status_events']:
                            report['portaudio_status_events'].append(dict(event,read_index=base_index+event['read_index'],
                                                                         start_frame=start_frame+event['start_frame']))
                        if interval.get('recording_started_at_utc'):
                            report.setdefault('recording_started_at_utc',interval['recording_started_at_utc'])
                        report['intervals'].append({'start_frame':start_frame,'end_frame':report['actual_captured_frames'],
                            'opened_at':interval.get('recording_started_at_utc'), 'closed_at':interval.get('stream_closed_at_utc'),
                            'reason':'pause' if paused else 'end'})
                        full.writeframes(b'')  # Update the WAV header at each pause/end.
                        evidence.write_json(directory/'capture.json',report)
                    if report['portaudio_status_events']:
                        raise RecordingError('capture_overflow')
                    if paused and report['actual_captured_frames'] < config['requested_frames']:
                        report['pause_events'].append({'at':chunk_capture._now(),'frame':report['actual_captured_frames']})
                        evidence.write_json(directory/'capture.json',report)
                        controller.capture_paused()
                        with controller.condition:
                            controller.condition.wait_for(lambda: controller.stop_event.is_set() or not controller.pause_event.is_set())
        finally:
            report.update(full_pcm_sha256=full_hash.hexdigest(), pcm_statistics={
                'peak_amplitude_pcm':peak,'rms_pcm':math.sqrt(squared/report['actual_captured_frames']) if report['actual_captured_frames'] else 0,
                'clipping_count':clipping,'nonzero_sample_count':nonzero})
            evidence.write_json(directory/'capture.json',report)
            # Incremental copies: never join a whole classroom's PCM in RAM.
            if (directory/'full.wav').exists():
                with (directory/'chunks-rejoined.wav').open('xb') as raw, wave.open(raw,'wb') as joined:
                    joined.setparams((1,2,SAMPLE_RATE,0,'NONE','not compressed'))
                    for boundary in report['chunk_boundaries']:
                        with wave.open(str(directory/f"chunk-{boundary['segment_id']:03d}.wav"),'rb') as part:
                            while pcm := part.readframes(65536):
                                joined.writeframesraw(pcm)
            controller.update(captured_duration=report['actual_captured_frames']/SAMPLE_RATE)
        return report
    return produce


def inspect_streaming(directory, segments, report):
    def digest(path):
        sha=hashlib.sha256()
        with wave.open(str(path),'rb') as wav:
            if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getcomptype()) != (1,2,SAMPLE_RATE,'NONE'):
                raise RecordingError('Unexpected WAV format')
            total=0
            while pcm := wav.readframes(65536):
                sha.update(pcm); total += len(pcm)
            if total != wav.getnframes()*2: raise RecordingError('Truncated WAV')
        return sha.hexdigest(),total//2
    joined=hashlib.sha256(); frames=0
    for segment in segments:
        with wave.open(str(segment.audio_path),'rb') as wav:
            if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getcomptype()) != (1,2,SAMPLE_RATE,'NONE'):
                raise RecordingError('Unexpected chunk WAV format')
            count=0
            while pcm := wav.readframes(65536):
                joined.update(pcm); frames += len(pcm)//2; count += len(pcm)//2
            if count != wav.getnframes() or count != segment.end_frame-segment.start_frame:
                raise RecordingError('Chunk frame count mismatch')
    full_hash,full_frames=digest(directory/'full.wav')
    rejoined_hash,rejoined_frames=digest(directory/'chunks-rejoined.wav')
    return {'full':{'total_frames':full_frames,'actual_duration_seconds':full_frames/SAMPLE_RATE,
                    'channels':1,'sample_rate_hz':SAMPLE_RATE,'bits_per_sample':16,**report.get('pcm_statistics',{})},
            'sum_chunk_frames':frames,'frame_sum_matches_full':frames==full_frames==report['actual_captured_frames'],
            'chunk_pcm_equals_full':joined.hexdigest()==full_hash,
            'rejoined_pcm_equals_full':rejoined_hash==full_hash and rejoined_frames==full_frames,
            'full_pcm_sha256':full_hash,'chunk_concat_pcm_sha256':joined.hexdigest(),
            'rejoined_pcm_sha256':rejoined_hash}


def pause_integrity(segments, total, report):
    """Short chunks may occur ONLY at a recorded pause boundary or final end."""
    cursor=0; gaps=[]; overlaps=[]; sizes=True
    pause_frames={p['frame'] for p in report.get('pause_events',[])}
    for s in segments:
        if s.start_frame>cursor: gaps.append({'start_frame':cursor,'end_frame':s.start_frame})
        if s.start_frame<cursor: overlaps.append({'start_frame':s.start_frame,'end_frame':cursor})
        count=s.end_frame-s.start_frame
        sizes &= 0<count<=12*SAMPLE_RATE and (count==12*SAMPLE_RATE or s.end_frame in pause_frames or s.end_frame==total)
        cursor=s.end_frame
    if cursor<total: gaps.append({'start_frame':cursor,'end_frame':total})
    ids=[s.segment_id for s in segments]
    duplicates=sorted({i for i in ids if ids.count(i)>1})
    expected=list(range(len(report.get('chunk_boundaries',[]))))
    missing=sorted(set(expected)-set(ids))
    checks={'ids_exactly_0_to_n_minus_1':ids==expected,'no_missing_id':not missing,
            'no_duplicate_id':not duplicates,'no_frame_gaps':not gaps,'no_frame_overlaps':not overlaps,
            'valid_chunk_and_pause_boundaries':bool(sizes),'frames_cover_actual_capture':cursor==total}
    return {'checks':checks,'ok':all(checks.values()),'missing_segment_ids':missing,
            'duplicate_segment_ids':duplicates,'frame_gaps':gaps,'frame_overlaps':overlaps,
            'sum_segment_frames':sum(s.end_frame-s.start_frame for s in segments)}
