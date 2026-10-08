"""User-saved local classroom snapshots. No audio, API, or database dependency."""
import copy
import json
import os
import re
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


class RecordError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def record_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{8}T\d{12}Z-[0-9a-f]{8}', value):
        raise RecordError('invalid_record_id')
    return value


def record_name(value):
    if (not isinstance(value, str) or not value.strip() or len(value.strip()) > 100
            or any(unicodedata.category(c) in ('Cc', 'Cs') for c in value)):
        raise RecordError('invalid_record_name')
    return value.strip()


def default_name(course_name, session_id):
    # Session IDs carry UTC capture start. Labels use this computer's local time.
    started = datetime.strptime(record_id(session_id).split('-')[0], '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
    return f'{course_name} · {started.astimezone():%Y-%m-%d %H:%M}'


class ClassroomRecords:
    def __init__(self, root):
        self.directory = Path(root) / 'data/classroom_records'
        self.lock = threading.RLock()

    def _path(self, identifier):
        path = self.directory / f'{record_id(identifier)}.json'
        if path.resolve().parent != self.directory.resolve():
            raise RecordError('invalid_record_id')
        return path

    def _read(self, identifier):
        try:
            value = json.loads(self._path(identifier).read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise RecordError('record_not_found', 404) from None
        except (OSError, ValueError):
            raise RecordError('record_unreadable', 500) from None
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or value.get('record_id') != identifier or not isinstance(value.get('snapshot'), dict)
                or value['snapshot'].get('session_id') != identifier
                or value['snapshot'].get('status') not in ('Idle', 'Processing', 'Paused', 'Pausing', 'Starting', 'Recording', 'Importing')
                or value['snapshot'].get('course_id') != value.get('course_id')):
            raise RecordError('record_unreadable', 500)
        if (any(not isinstance(value.get(key), str) for key in
                ('name', 'course_id', 'course_name', 'created_at', 'updated_at'))
                or not isinstance(value.get('duration'), (int, float))
                or type(value.get('segment_count')) is not int
                or not isinstance(value['snapshot'].get('segments'), list)):
            raise RecordError('record_unreadable', 500)
        try:
            record_name(value['name'])
        except (KeyError, RecordError):
            raise RecordError('record_unreadable', 500) from None
        return value

    def _write(self, identifier, value):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                             dir=self.directory, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self._path(identifier))
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    @staticmethod
    def metadata(value):
        return {key: value[key] for key in ('record_id', 'name', 'course_id', 'course_name',
                                          'created_at', 'updated_at', 'duration', 'segment_count')} | {'source_kind':value.get('source_kind','microphone')}

    def list(self):
        with self.lock:
            records, unreadable = [], 0
            for path in self.directory.glob('*.json'):
                try:
                    records.append(self.metadata(self._read(record_id(path.stem))))
                except (RecordError, KeyError, TypeError):
                    unreadable += 1
            records.sort(key=lambda item: (item['created_at'], item['record_id']), reverse=True)
            return {'records': records, 'unreadable_count': unreadable}

    def get(self, identifier):
        with self.lock:
            return self._read(record_id(identifier))

    def save(self, snapshot, course_name, *, name=None):
        if snapshot.get('status') not in ('Idle', 'Processing', 'Paused', 'Importing'):
            raise RecordError('record_not_ready', 409)
        identifier = record_id(snapshot.get('session_id'))
        if not snapshot.get('segments'):
            raise RecordError('record_empty', 409)
        with self.lock:
            path = self._path(identifier)
            if path.exists():
                # A second Save is idempotent; it must not overwrite a renamed title/content.
                return self._read(identifier)
            title = record_name(default_name(course_name + (' · 导入' if snapshot.get('source_kind')=='import' else ''), identifier) if name is None else name)
            now = datetime.now(timezone.utc).isoformat()
            saved = {key: copy.deepcopy(snapshot.get(key)) for key in
                     ('status', 'session_id', 'course_id', 'segments', 'error', 'summary',
                      'stop_requested', 'current_summary', 'summary_history', 'summary_state')}
            saved.update({k:copy.deepcopy(snapshot[k]) for k in ('source_kind','source_files','import_progress') if k in snapshot})
            value = {'schema_version': 1, 'record_id': identifier, 'name': title,
                     'course_id': snapshot['course_id'], 'course_name': course_name,
                     'source_kind':snapshot.get('source_kind','microphone'),
                     'created_at': now, 'updated_at': now,
                     'duration': (snapshot.get('summary') or {}).get('duration') or snapshot['segments'][-1]['end_time'],
                     'segment_count': len(snapshot['segments']), 'snapshot': saved,
                     'processing_pending': snapshot['status'] != 'Idle',
                     'snapshot_revision': snapshot.get('revision', 0)}
            self._write(identifier, value)
            return value

    def sync_pending(self, snapshot):
        """Only refresh records explicitly saved while draining; never mutate finalized history.

        Called under the controller lock, then storage lock (same order as Save).
        No HTTP, thread join or audio operation runs with either lock held.
        """
        identifier = snapshot.get('session_id')
        if not identifier or snapshot.get('status') not in ('Idle', 'Processing', 'Paused', 'Pausing', 'Starting', 'Recording', 'Importing'):
            return
        with self.lock:
            if not self._path(identifier).exists():
                return
            value = self._read(identifier)
            if not value.get('processing_pending') or snapshot.get('revision', 0) <= value.get('snapshot_revision', -1):
                return
            value['snapshot'] = {key: copy.deepcopy(snapshot.get(key)) for key in value['snapshot']}
            value.update(processing_pending=snapshot['status'] != 'Idle',
                         snapshot_revision=snapshot.get('revision', 0),
                         updated_at=datetime.now(timezone.utc).isoformat(),
                         duration=(snapshot.get('summary') or {}).get('duration') or snapshot['segments'][-1]['end_time'],
                         segment_count=len(snapshot['segments']))
            self._write(identifier, value)

    def rename(self, identifier, name):
        title = record_name(name)
        with self.lock:
            value = self._read(record_id(identifier))
            value.update(name=title, updated_at=datetime.now(timezone.utc).isoformat())
            self._write(identifier, value)
            return value
