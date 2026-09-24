import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gromacs_monitor.config import Config
from gromacs_monitor.model import MonitorSnapshot, ProcessInfo, Progress, RunStatus, Stage
from gromacs_monitor.state_store import StateStore
from gromacs_monitor.app import MonitorEngine


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.base = self.root / "runs"
        self.run = self.base / "run-A"
        (self.run / "04_prod").mkdir(parents=True)
        self.log = self.run / "04_prod" / "prod.log"
        self.log.write_text("Step Time\n1 1.0\n", encoding="utf-8")
        self.config = Config("token", 42, self.base, self.root / "gmx", poll_seconds=60, stall_seconds=1800, stop_confirm_seconds=300)
        self.store = StateStore(self.root / "state.json")
        self.now = 10_000.0
        self.process = ProcessInfo(123, ("/x/gmx", "mdrun", "-deffnm", "04_prod/prod"), "/x/gmx mdrun -deffnm 04_prod/prod", self.run, self.run, Stage.PROD, "04_prod/prod")

    def tearDown(self):
        self.td.cleanup()

    def _set_age(self, age):
        os.utime(self.log, (self.now - age, self.now - age))

    def snapshot_at_age(self, age):
        self._set_age(age)
        engine = MonitorEngine(self.config, self.store, clock=lambda: self.now)
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[self.process]), \
             mock.patch("gromacs_monitor.app.read_progress", return_value=Progress(1.0, 2.0, 50.0)):
            return engine.inspect_once()

    def test_running_stalled_boundary_is_exact(self):
        self.assertEqual(self.snapshot_at_age(1799.0).status, RunStatus.RUNNING)
        self.assertEqual(self.snapshot_at_age(1800.0).status, RunStatus.RUNNING)
        self.assertEqual(self.snapshot_at_age(1800.001).status, RunStatus.STALLED)

    def test_stale_log_without_process_is_stopped_not_stalled(self):
        self._set_age(9999)
        state = self.store.load()
        state.selected_run = str(self.run)
        state.last_stage = Stage.PROD.value
        self.store.save(state)
        engine = MonitorEngine(self.config, self.store, clock=lambda: self.now)
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[]), \
             mock.patch("gromacs_monitor.app.log_has_finished", return_value=False), \
             mock.patch("gromacs_monitor.app.read_progress", return_value=Progress(1, 2, 50)):
            self.assertEqual(engine.inspect_once().status, RunStatus.STOPPED)

    def test_no_process_finished_marker_is_completed(self):
        state = self.store.load()
        state.selected_run = str(self.run)
        state.last_stage = Stage.PROD.value
        self.store.save(state)
        engine = MonitorEngine(self.config, self.store, clock=lambda: self.now)
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[]), \
             mock.patch("gromacs_monitor.app.log_has_finished", return_value=True), \
             mock.patch("gromacs_monitor.app.read_progress", return_value=Progress(2, 2, 100)):
            self.assertEqual(engine.inspect_once().status, RunStatus.COMPLETED)

    def test_two_processes_is_multiple_process(self):
        engine = MonitorEngine(self.config, self.store, clock=lambda: self.now)
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[self.process, self.process]):
            self.assertEqual(engine.inspect_once().status, RunStatus.MULTIPLE_PROCESS)

    def test_unsupported_stage_is_idle(self):
        bad = ProcessInfo(123, ("/x/gmx", "mdrun", "-deffnm", "01_em/em"), "cmd", self.run, self.run, None, "01_em/em")
        engine = MonitorEngine(self.config, self.store, clock=lambda: self.now)
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[bad]):
            snap = engine.inspect_once()
        self.assertEqual(snap.status, RunStatus.IDLE)
        self.assertIn("unsupported", snap.diagnostic.lower())

    def test_live_supported_process_with_missing_log_is_idle(self):
        self.log.unlink()
        engine = MonitorEngine(self.config, self.store, clock=lambda: self.now)
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[self.process]):
            snap = engine.inspect_once()
        self.assertEqual(snap.status, RunStatus.IDLE)
        self.assertIn("log", snap.diagnostic.lower())

    def test_selected_run_with_missing_log_is_idle(self):
        self.log.unlink()
        state = self.store.load()
        state.selected_run = str(self.run)
        state.last_stage = Stage.PROD.value
        self.store.save(state)
        engine = MonitorEngine(self.config, self.store, clock=lambda: self.now)
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[]):
            self.assertEqual(engine.inspect_once().status, RunStatus.IDLE)

    def test_identical_stalled_transition_notifies_once_then_running_again(self):
        engine = MonitorEngine(self.config, self.store, clock=lambda: self.now)
        stalled = self.snapshot_at_age(1900)
        first = engine.commit_snapshot(stalled)
        second = engine.commit_snapshot(stalled)
        running = self.snapshot_at_age(10)
        third = engine.commit_snapshot(running)
        self.assertIn("STALLED", first)
        self.assertIsNone(second)
        self.assertIn("RUNNING AGAIN", third)

    def test_user_stop_signature_labels_next_stopped_transition(self):
        state = self.store.load()
        state.last_status = RunStatus.RUNNING.value
        state.selected_run = str(self.run)
        state.last_stage = Stage.PROD.value
        state.user_stop_signature = {"run_root": str(self.run.resolve()), "stage": Stage.PROD.value, "pid": 123, "time": self.now - 1}
        self.store.save(state)
        engine = MonitorEngine(self.config, self.store, clock=lambda: self.now)
        with mock.patch("gromacs_monitor.app.find_mdrun_processes", return_value=[]), \
             mock.patch("gromacs_monitor.app.log_has_finished", return_value=False):
            stopped = engine.inspect_once()
        message = engine.commit_snapshot(stopped)
        self.assertIn("STOPPED BY USER", message)
        self.assertIsNone(self.store.load().user_stop_signature)

    def test_new_live_process_clears_stale_user_stop_signature(self):
        state = self.store.load()
        state.last_status = RunStatus.STOPPED.value
        state.selected_run = str(self.run.resolve())
        state.last_stage = Stage.PROD.value
        state.user_stop_signature = {
            "run_root": str(self.run.resolve()),
            "stage": Stage.PROD.value,
            "pid": 123,
            "time": self.now - 10,
        }
        self.store.save(state)
        engine = MonitorEngine(self.config, self.store, clock=lambda: self.now)
        new_process = ProcessInfo(456, self.process.argv, self.process.command_text, self.run, self.run, Stage.PROD, "04_prod/prod")
        running = MonitorSnapshot(
            RunStatus.RUNNING,
            self.now,
            run_root=self.run,
            stage=Stage.PROD,
            process=new_process,
            log_mtime=self.now,
            log_age_seconds=0.0,
            checkpoint_exists=True,
            progress=Progress(1.0, 2.0, 50.0),
        )
        engine.commit_snapshot(running)
        self.assertIsNone(self.store.load().user_stop_signature)


if __name__ == "__main__":
    unittest.main()
