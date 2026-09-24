import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

from gromacs_monitor.config import Config
from gromacs_monitor.app import MonitorEngine
from gromacs_monitor.state_store import StateStore
from gromacs_monitor.telegram_api import TelegramClient, TelegramError


class _Handler(BaseHTTPRequestHandler):
    responses = []
    calls = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        type(self).calls.append((self.path, urllib.parse.parse_qs(body)))
        status, payload = type(self).responses.pop(0)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


class TelegramApiTests(unittest.TestCase):
    def setUp(self):
        _Handler.responses = []
        _Handler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.client = TelegramClient("SECRET:TOKEN", api_base=f"http://{host}:{port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_get_updates_posts_timeout_offset_and_filters_messages(self):
        _Handler.responses.append((200, {"ok": True, "result": [
            {"update_id": 10, "message": {"from": {"id": 42}, "chat": {"id": 99}, "text": "/status"}},
            {"update_id": 11, "message": {"from": {"id": 42}, "chat": {"id": 99}, "photo": []}},
            {"update_id": 12, "edited_message": {}},
        ]}))
        updates = self.client.get_updates(offset=10, timeout=25)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].update_id, 10)
        self.assertEqual(updates[0].text, "/status")
        path, form = _Handler.calls[0]
        self.assertTrue(path.endswith("/getUpdates"))
        self.assertEqual(form["offset"], ["10"])
        self.assertEqual(form["timeout"], ["25"])

    def test_send_message_posts_encoded_fields(self):
        _Handler.responses.append((200, {"ok": True, "result": {"message_id": 1}}))
        self.client.send_message(99, "hello & world")
        path, form = _Handler.calls[0]
        self.assertTrue(path.endswith("/sendMessage"))
        self.assertEqual(form["chat_id"], ["99"])
        self.assertEqual(form["text"], ["hello & world"])

    def test_http_invalid_json_and_api_failure_do_not_leak_token(self):
        for response in [
            (500, {"ok": False}),
            (200, b"not-json"),
            (200, {"ok": False, "description": "bad"}),
        ]:
            _Handler.responses.append(response)
            with self.assertRaises(TelegramError) as ctx:
                self.client.get_updates(None, timeout=1)
            self.assertNotIn("SECRET:TOKEN", str(ctx.exception))


class OfflineRecoveryTests(unittest.TestCase):
    def test_failed_transitions_replace_pending_and_recover_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = Config("t", 42, root / "runs", root / "gmx")
            store = StateStore(root / "state.json")
            times = iter([100.0, 200.0, 300.0])
            engine = MonitorEngine(cfg, store, clock=lambda: next(times))
            engine.record_failed_notification("GROMACS STALLED")
            engine.record_failed_notification("GROMACS STOPPED")
            pending = store.load().pending_notification
            self.assertEqual(pending["text"], "GROMACS STOPPED")
            self.assertEqual(pending["replaced_count"], 1)
            sent = []
            summary = engine.deliver_pending_notification(sent.append)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0], summary)
            self.assertIn("GROMACS STOPPED", summary)
            self.assertIn("1", summary)
            self.assertIsNone(store.load().pending_notification)
            self.assertIsNone(engine.deliver_pending_notification(sent.append))
            self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
