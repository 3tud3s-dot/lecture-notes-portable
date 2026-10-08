"""Read one explicitly selected, completed session into the browser snapshot."""
import json
import re
from pathlib import Path

from .current_summary import parse_summary
from .lecture_summary import MODE, parse_summary as parse_lecture_summary
from .evidence import redact
from .live_summary import summary_view


def read_completed_snapshot(root, session_id, courses):
    if not isinstance(session_id, str) or not re.fullmatch(r'\d{8}T\d{12}Z-[0-9a-f]{8}', session_id):
        raise ValueError('invalid_session_id')
    candidates = [Path(root)/'outputs'/kind/session_id for kind in ('live-summary-v1','media-import')]
    matches = [p for p in candidates if p.is_dir()]
    if len(matches) != 1:raise ValueError('session_not_found')
    base = matches[0]
    def read(name):
        return json.loads((base/name).read_text(encoding='utf-8-sig'))
    browser = read('browser-session.json')
    capture = read('browser-summary.json')
    session = read('session.json')
    summaries = read('summary/summary-session.json')
    if (browser['session_id'] != session_id or session['session_id'] != session_id
            or browser['course_id'] not in courses or session['status'] not in
            ('completed', 'completed_with_errors', 'failed') or not session.get('consumer_finished_at')
            or not summaries.get('consumer_finished_at') or summaries.get('final_pending_depth') != 0
            or summaries.get('inflight_request_id') is not None or summaries.get('requests_unresolved') != 0):
        raise ValueError('session_not_completed')
    rows = [{'segment_id': s['segment_id'], 'start_time': s['start_time'], 'end_time': s['end_time'],
             'raw_text': s['raw_transcript'], 'status': s['status'], 'http_status': s['http_status'],
             'error': s['error']} for s in session['segments']]
    if browser.get('source_kind') == 'import':
        origins = read('import-sources.json')
        for row in rows:row.update(origins.get(str(row['segment_id']),{}))
    if [s['segment_id'] for s in rows] != list(range(len(rows))):
        raise ValueError('invalid_raw_ids')
    by_id = {s['segment_id']: s for s in rows}
    history, seen, previous = [], set(), None
    for record in summaries['updates']:
        if type(record['request_id']) is not int or record['request_id'] in seen:
            raise ValueError('invalid_summary_ids')
        seen.add(record['request_id'])
        if record['status'] != 'succeeded':
            continue
        window = [by_id[i] for i in record['source_segment_ids']]
        cumulative = record.get('summary_mode') == MODE
        if ((not cumulative and not 3 <= len(window) <= 4) or not window
                or any(s['status'] != 'completed' for s in window)):
            raise ValueError('invalid_summary_window')
        if cumulative:
            parse_lecture_summary(json.dumps(record['summary']), window, previous, record['length_budget_chars'])
        else:
            parse_summary(json.dumps(record['summary']), window)
        previous = record['summary']
        history.append(summary_view(record, window))
    history.sort(key=lambda s: s['request_id'])
    error = ('session_failed_audio_and_results_preserved' if session.get('error') else
             'some_segments_failed_audio_preserved' if session['status'] == 'completed_with_errors' else None)
    last = summaries['updates'][-1] if summaries['updates'] else None
    current = history[-1] if history else None
    if current and last and last.get('summary_mode') == MODE and last['status'] == 'unchanged':
        current = {**current, 'end_time': last['coverage_end_time'],
                   'missing_segment_ids': last['missing_segment_ids'], 'trigger': last['trigger']}
    return redact({'status': 'Idle', 'session_id': session_id, 'course_id': browser['course_id'],
                   'segments': rows, 'error': error, 'summary': capture,
                   'stop_requested': capture['stop_requested'], 'summary_history': history,
                   'current_summary': current, 'source_kind':browser.get('source_kind','microphone'),
                   'source_files':browser.get('source_files',[]),
                   'summary_state': {'enabled': True, 'status': 'failed' if last and last['status'] == 'failed'
                                     else 'ready' if history else 'waiting', 'pending': False,
                                     'error': last.get('error') if last else None}}, '')
