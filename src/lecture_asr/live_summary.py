"""Cumulative Summary adapter. Independent of the frozen microphone/ASR consumer."""
import copy
import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .lecture_summary import (MODE, PROMPT_VERSION, SummaryError, length_budget,
                              messages_for, parse_summary, source_ids, validate_segments)
from .evidence import redact, write_json
from .llm import LLMClientError, SiliconFlowLLMClient, load_llm_config


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def summary_view(record, segments):
    """The generated summary unchanged, with its input window provenance."""
    view = {'summary': record['summary'], 'source_segment_ids': record['source_segment_ids'],
            'start_time': segments[0]['start_time'], 'end_time': segments[-1]['end_time'],
            'trigger': record['trigger'], 'request_id': record['request_id']}
    if record.get('summary_mode') == MODE:
        view.update(mode=MODE, start_time=0, end_time=record['coverage_end_time'],
                    length_budget_chars=record['length_budget_chars'],
                    missing_segment_ids=record['missing_segment_ids'])
    return view


def preflight(root, course_id):
    """Local config/reference reads only. No SDK, network or device access."""
    config = load_llm_config()
    if (not config.api_key or config.model != 'Qwen/Qwen3.5-35B-A3B'
            or config.base_url.rstrip('/') != 'https://api.siliconflow.cn/v1'):
        raise SummaryError('summary_configuration_unavailable')
    root = Path(root)
    catalog = json.loads((root/'references/index.json').read_text(encoding='utf-8'))
    entry = next(c for c in catalog if c['course_id'] == course_id)
    path = (root/entry['reference_path']).resolve()
    if not path.is_relative_to((root/'references').resolve()):
        raise SummaryError('invalid_reference_path')
    reference = json.loads(path.read_text(encoding='utf-8'))
    if reference['course_id'] != course_id:
        raise SummaryError('reference_course_mismatch')
    return config, reference


def reference_hints(reference, segments):
    """At most 10 matching terms, only from the selected course. No definitions."""
    text = '\n'.join(s['raw_text'] for s in segments).casefold()
    terms, seen = [], set()
    for section in reference['sections']:
        for term in section.get('terms', []):
            name = term['term']
            aliases = term.get('aliases', [])
            if name in seen or not any(len(t) >= 2 and t.casefold() in text for t in [name, *aliases]):
                continue
            seen.add(name)
            terms.append({'term': name, 'aliases': aliases, 'section_id': section['section_id']})
            if len(terms) == 10:
                break
        if len(terms) == 10:
            break
    return {'course_id': reference['course_id'], 'terms': terms,
            'role': 'terminology hints only; never a source of lecture facts'}


def live_client(config):
    # SDK connect/read/write inactivity timeout, not a guaranteed total deadline.
    return SiliconFlowLLMClient(config, timeout=20.0)


class RollingSummary:
    """One worker + latest pending snapshot; only successful updates consume RAW.

    Warm up to four segments, then every two new segments. Resolve unconsumed
    RAW when executing, so failed or superseded updates cannot skip new content.
    Stop integrates remaining RAW; no duplicate LLM call when nothing is new.
    """
    def __init__(self, directory, config, reference, on_update, *, client_factory=live_client):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.config, self.reference, self.on_update = config, reference, on_update
        self.client_factory = client_factory
        self.condition = threading.Condition()
        self.pending = None
        self.closing = False
        self.seen_count = 0
        self.previous = None
        self.covered_through = -1
        self.missing_ids = []
        self.current = None
        self.history = []
        self.records = []
        self.skips = []
        self.inflight = None
        self.last_failed_source_ids = None
        self.final_status = 'not_requested'
        self.fatal = None
        self.finish_requested_at = None
        self.finished_at = None
        self.drain_seconds = None
        self._save()
        self.worker = threading.Thread(target=self._loop, name='current-summary', daemon=False)
        self.worker.start()

    def _public(self, status, error=None):
        self.on_update(current_summary=copy.deepcopy(self.current), summary_history=copy.deepcopy(self.history), summary_state={
            'enabled': True, 'status': status, 'pending': self.pending is not None,
            'error': error})

    def _save(self):
        # Called under the scheduler lock after startup; no credentials/SDK objects.
        attempted = [r for r in self.records if r['status'] in ('running', 'succeeded', 'failed')]
        write_json(self.directory/'summary-session.json', redact({
            'model': self.config.model, 'temperature': self.config.temperature,
            'prompt_version': PROMPT_VERSION, 'summary_mode': MODE,
            'input_policy': 'previous complete summary + all unconsumed successful RAW',
            'covered_through_segment_id': self.covered_through,
            'missing_segment_ids': self.missing_ids,
            'length_budget': 'min(1800, round(180 + 140*log2(1+elapsed_seconds/60))) chars',
            'update_every_new_segments': 2, 'first_regular_after_segments': 4,
            'automatic_retries': 0, 'live_sdk_timeout_seconds': 20, 'queue_policy': 'one worker; latest pending window replaces older pending',
            'requests_started': len(attempted),
            'requests_succeeded': sum(r['status'] == 'succeeded' for r in self.records),
            'requests_failed': sum(r['status'] == 'failed' for r in self.records),
            'requests_unresolved': sum(r['status'] == 'running' for r in self.records),
            'coalesced_windows': sum(r['status'] == 'superseded' for r in self.records),
            'max_pending_depth': int(bool(self.records)),
            'final_pending_depth': int(self.pending is not None),
            'inflight_request_id': self.inflight, 'final_update_status': self.final_status,
            'finish_requested_at': self.finish_requested_at, 'consumer_finished_at': self.finished_at,
            'post_raw_drain_seconds': self.drain_seconds, 'fatal_error': self.fatal,
            'updates': self.records, 'skipped_updates': self.skips,
        }, self.config.api_key))

    def submit(self, segments, *, final=False):
        rows = copy.deepcopy(segments)
        with self.condition:
            if self.closing or self.fatal:
                return
            if not final:
                if len(rows) <= self.seen_count:
                    return
                self.seen_count = len(rows)
                if len(rows) < 4 or len(rows) % 2:
                    return
            trigger = 'final' if final else 'periodic'
            valid = bool(rows) and all(
                row.get('segment_id') == i and type(row.get('segment_id')) is int
                and row.get('status') in ('completed', 'failed')
                and isinstance(row.get('start_time'), (float, int))
                and not isinstance(row.get('start_time'), bool)
                and isinstance(row.get('end_time'), (float, int))
                and not isinstance(row.get('end_time'), bool)
                and math.isfinite(row['start_time']) and math.isfinite(row['end_time'])
                and 0 <= row['start_time'] < row['end_time']
                and (not i or row['start_time'] == rows[i-1]['end_time'])
                for i, row in enumerate(rows))
            if not valid:
                self.skips.append({'trigger': trigger, 'at': timestamp(),
                                   'source_segment_ids': [], 'reason': 'invalid_or_empty_raw_snapshot'})
                if final:
                    self.final_status = 'skipped_no_usable_raw'
                self._save()
                self._public('updating' if self.inflight is not None else 'waiting', 'invalid_raw_snapshot')
                return
            if self.pending is not None:
                self.pending['record']['status'] = 'superseded'
                self.pending['record']['superseded_at'] = timestamp()
            record = {'request_id': len(self.records), 'trigger': trigger,
                      'source_segment_ids': [], 'summary_mode': MODE,
                      'queued_at': timestamp(), 'status': 'pending', 'model': self.config.model,
                      'request_started_at': None, 'request_finished_at': None,
                      'latency_seconds': None, 'summary': None, 'error': None}
            self.records.append(record)
            self.pending = {'record': record, 'rows': rows}
            if final:
                self.final_status = 'pending'
            self._save()
            self._public('updating')
            self.condition.notify_all()

    def finish(self, segments):
        """Called only after RAW tail + ASR drain. Keeps Processing until joined."""
        begin = time.perf_counter()
        with self.condition:
            self.finish_requested_at = timestamp()
        try:
            self.submit(segments, final=True)
        finally:
            with self.condition:
                self.closing = True
                self.condition.notify_all()
            self.worker.join()
        with self.condition:
            self.finished_at = timestamp()
            self.drain_seconds = time.perf_counter()-begin
            self._save()

    def _loop(self):
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.pending is not None or self.closing)
                    if self.pending is None:
                        return
                    job, self.pending = self.pending, None
                    self.inflight = job['record']['request_id']
                self._execute(job)
        except Exception:
            # Summary failure cannot terminate capture or the ASR consumer.
            with self.condition:
                self.fatal = 'summary_worker_failed_evidence_preserved'
                self._public('failed', self.fatal)

    def _execute(self, job):
        record, rows = job['record'], job['rows']
        unconsumed = [s for s in rows if s['segment_id'] > self.covered_through]
        segments = [{k: s[k] for k in ('segment_id', 'start_time', 'end_time', 'raw_text')}
                    for s in unconsumed if s['status'] == 'completed' and (s.get('raw_text') or '').strip()]
        missing = [{'segment_id': s['segment_id'], 'start_time': s['start_time'], 'end_time': s['end_time']}
                   for s in unconsumed if s['status'] != 'completed' or not (s.get('raw_text') or '').strip()]
        record.update(source_segment_ids=[s['segment_id'] for s in segments],
                      previous_source_segment_ids=sorted(source_ids(self.previous)),
                      previous_covered_through_segment_id=self.covered_through,
                      covered_through_segment_id=rows[-1]['segment_id'],
                      coverage_end_time=rows[-1]['end_time'], length_budget_chars=length_budget(rows[-1]['end_time']),
                      missing_segment_ids=sorted(set(self.missing_ids + [s['segment_id'] for s in missing])))
        if (record['trigger'] != 'final' and segments
                and record['source_segment_ids'] == self.last_failed_source_ids):
            # More failed ASR rows are not new useful LLM input. Wait for new RAW,
            # while still allowing the explicit final update to try once.
            with self.condition:
                record.update(status='skipped', reason='unchanged_failed_raw_waiting_for_new_content',
                              request_finished_at=timestamp())
                self.inflight = None
                self._save()
                self._public('failed', 'summary_waiting_for_new_raw')
            return
        if not segments:
            with self.condition:
                record.update(status='unchanged' if self.previous else 'skipped',
                              reason='no_new_usable_raw', request_finished_at=timestamp())
                self.inflight = None
                self.missing_ids = record['missing_segment_ids']
                if self.previous:
                    self.covered_through = rows[-1]['segment_id']
                    self.current = {**self.current, 'end_time': rows[-1]['end_time'],
                                    'missing_segment_ids': self.missing_ids}
                if record['trigger'] == 'final':
                    self.final_status = 'unchanged_no_new_raw' if self.previous else 'skipped_no_usable_raw'
                    if self.current:
                        self.current = {**self.current, 'trigger': 'final'}
                self._save()
                self._public('ready' if self.previous else 'waiting')
            return
        validate_segments(segments)
        budget = length_budget(rows[-1]['end_time'])
        folder = self.directory/f"update-{record['request_id']:03d}"
        folder.mkdir()
        hints = reference_hints(self.reference, segments)
        messages = messages_for(segments, hints, self.previous, rows[-1]['end_time'], missing)
        write_json(folder/'input.json', redact({'segments': segments, 'reference': hints,
                                                'previous_summary': self.previous,
                                                'missing_raw_segments': missing,
                                                'length_budget_chars': budget,
                                                'lecture_elapsed_seconds': rows[-1]['end_time']}, self.config.api_key))
        write_json(folder/'messages.json', redact(messages, self.config.api_key))
        write_json(folder/'request.json', {'endpoint': self.config.chat_endpoint,
                   'model': self.config.model, 'temperature': self.config.temperature,
                   'max_tokens': 2*budget + 600, 'enable_thinking': False,
                   'response_format': {'type': 'json_object'}, 'automatic_retries': 0})
        begin = time.perf_counter()
        with self.condition:
            record.update(status='running', request_started_at=timestamp())
            self._save()
            self._public('updating')
        try:
            with self.client_factory(self.config) as client:
                response = client.complete(messages, max_tokens=2*budget+600, enable_thinking=False,
                                           response_format={'type': 'json_object'})
            write_json(folder/'response.json', redact(response.model_dump(mode='json'), self.config.api_key))
            if response.choices[0].finish_reason != 'stop':
                raise SummaryError('incomplete_model_output')
            content = response.choices[0].message.content
            (folder/'llm-content.txt').write_text(redact(content or '', self.config.api_key), encoding='utf-8')
            parsed = parse_summary(content, segments, self.previous, budget)
            # A contaminated response must never leak to SSE or disk.
            if redact(parsed, self.config.api_key) != parsed:
                raise SummaryError('sensitive_model_output_rejected')
            with self.condition:
                self.last_failed_source_ids = None
                self.previous = parsed
                self.covered_through = rows[-1]['segment_id']
                self.missing_ids = record['missing_segment_ids']
                record.update(status='succeeded', summary=parsed)
                self.current = summary_view(record, segments)
                self.history.append(copy.deepcopy(self.current))
        except LLMClientError as error:
            record.update(status='failed', error=error.code, http_status=error.http_status)
        except SummaryError as error:
            record.update(status='failed', error=str(error))
        except Exception:
            record.update(status='failed', error='summary_request_failed')
        finally:
            with self.condition:
                record.update(request_finished_at=timestamp(), latency_seconds=time.perf_counter()-begin)
                if record['status'] == 'failed':
                    self.last_failed_source_ids = record['source_segment_ids']
                if record['trigger'] == 'final':
                    self.final_status = record['status']
                write_json(folder/'result.json', redact(record, self.config.api_key))
                self.inflight = None
                self._save()
                self._public('updating' if self.pending is not None else
                             'ready' if record['status'] == 'succeeded' else 'failed', record['error'])
