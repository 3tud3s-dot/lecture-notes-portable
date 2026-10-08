"""Local browser adapter: one session, frozen FIFO/ASR, cooperative capture stop."""
import copy
import json
import threading
from pathlib import Path

from . import asr, chunk_capture, evidence, recording
from .config import load_settings
from .concurrent_pipeline import run_concurrent
from .live_capture import MAX_DURATION, pausable_producer, inspect_streaming, pause_integrity
from .live_recovery import recovering_asr, write_live_summary
from .live_summary import RollingSummary, preflight as summary_preflight
from .classroom_records import ClassroomRecords
from .media_import import MediaSelection, MediaError, imported_producer, inspect_import
from .portable_device import DeviceSelectionError, selected_input

ACTIVE = ('Starting', 'Recording', 'Pausing', 'Paused', 'Importing', 'Processing')


class LiveError(Exception):
    """Only fixed public codes; never propagate SDK/device exception text."""
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class LiveController:
    def __init__(self, root=None, *, duration=MAX_DURATION, runner=None, enable_summary=False):
        self.root = Path(root) if root else Path(__file__).resolve().parents[2]
        recording.validate_recording(1, duration, max_duration=MAX_DURATION)
        self.duration = duration
        self.courses = {c['course_id']: c['display_name'] for c in json.loads(
            (self.root/'references/index.json').read_text(encoding='utf-8'))}
        self.condition = threading.Condition(threading.RLock())
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.worker = None
        self.runner = runner or self._run_v0
        self.enable_summary = enable_summary
        self.summary_worker = None
        self.records = ClassroomRecords(self.root)
        self.media = MediaSelection(self.root)
        self.import_files = []
        self.import_sources = {}
        self.import_decoder = None
        self.active_device = None
        self.state = {'revision': 0, 'status': 'Idle', 'session_id': None, 'course_id': None,
                      'segments': [], 'error': None, 'summary': None, 'stop_requested': False,
                      'current_summary': None, 'summary_history': [], 'summary_state': self._initial_summary_state()}

    def save_record(self, session_id, name=None):
        with self.condition:
            if self.state['status'] not in ('Idle', 'Processing', 'Paused', 'Importing'):
                raise LiveError('record_not_ready', 409)
            if session_id != self.state['session_id']:
                raise LiveError('stale_session', 409)
            if self.state['course_id'] not in self.courses:
                raise LiveError('record_empty', 409)
            self.records.sync_pending(self.state)
            value = self.records.save(self.snapshot(), self.courses[self.state['course_id']], name=name)
            if self.state.get('record_sync_error'):
                self.update(record_sync_error=None)
            return value

    def restore_completed_session(self, session_id):
        # Explicit local recovery only; no device, worker, or HTTP invocation.
        from .live_snapshot import read_completed_snapshot
        with self.condition:
            if self.state['status'] in ACTIVE or (self.worker and self.worker.is_alive()):
                raise LiveError('session_already_active', 409)
            try:
                snapshot = read_completed_snapshot(self.root, session_id, self.courses)
            except Exception:
                raise LiveError('completed_session_restore_unavailable') from None
            self.update(**snapshot)

    def _initial_summary_state(self):
        return {'enabled': self.enable_summary, 'status': 'waiting' if self.enable_summary else 'disabled',
                'pending': False, 'error': None}

    def configuration(self):
        try:
            microphone = self.active_device or selected_input(self.root)
            availability = 'selected'
        except DeviceSelectionError:
            microphone = {'device_index': None, 'device_name': None, 'host_api': None,
                          'selection_mode': None, 'device_default_sample_rate_hz': None}
            availability = 'unavailable'
        return {**microphone, 'microphone_status': availability,
                'sample_rate': 44100, 'channels': 1, 'sample_format': 'PCM16',
                'chunk_duration': 12, 'overlap': 0, 'maximum_duration': self.duration,
                'media_import_supported': True, 'import_maximum_duration': MAX_DURATION,
                'pause_supported': True, 'duration_basis': 'captured_audio_excluding_pauses',
                'asr_model': 'Qwen/Qwen3-ASR-1.7B', 'summary_enabled': self.enable_summary,
                'summary_update_every_segments': 2, 'summary_mode': 'cumulative',
                'summary_context': 'previous complete summary + unconsumed RAW'}

    def snapshot(self):
        with self.condition:
            return copy.deepcopy(self.state)

    def wait_snapshot(self, revision, timeout=10):
        with self.condition:
            self.condition.wait_for(lambda: self.state['revision'] != revision, timeout)
            return copy.deepcopy(self.state)

    def update(self, **values):
        with self.condition:
            self.state.update(values)
            self.state['revision'] += 1
            try:
                self.records.sync_pending(self.state)
                self.state['record_sync_error'] = None
            except (OSError, ValueError):
                self.state['record_sync_error'] = 'record_sync_failed'
            self.condition.notify_all()

    def start(self, course_id, confirmed):
        if confirmed is not True:
            raise LiveError('human_confirmation_required')
        if not isinstance(course_id, str) or course_id not in self.courses:
            raise LiveError('unknown_course')
        with self.condition:
            if self.state['status'] in ACTIVE or (self.worker and self.worker.is_alive()):
                raise LiveError('session_already_active', 409)
            self.stop_event = threading.Event()
            self.pause_event = threading.Event()
            self.active_device = None
            self.summary_worker = None
            self.update(status='Starting', session_id=None, course_id=course_id, segments=[],
                        error=None, summary=None, stop_requested=False, current_summary=None, summary_history=[],
                        summary_state=self._initial_summary_state(), asr_progress=None, processing_phase=None, captured_duration=0, source_kind='microphone', source_files=[], import_progress=None)
            self.worker = threading.Thread(target=self._work, name='classroom-session', daemon=False)
            self.worker.start()
            return copy.deepcopy(self.state)

    def select_media(self, mode):
        with self.condition:
            if self.state['status'] in ACTIVE:
                raise LiveError('session_already_active', 409)
        result = self.media.select(mode)  # No controller lock while the native dialog is open.
        with self.condition:
            if self.state['status'] in ACTIVE:
                raise LiveError('session_already_active', 409)
        return result

    def start_import(self, course_id, confirmed, batch_id, file_ids):
        if confirmed is not True:
            raise LiveError('human_confirmation_required')
        if not isinstance(course_id, str) or course_id not in self.courses:
            raise LiveError('unknown_course')
        with self.condition:
            if self.state['status'] in ACTIVE or (self.worker and self.worker.is_alive()):
                raise LiveError('session_already_active', 409)
            files = self.media.resolve(batch_id, file_ids)
            self.stop_event = threading.Event(); self.pause_event = threading.Event()
            self.active_device = None
            self.import_files = files; self.import_sources = {}; self.summary_worker = None
            self.update(status='Starting', session_id=None, course_id=course_id, segments=[], error=None,
                summary=None, stop_requested=False, current_summary=None, summary_history=[],
                summary_state=self._initial_summary_state(), asr_progress=None, processing_phase=None,
                captured_duration=0, source_kind='import', import_progress=None,
                source_files=[{k:v for k,v in f.items() if k!='path'} for f in files])
            self.worker = threading.Thread(target=self._work, name='file-import-session', daemon=False)
            self.worker.start()
            return self.snapshot()

    def _run_import(self):
        settings = load_settings(); asr.validate_settings(settings)
        if (settings.transcription_endpoint != 'https://api.siliconflow.cn/v1/audio/transcriptions'
                or settings.asr_model != 'Qwen/Qwen3-ASR-1.7B'):
            raise LiveError('frozen_asr_configuration_required')
        if self.stop_event.is_set():return
        config = reference = None
        if self.enable_summary:
            config, reference = summary_preflight(self.root, self.state['course_id'])
        finished = threading.Event(); locations = {}
        def on_session(session_id, audio, output):
            locations.update(audio=audio, output=output)
            evidence.write_json(output/'browser-session.json', {'session_id':session_id,
                'course_id':self.state['course_id'],'course_name':self.courses[self.state['course_id']],
                'source_kind':'import','source_files':self.state['source_files'],
                'human_start_confirmed':True,'configuration':self.configuration()})
            if self.enable_summary:
                self.summary_worker=RollingSummary(output/'summary',config,reference,self.update)
            self.update(session_id=session_id,status='Processing' if self.stop_event.is_set() else 'Importing')
        def completed(segment):
            try:self.segment_finished(segment)
            finally:finished.set()
        session=run_concurrent(imported_producer(self.root,self.import_files,self,finished),
            recovering_asr(settings,self.update),self.root/'recordings/media-import',self.root/'outputs/media-import',
            on_session=on_session,on_segment=completed,chunk_seconds=12,allow_partial_chunks=True)
        report=session.capture_report
        if report is None:
            path=locations['audio']/'capture.json'
            report=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        frames=report.get('actual_captured_frames',0)
        integrity=inspect_import(locations['audio'],session.segments) if frames else None
        summary={'source_kind':'import','duration':frames/44100,'chunks':len(session.segments),
            'total_frames':frames,'queue_depth':session.queue_depth,'max_queue_depth':session.max_queue_depth,
            'audio_integrity':integrity,'source_files':report.get('files',[]),
            'session_status':session.status,'stop_requested':self.stop_event.is_set()}
        evidence.write_json(locations['output']/'browser-summary.json',summary)
        evidence.write_json(locations['output']/'import-sources.json',self.import_sources)
        failure=self.state.get('error') or ('media_decode_failed' if session.error else
                 'some_segments_failed_audio_preserved' if session.status=='completed_with_errors' else None)
        if integrity and not all(integrity[k] for k in ('chunk_pcm_equals_full','frame_sum_matches_full','contiguous_frame_metadata')):
            failure='media_incomplete_pcm'
        self.update(summary=summary,error=failure)

    def stop(self, session_id):
        with self.condition:
            if session_id != self.state['session_id']:
                raise LiveError('stale_session', 409)
            if self.state['status'] in ACTIVE:
                self.stop_event.set()
                if self.import_decoder is not None and self.import_decoder.poll() is None:
                    try:self.import_decoder.terminate()
                    except OSError:pass
                self.update(status='Processing', stop_requested=True)
            return copy.deepcopy(self.state)

    def pause(self, session_id):
        with self.condition:
            if session_id != self.state['session_id']:
                raise LiveError('stale_session', 409)
            if self.state['status'] == 'Pausing' or self.state['status'] == 'Paused':
                return self.snapshot()
            if self.state['status'] != 'Recording' or self.stop_event.is_set():
                raise LiveError('cannot_pause', 409)
            self.pause_event.set()
            self.update(status='Pausing')
            return self.snapshot()

    def capture_paused(self):
        with self.condition:
            if not self.stop_event.is_set():
                self.update(status='Paused')

    def resume(self, session_id):
        with self.condition:
            if session_id != self.state['session_id']:
                raise LiveError('stale_session', 409)
            if self.state['status'] != 'Paused' or self.stop_event.is_set():
                raise LiveError('cannot_resume', 409)
            self.pause_event.clear()
            self.update(status='Starting')  # Recording only after the stream actually starts.
            return self.snapshot()

    def recording_started(self):
        with self.condition:
            if not self.stop_event.is_set():
                self.update(status='Recording')

    def segment_finished(self, segment):
        # Exact raw string; IDs are the identity, never text matching or merging.
        row = {'segment_id': segment.segment_id, 'start_time': segment.start_time,
               'end_time': segment.end_time, 'raw_text': segment.raw_transcript,
               'status': segment.status, 'http_status': segment.http_status,
               'error': segment.error}
        with self.condition:
            if self.state.get('source_kind') == 'import':
                row.update(self.import_sources.get(segment.segment_id, {}))
            rows = {s['segment_id']: s for s in self.state['segments']}
            rows[row['segment_id']] = row
            self.update(segments=[rows[k] for k in sorted(rows)])
            ordered = copy.deepcopy(self.state['segments'])
        if self.summary_worker:
            try:
                self.summary_worker.submit(ordered)
            except Exception:
                # This callback runs on the frozen ASR worker: never let Summary
                # errors prevent later RAW segments from being delivered.
                self.update(summary_state={'enabled': True, 'status': 'failed', 'pending': False,
                                           'error': 'summary_worker_failed_evidence_preserved'})

    def _work(self):
        try:
            if self.state.get('source_kind') == 'import':
                self._run_import()
            else:
                self.runner(self)
        except (LiveError, MediaError) as error:
            self.update(error=error.code)
        except Exception:
            self.update(error='session_failed_audio_and_results_preserved')
        finally:
            if self.summary_worker:
                self.update(status='Processing', processing_phase='summary')
                try:
                    self.summary_worker.finish(self.snapshot()['segments'])
                except Exception:
                    self.update(summary_state={'enabled': True, 'status': 'failed', 'pending': False,
                                               'error': 'summary_finalization_failed_evidence_preserved'})
            self.active_device = None
            self.update(status='Idle', processing_phase=None, asr_progress=None)

    def _run_v0(self, _controller):
        # Import/config/device checks happen only after the explicit browser confirmation.
        try:
            settings = load_settings()
            asr.validate_settings(settings)
            if (settings.transcription_endpoint != 'https://api.siliconflow.cn/v1/audio/transcriptions'
                    or settings.asr_model != 'Qwen/Qwen3-ASR-1.7B'):
                raise LiveError('frozen_asr_configuration_required')
        except LiveError:
            raise
        except Exception:
            raise LiveError('asr_configuration_unavailable') from None
        if self.stop_event.is_set():
            self.update(status='Idle')
            return
        summary_config = summary_reference = None
        if self.enable_summary:
            try:
                summary_config, summary_reference = summary_preflight(self.root, self.state['course_id'])
            except Exception:
                raise LiveError('summary_configuration_unavailable') from None
        try:
            selected = selected_input(self.root)
            config = recording.prepare_recording(selected['device_index'], self.duration,
                                                 selected['device_name'], selected['host_api'],
                                                 max_duration=MAX_DURATION)
            self.active_device = selected
        except Exception:
            raise LiveError('microphone_configuration_unavailable') from None
        config['chunk_duration_seconds'] = 12
        locations = {}
        def on_session(session_id, audio, output):
            locations.update(audio=audio, output=output)
            evidence.write_json(output/'browser-session.json', {
                'session_id': session_id, 'course_id': self.state['course_id'],
                'course_name': self.courses[self.state['course_id']],
                'human_start_confirmed': True, 'configuration': self.configuration()})
            self.update(session_id=session_id)
            if self.enable_summary:
                self.summary_worker = RollingSummary(output/'summary', summary_config,
                                                     summary_reference, self.update)
        capture = pausable_producer(config, self)
        def produce(publish, directory):
            try:
                return capture(publish, directory)
            finally:
                self.update(status='Processing', processing_phase='asr')
        artifact_group = 'live-summary-v1' if self.enable_summary else 'frontend-v1'
        session = run_concurrent(produce, recovering_asr(settings, self.update),
            self.root/'recordings'/artifact_group, self.root/'outputs'/artifact_group,
            on_session=on_session, on_segment=self.segment_finished, chunk_seconds=12, allow_partial_chunks=True)
        # run_concurrent returns only AFTER tail publication, FIFO drain and worker join.
        report = session.capture_report
        if report is None:
            # The producer persists partial evidence even when acquisition raises.
            saved_report = locations['audio']/'capture.json'
            report = json.loads(saved_report.read_text(encoding='utf-8')) if saved_report.is_file() else {}
        total_frames = report.get('actual_captured_frames', 0)
        continuity = pause_integrity(session.segments, total_frames, report)
        integrity = inspect_streaming(locations['audio'], session.segments, report) if total_frames else None
        summary = {'session_status': session.status, 'total_frames': total_frames,
                   'duration': total_frames/44100, 'chunks': len(session.segments),
                   'queue_depth': session.queue_depth, 'max_queue_depth': session.max_queue_depth,
                   'stream_open_count': report.get('stream_open_count', 0),
                   'stream_close_count': report.get('stream_close_count', 0),
                   'overflow_events': len(report.get('portaudio_status_events', [])),
                   'continuity': continuity, 'audio_integrity': integrity,
                   'stop_requested': self.stop_event.is_set()}
        evidence.write_json(locations['output']/'browser-summary.json', summary)
        write_live_summary(session, locations['audio'], locations['output'], capture_integrity=integrity)
        error = ('session_failed_audio_and_results_preserved' if session.error else
                 'some_segments_failed_audio_preserved' if session.status == 'completed_with_errors' else None)
        self.update(summary=summary, error=error)

    def close(self):
        with self.condition:
            worker = self.worker
            if self.state['status'] in ACTIVE:
                self.stop_event.set()
                if self.import_decoder is not None and self.import_decoder.poll() is None:
                    try:self.import_decoder.terminate()
                    except OSError:pass
                self.update(status='Processing', stop_requested=True)
        if worker:
            worker.join()  # Same bounded HTTP timeout/drain contract as V0; no forced kill.
