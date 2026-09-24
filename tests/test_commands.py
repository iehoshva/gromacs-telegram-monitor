import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gromacs_monitor.app import BotApplication, MonitorEngine
from gromacs_monitor.config import Config
from gromacs_monitor.model import MonitorSnapshot, ProcessInfo, Progress, RunStatus, Stage
from gromacs_monitor.state_store import StateStore
from gromacs_monitor.telegram_api import TelegramUpdate


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.base = self.root / "runs"
        self.base.mkdir()
        self.cfg = Config("token", 42, self.base, self.root / "gmx")
        self.store = StateStore(self.root / "state.json")
        self.engine = MonitorEngine(self.cfg, self.store, clock=lambda: 10000.0)
        self.app = BotApplication(self.cfg, self.store, engine=self.engine)

    def tearDown(self):
        self.td.cleanup()

    def update(self, text, user=42):
        return TelegramUpdate(1, user, 99, text)

    def make_run(self, name, stage):
        mapping = {Stage.NVT: ("02_nvt", "nvt.log"), Stage.NPT: ("03_npt", "npt.log"), Stage.PROD: ("04_prod", "prod.log")}
        folder, log = mapping[stage]
        path = self.base / name
        (path / folder).mkdir(parents=True)
        (path / folder / log).write_text("x")
        return path

    def test_unauthorized_user_gets_no_operational_details(self):
        self.make_run("secret-run", Stage.PROD)
        reply = self.app.handle_update(self.update("/runs", user=7))
        self.assertEqual(reply, "Unauthorized.")
        self.assertNotIn("secret-run", reply)

    def test_runs_are_numbered_and_use_persists_selection(self):
        self.make_run("charlie", Stage.PROD)
        self.make_run("Alpha", Stage.NVT)
        self.make_run("bravo", Stage.NPT)
        reply = self.app.handle_update(self.update("/runs"))
        self.assertIn("1. Alpha [NVT]", reply)
        self.assertIn("2. bravo [NPT]", reply)
        self.assertIn("3. charlie [PROD]", reply)
        self.assertEqual([Path(p).name for p in self.store.load().last_runs], ["Alpha", "bravo", "charlie"])
        selected = self.app.handle_update(self.update("/use 2"))
        self.assertIn("bravo", selected)
        self.assertEqual(Path(self.store.load().selected_run).name, "bravo")
        self.assertEqual(self.store.load().last_stage, Stage.NPT.value)
        restarted = MonitorEngine(self.cfg, self.store, clock=lambda: 10001.0)
        self.assertEqual(Path(restarted.state.selected_run).name, "bravo")
        self.assertEqual(restarted.state.last_stage, Stage.NPT.value)

    def test_use_rejects_invalid_index(self):
        self.assertIn("Invalid", self.app.handle_update(self.update("/use nope")))
        self.assertIn("Invalid", self.app.handle_update(self.update("/use 1")))

    def test_use_revalidates_symlink_that_now_escapes_base(self):
        outside = self.root / "outside"
        (outside / "04_prod").mkdir(parents=True)
        (outside / "04_prod" / "prod.log").write_text("x")
        safe = self.make_run("safe", Stage.PROD)
        link = self.base / "link"
        link.symlink_to(safe, target_is_directory=True)
        state = self.store.load()
        state.last_runs = [str(link)]
        self.store.save(state)
        link.unlink()
        link.symlink_to(outside, target_is_directory=True)
        self.engine.state = self.store.load()
        self.assertIn("no longer valid", self.app.handle_update(self.update("/use 1")))

    def test_where_returns_selected_full_path(self):
        run = self.make_run("run-A", Stage.PROD)
        state = self.store.load()
        state.selected_run = str(run.resolve())
        self.store.save(state)
        self.engine.state = self.store.load()
        self.assertEqual(self.app.handle_update(self.update("/where")), str(run.resolve()))

    def test_status_formats_progress_process_log_age_and_checkpoint(self):
        run = self.make_run("run-A", Stage.PROD)
        proc = ProcessInfo(18422, ("/x/gmx", "mdrun"), "/x/gmx mdrun", run, run, Stage.PROD, "04_prod/prod")
        snap = MonitorSnapshot(
            RunStatus.RUNNING,
            10000.0,
            run_root=run,
            stage=Stage.PROD,
            process=proc,
            log_mtime=9880.0,
            log_age_seconds=120.0,
            checkpoint_exists=True,
            progress=Progress(37.62, 50.0, 75.24),
        )
        with mock.patch.object(self.engine, "inspect_once", return_value=snap):
            reply = self.app.handle_update(self.update("/status"))
        self.assertIn("Status: RUNNING", reply)
        self.assertIn("Stage: PROD", reply)
        self.assertIn("Progress: 37.620 ns / 50.000 ns (75.24%)", reply)
        self.assertIn("PID: 18422", reply)
        self.assertIn("Last log update: 2m 0s ago", reply)
        self.assertIn("Checkpoint: yes", reply)

    def test_status_does_not_guess_target(self):
        snap = MonitorSnapshot(RunStatus.STOPPED, 10000.0, progress=Progress(37.62, None, None))
        with mock.patch.object(self.engine, "inspect_once", return_value=snap):
            reply = self.app.handle_update(self.update("/status"))
        self.assertIn("Progress: 37.620 ns / target unavailable", reply)

    def test_help_lists_exact_command_set(self):
        reply = self.app.handle_update(self.update("/help"))
        for command in ["/status", "/runs", "/use <n>", "/where", "/continue", "/stop", "/confirmstop", "/help"]:
            self.assertIn(command, reply)
        self.assertIn("confirm", reply.lower())


if __name__ == "__main__":
    unittest.main()

from gromacs_monitor.gromacs import choose_continuation_stage, launch_continue


class ControlCommandTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.base = self.root / "runs"
        self.base.mkdir()
        self.cfg = Config("token", 42, self.base, self.root / "gmx")
        self.store = StateStore(self.root / "state.json")
        self.engine = MonitorEngine(self.cfg, self.store, clock=lambda: 10000.0)
        self.app = BotApplication(self.cfg, self.store, engine=self.engine)
        self.run = self.make_run("run-A", Stage.PROD)
        (self.run / "04_prod" / "prod.cpt").write_bytes(b"cpt")
        state = self.store.load()
        state.selected_run = str(self.run.resolve())
        state.last_stage = Stage.PROD.value
        self.store.save(state)
        self.engine.state = self.store.load()
        self.proc = ProcessInfo(
            18422,
            ("/home/parsaoran66/opt/gromacs-2024.6/bin/gmx", "mdrun", "-deffnm", "04_prod/prod"),
            "/home/parsaoran66/opt/gromacs-2024.6/bin/gmx mdrun -deffnm 04_prod/prod",
            self.run.resolve(), self.run.resolve(), Stage.PROD, "04_prod/prod",
        )

    def tearDown(self):
        self.td.cleanup()

    def update(self, text, user=42):
        return TelegramUpdate(1, user, 99, text)

    def make_run(self, name, stage):
        mapping = {Stage.NVT: ("02_nvt", "nvt.log"), Stage.NPT: ("03_npt", "npt.log"), Stage.PROD: ("04_prod", "prod.log")}
        folder, log = mapping[stage]
        path = self.base / name
        (path / folder).mkdir(parents=True, exist_ok=True)
        (path / folder / log).write_text("x")
        return path

    def test_choose_continuation_stage_prefers_valid_remembered_then_highest(self):
        npt = self.run / "03_npt"
        npt.mkdir()
        (npt / "npt.log").write_text("x")
        (npt / "npt.cpt").write_bytes(b"x")
        self.assertEqual(choose_continuation_stage(self.run, Stage.NPT), Stage.NPT)
        self.assertEqual(choose_continuation_stage(self.run, None), Stage.PROD)

    @mock.patch("gromacs_monitor.app.find_mdrun_processes")
    def test_continue_rejects_any_live_mdrun(self, find):
        find.return_value = [self.proc]
        reply = self.app.handle_update(self.update("/continue"))
        self.assertIn("already active", reply)

    def test_continue_rejects_no_selected_run(self):
        self.engine.state.selected_run = None
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[]):
            self.assertIn("No run selected", self.app.handle_update(self.update("/continue")))

    def test_continue_rejects_selected_run_outside_base(self):
        outside = self.root / "outside"
        (outside / "04_prod").mkdir(parents=True)
        (outside / "04_prod" / "prod.log").write_text("x")
        (outside / "04_prod" / "prod.cpt").write_bytes(b"x")
        self.engine.state.selected_run = str(outside)
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[]):
            self.assertIn("invalid", self.app.handle_update(self.update("/continue")).lower())

    def test_continue_rejects_missing_checkpoint(self):
        (self.run / "04_prod" / "prod.cpt").unlink()
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[]), \
             mock.patch("gromacs_monitor.app.launch_continue") as launch:
            reply = self.app.handle_update(self.update("/continue"))
        self.assertIn("checkpoint", reply.lower())
        launch.assert_not_called()

    @mock.patch("gromacs_monitor.app.launch_continue")
    @mock.patch("gromacs_monitor.app.find_mdrun_processes")
    def test_continue_rejects_multiple_processes(self, find, launch):
        find.return_value = [self.proc, self.proc]
        reply = self.app.handle_update(self.update("/continue"))
        self.assertIn("multiple", reply.lower())
        launch.assert_not_called()

    @mock.patch("gromacs_monitor.app.launch_continue", return_value=9001)
    @mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[])
    def test_continue_launches_remembered_stage(self, find, launch):
        reply = self.app.handle_update(self.update("/continue"))
        launch.assert_called_once_with(self.cfg, self.run.resolve(), Stage.PROD)
        self.assertIn("9001", reply)

    @mock.patch("gromacs_monitor.gromacs.subprocess.Popen")
    def test_launch_continue_uses_exact_argv_and_detached_process(self, popen):
        popen.return_value.pid = 321
        pid = launch_continue(self.cfg, self.run.resolve(), Stage.PROD)
        self.assertEqual(pid, 321)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, [str(self.cfg.gmx_path), "mdrun", "-deffnm", "04_prod/prod", "-cpi", "04_prod/prod.cpt", "-append"])
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["cwd"], str(self.run.resolve()))
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])

    @mock.patch("gromacs_monitor.app.force_kill_pid")
    @mock.patch("gromacs_monitor.app.find_mdrun_processes")
    def test_stop_only_creates_pending_confirmation(self, find, kill):
        find.return_value = [self.proc]
        reply = self.app.handle_update(self.update("/stop"))
        self.assertIn("18422", reply)
        self.assertIn("/confirmstop", reply)
        self.assertIsNotNone(self.store.load().pending_stop)
        kill.assert_not_called()

    @mock.patch("gromacs_monitor.app.force_kill_pid")
    @mock.patch("gromacs_monitor.app.inspect_pid")
    @mock.patch("gromacs_monitor.app.find_mdrun_processes")
    def test_confirmstop_exact_match_kills_once(self, find, inspect, kill):
        find.return_value = [self.proc]
        self.app.handle_update(self.update("/stop"))
        inspect.return_value = self.proc
        reply = self.app.handle_update(self.update("/confirmstop"))
        kill.assert_called_once_with(18422)
        self.assertIn("killed", reply.lower())
        saved = self.store.load()
        self.assertIsNone(saved.pending_stop)
        self.assertEqual(saved.user_stop_signature["pid"], 18422)

    @mock.patch("gromacs_monitor.app.force_kill_pid")
    @mock.patch("gromacs_monitor.app.inspect_pid")
    @mock.patch("gromacs_monitor.app.find_mdrun_processes")
    def test_confirmstop_rejects_pid_reuse_changed_argv(self, find, inspect, kill):
        find.return_value = [self.proc]
        self.app.handle_update(self.update("/stop"))
        changed = ProcessInfo(
            18422, ("/x/gmx", "mdrun", "-deffnm", "03_npt/npt"), "changed",
            self.run.resolve(), self.run.resolve(), Stage.NPT, "03_npt/npt"
        )
        inspect.return_value = changed
        reply = self.app.handle_update(self.update("/confirmstop"))
        self.assertIn("changed", reply.lower())
        kill.assert_not_called()

    @mock.patch("gromacs_monitor.app.force_kill_pid")
    @mock.patch("gromacs_monitor.app.inspect_pid", return_value=None)
    def test_confirmstop_rejects_vanished_pid(self, inspect, kill):
        self.engine.state.pending_stop = {
            "pid": 18422, "argv": list(self.proc.argv), "cwd": str(self.proc.cwd),
            "run_root": str(self.proc.run_root), "stage": Stage.PROD.value, "expires_at": 10100.0,
        }
        self._save_engine_state()
        reply = self.app.handle_update(self.update("/confirmstop"))
        self.assertIn("no longer", reply.lower())
        kill.assert_not_called()

    def _save_engine_state(self):
        self.store.save(self.engine.state)

    @mock.patch("gromacs_monitor.app.force_kill_pid")
    def test_confirmstop_rejects_expired_confirmation(self, kill):
        self.engine.state.pending_stop = {
            "pid": 18422, "argv": list(self.proc.argv), "cwd": str(self.proc.cwd),
            "run_root": str(self.proc.run_root), "stage": Stage.PROD.value, "expires_at": 9999.0,
        }
        self._save_engine_state()
        reply = self.app.handle_update(self.update("/confirmstop"))
        self.assertIn("expired", reply.lower())
        kill.assert_not_called()

    @mock.patch("gromacs_monitor.app.force_kill_pid")
    @mock.patch("gromacs_monitor.app.find_mdrun_processes")
    def test_stop_rejects_multiple_processes(self, find, kill):
        find.return_value = [self.proc, self.proc]
        reply = self.app.handle_update(self.update("/stop"))
        self.assertIn("multiple", reply.lower())
        kill.assert_not_called()
