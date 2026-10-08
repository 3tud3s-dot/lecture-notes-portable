"""Loopback-only standard-library HTTP + SSE; startup never records or calls ASR."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote

from .live import LiveController, LiveError
from .classroom_records import RecordError
from .media_import import MediaError

ORIGINS = {f'http://{host}:{port}' for host in ('localhost', '127.0.0.1') for port in (5173, 4173)}


def handler_for(controller):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            pass  # No request headers, bodies, credentials or exception details in logs.

        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass  # Windows/browser cancellation is a normal transport disconnect.

        def local_request(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',
                                                f'localhost:{self.server.server_port}')

        def allowed_origin(self):
            return self.headers.get('Origin') in ORIGINS

        def cors(self):
            if self.allowed_origin():
                self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
                self.send_header('Vary', 'Origin')

        def reply(self, status, body):
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status); self.cors()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.close_connection = True
            self.end_headers(); self.wfile.write(data)

        def do_OPTIONS(self):
            if not self.local_request() or not self.allowed_origin():
                return self.reply(403, {'error': 'origin_not_allowed'})
            self.send_response(204); self.cors()
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Lecture-Client')
            self.send_header('Content-Length', '0'); self.end_headers()

        def do_GET(self):
            if not self.local_request() or (self.headers.get('Origin') and not self.allowed_origin()):
                return self.reply(403, {'error': 'origin_not_allowed'})
            path = urlsplit(self.path).path
            if path == '/api/health':
                return self.reply(200, {'ready': True, 'configuration': controller.configuration()})
            if path == '/api/state':
                return self.reply(200, controller.snapshot())
            if path == '/api/records' or path.startswith('/api/records/'):
                try:
                    return self.reply(200, controller.records.list() if path == '/api/records'
                                      else controller.records.get(unquote(path.removeprefix('/api/records/'))))
                except RecordError as error:
                    return self.reply(getattr(error, 'status', 400), {'error': error.code})
                except OSError:
                    return self.reply(500, {'error': 'record_storage_failed'})
            if path != '/api/events':
                return self.reply(404, {'error': 'not_found'})
            self.close_connection = True  # One streaming response per connection.
            self.send_response(200); self.cors()
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache, no-transform')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            revision = -1
            try:
                while not self.server.exiting:
                    snapshot = controller.wait_snapshot(revision)
                    if snapshot['revision'] != revision:
                        revision = snapshot['revision']
                        data = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
                        self.wfile.write(f'id: {revision}\nevent: snapshot\ndata: {data}\n\n'.encode('utf-8'))
                    else:
                        self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # Disconnect never starts/stops capture; reconnect gets a full snapshot.

        def do_POST(self):
            if (not self.local_request() or not self.allowed_origin()
                    or self.headers.get('X-Lecture-Client') != 'browser'):
                return self.reply(403, {'error': 'origin_not_allowed'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 16384 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, UnicodeError):
                return self.reply(400, {'error': 'invalid_request'})
            try:
                if self.path == '/api/start':
                    result = controller.start(data.get('course_id'), data.get('confirmed'))
                elif self.path == '/api/import/select':
                    result = controller.select_media(data.get('mode'))
                elif self.path == '/api/import/start':
                    result = controller.start_import(data.get('course_id'), data.get('confirmed'), data.get('batch_id'), data.get('file_ids'))
                elif self.path == '/api/pause':
                    result = controller.pause(data.get('session_id'))
                elif self.path == '/api/resume':
                    result = controller.resume(data.get('session_id'))
                elif self.path == '/api/stop':
                    result = controller.stop(data.get('session_id'))
                elif self.path == '/api/records/save':
                    result = controller.save_record(data.get('session_id'), data.get('name'))
                elif self.path == '/api/records/rename':
                    result = controller.records.rename(data.get('record_id'), data.get('name'))
                else:
                    return self.reply(404, {'error': 'not_found'})
                self.reply(200 if self.path.startswith('/api/records/') else 202, result)
            except (LiveError, RecordError, MediaError) as error:
                self.reply(getattr(error, 'status', 400), {'error': error.code})
            except OSError:
                self.reply(500, {'error': 'record_storage_failed'})
    return Handler


def make_server(controller, port=8765):
    server = ThreadingHTTPServer(('127.0.0.1', port), handler_for(controller))
    server.daemon_threads = True
    server.exiting = False
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--duration', type=float, default=6000, help='Captured audio cap, 0 < seconds <= 6000; pauses excluded')
    parser.add_argument('--summary', action='store_true', help='Enable cumulative Summary after confirmed browser Start')
    parser.add_argument('--restore-session', help='Restore one completed local Summary session for display only; never resume recording')
    args = parser.parse_args(argv)
    controller = LiveController(duration=args.duration, enable_summary=args.summary)
    if args.restore_session:
        if not args.summary:
            parser.error('--restore-session requires --summary')
        try:
            controller.restore_completed_session(args.restore_session)
        except LiveError:
            parser.error('Completed session could not be restored; no recording started.')
    server = make_server(controller, args.port)
    print(f'Local classroom backend: http://127.0.0.1:{server.server_port}', flush=True)
    print('Ready; no microphone opened. Only an explicitly confirmed browser Start can record.', flush=True)
    print(f'Cumulative Summary: {"enabled" if args.summary else "disabled"}; no LLM request until confirmed Start and sufficient RAW.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopping capture and waiting for ASR + Summary work to drain...', flush=True)
    finally:
        server.exiting = True
        controller.close()
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
