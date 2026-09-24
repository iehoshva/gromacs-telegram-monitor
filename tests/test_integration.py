import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from gromacs_monitor.app import BotApplication, MonitorEngine
from gromacs_monitor.config import Config
from gromacs_monitor.model import ProcessInfo, Stage
from gromacs_monitor.state_store import StateStore
from gromacs_monitor.telegram_api import TelegramClient, TelegramError


class _BotHandler(BaseHTTPRequestHandler):
    update_batches = []
    sent = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if self.path.endswith("/getUpdates"):
            result = type(self).update_batches.pop(0) if type(self).update_batches else []
            payload = {"ok": True, "result": result}
        elif self.path.endswith("/sendMessage"):
            type(self).sent.append((int(body["chat_id"][0]), body["text"][0]))
            payload = {"ok": True, "result": {"message_id": len(type(self).sent)}}
        else:
            payload = {"ok": False}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        pass


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.base = self.root / "runs"
        self.run = self.base / "run-A"
        prod = self.run / "04_prod"
        prod.mkdir(parents=True)
        self.log = prod / "prod.log"
        self.log.write_text("Step Time\n18810000 37620.0\n", encoding="utf-8")
        (prod / "prod.cpt").write_bytes(b"checkpoint")
        (prod / "prod.tpr").write_bytes(b"tpr")
        self.gmx = self.root / "gmx"
        self.gmx.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'dump':\n"
            " print('init-step = 0')\n"
            " print('nsteps = 25000000')\n"
            " print('init-t = 0')\n"
            " print('delta-t = 0.002')\n"
            " sys.exit(0)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        self.gmx.chmod(0o755)
        self.cfg = Config("TOKEN", 42, self.base, self.gmx, poll_seconds=1, stall_seconds=1800, stop_confirm_seconds=300)
        self.store = StateStore(self.root / "state.json")
        state = self.store.load()
        state.selected_run = str(self.run.resolve())
        state.last_stage = Stage.PROD.value
        self.store.save(state)
        self.engine = MonitorEngine(self.cfg, self.store, clock=lambda: 10_000.0)
        self.proc = ProcessInfo(18422, (str(self.gmx), "mdrun", "-deffnm", "04_prod/prod"), f"{self.gmx} mdrun -deffnm 04_prod/prod", self.run.resolve(), self.run.resolve(), Stage.PROD, "04_prod/prod")
        _BotHandler.update_batches = []
        _BotHandler.sent = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _BotHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.client = TelegramClient("TOKEN", api_base=f"http://{host}:{port}")
        self.app = BotApplication(self.cfg, self.store, engine=self.engine, telegram=self.client)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.td.cleanup()

    def _update(self, update_id, text):
        return {"update_id": update_id, "message": {"from": {"id": 42}, "chat": {"id": 99}, "text": text}}

    def test_fake_gmx_and_telegram_command_flow_and_monitor_dedup(self):
        os.utime(self.log, (9900.0, 9900.0))
        _BotHandler.update_batches.append([
            self._update(1, "/status"),
            self._update(2, "/stop"),
            self._update(3, "/confirmstop"),
        ])
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[self.proc]), \
             mock.patch("gromacs_monitor.app.inspect_pid", return_value=self.proc), \
             mock.patch("gromacs_monitor.app.force_kill_pid") as kill:
            self.app.poll_once()
        self.assertEqual(kill.call_count, 1)
        sent_texts = [text for _, text in _BotHandler.sent]
        self.assertTrue(any("Progress: 37.620 ns / 50.000 ns (75.24%)" in text for text in sent_texts))
        self.assertTrue(any("Pending stop" in text for text in sent_texts))

        os.utime(self.log, (8000.0, 8000.0))
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[self.proc]):
            self.app.monitor_cycle()
            self.app.monitor_cycle()
        stalled = [text for _, text in _BotHandler.sent if "GROMACS STALLED" in text]
        self.assertEqual(len(stalled), 1)

        self.engine.state.user_stop_signature = None
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[]):
            self.app.monitor_cycle()
        self.assertTrue(any("GROMACS STOPPED" in text for _, text in _BotHandler.sent))

    def test_poll_once_advances_offset(self):
        _BotHandler.update_batches.append([self._update(8, "/help")])
        self.app.poll_once()
        self.assertEqual(self.app.offset, 9)

    def test_run_forever_can_shutdown_without_touching_gromacs(self):
        class Client:
            def get_updates(inner, offset, timeout=25):
                self.app.shutdown_event.set()
                return []
            def send_message(inner, chat_id, text):
                pass
        self.app.telegram = Client()
        with mock.patch.object(self.engine, "inspect_once", wraps=self.engine.inspect_once), \
             mock.patch("gromacs_monitor.app.force_kill_pid") as kill:
            self.app.run_forever()
        self.assertTrue(self.app.shutdown_event.is_set())
        kill.assert_not_called()

    def test_telegram_failure_does_not_prevent_local_state_commit(self):
        class OfflineClient:
            def send_message(inner, chat_id, text):
                raise TelegramError("offline")

        self.app.telegram = OfflineClient()
        os.utime(self.log, (8000.0, 8000.0))
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[self.proc]):
            transition = self.app.monitor_cycle()

        saved = self.store.load()
        self.assertIn("STALLED", transition)
        self.assertEqual(saved.last_status, "STALLED")
        self.assertIsNotNone(saved.pending_notification)
        self.assertIn("STALLED", saved.pending_notification["text"])


if __name__ == "__main__":
    unittest.main()


class DeploymentStaticTests(unittest.TestCase):
    def test_service_config_and_installer_contract(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / "gromacs-telegram.service").read_text(encoding="utf-8")
        example = (root / "config.env.example").read_text(encoding="utf-8")
        installer = (root / "install.sh").read_text(encoding="utf-8")

        self.assertIn("Restart=always", service)
        self.assertIn("RestartSec=5", service)
        self.assertIn("KillMode=process", service)
        self.assertIn("WantedBy=default.target", service)
        self.assertIn("ExecStart=/usr/bin/python3 %h/.local/share/gromacs-telegram/gromacs_bot.py", service)
        self.assertNotIn("ExecStop=", service)
        self.assertNotIn("BindsTo=", service)
        self.assertNotIn("PartOf=", service)

        self.assertIn("TELEGRAM_BOT_" "TOKEN=\n", example)
        self.assertIn("TELEGRAM_USER_ID=\n", example)
        self.assertIn("BASE_DIR=/home/parsaoran66/Documents/Yehosyua/GROMACS/Run/runs", example)
        self.assertIn("GMX_PATH=/home/parsaoran66/opt/gromacs-2024.6/bin/gmx", example)
        self.assertIn("POLL_SECONDS=60", example)
        self.assertIn("STALL_SECONDS=1800", example)
        self.assertIn("STOP_CONFIRM_SECONDS=300", example)

        self.assertIn("set -euo pipefail", installer)
        self.assertIn("chmod 600", installer)
        self.assertIn("systemctl --user daemon-reload", installer)
        self.assertIn("systemctl --user enable gromacs-telegram.service", installer)
        self.assertIn("systemctl --user restart gromacs-telegram.service", installer)
        self.assertIn('loginctl enable-linger "$USER"', installer)
        self.assertNotIn("123:abc", installer)
        self.assertNotIn("123:abc", service)

    def test_readme_documents_operator_and_safety_flow(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        for phrase in [
            "BotFather", "/status", "/runs", "/use <n>", "/where", "/continue",
            "/stop", "/confirmstop", "/help", "1800", "TPR", "STOPPED BY USER",
            "MULTIPLE_PROCESS", "journalctl --user", "uninstall",
        ]:
            self.assertIn(phrase.lower(), readme.lower())
