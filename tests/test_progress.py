import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gromacs_monitor.gromacs import log_has_finished, read_current_time_ps, read_progress, read_target_time_ps
from gromacs_monitor.model import Stage


class ProgressTests(unittest.TestCase):
    def test_reads_last_step_time_pair(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "prod.log"
            log.write_text(
                "Step           Time\n"
                "1000        2.00000\n"
                "noise\n"
                "Step           Time\n"
                "25000000    50000.00000\n",
                encoding="utf-8",
            )
            self.assertEqual(read_current_time_ps(log), 50000.0)

    def test_finished_marker_only_needs_log_tail(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "prod.log"
            log.write_text("x\nFinished mdrun\n", encoding="utf-8")
            self.assertTrue(log_has_finished(log))

    @mock.patch("gromacs_monitor.gromacs.subprocess.run")
    def test_reads_target_from_dump(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout=(
            "init-step = 1000\n"
            "nsteps = 24999000\n"
            "init-t = 0\n"
            " delta-t = 0.002\n"
        ), stderr="")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tpr = root / "prod.tpr"
            tpr.write_bytes(b"x")
            self.assertEqual(read_target_time_ps(root / "gmx", tpr, root), 50000.0)

    @mock.patch("gromacs_monitor.gromacs.subprocess.run")
    def test_accepts_tinit_dt_aliases(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="init-step = 0\nnsteps = 1000000\ntinit = 5\ndt = 0.002\n", stderr="")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tpr = root / "nvt.tpr"
            tpr.write_bytes(b"x")
            self.assertEqual(read_target_time_ps(root / "gmx", tpr, root), 2005.0)

    @mock.patch("gromacs_monitor.gromacs.subprocess.run")
    def test_negative_nsteps_has_no_finite_target(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="init-step = 0\nnsteps = -1\ntinit = 0\ndt = 0.002\n", stderr="")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tpr = root / "prod.tpr"
            tpr.write_bytes(b"x")
            self.assertIsNone(read_target_time_ps(root / "gmx", tpr, root))

    @mock.patch("gromacs_monitor.gromacs.subprocess.run")
    def test_malformed_dump_returns_none(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="nsteps = bananas\n", stderr="")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tpr = root / "prod.tpr"
            tpr.write_bytes(b"x")
            self.assertIsNone(read_target_time_ps(root / "gmx", tpr, root))

    @mock.patch("gromacs_monitor.gromacs.subprocess.run")
    def test_missing_tpr_does_not_invoke_gmx(self, run):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertIsNone(read_target_time_ps(root / "gmx", root / "missing.tpr", root))
            run.assert_not_called()

    @mock.patch("gromacs_monitor.gromacs.subprocess.run")
    def test_command_failure_returns_none(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="bad")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tpr = root / "prod.tpr"
            tpr.write_bytes(b"x")
            self.assertIsNone(read_target_time_ps(root / "gmx", tpr, root))

    @mock.patch("gromacs_monitor.gromacs.read_target_time_ps", return_value=50000.0)
    def test_read_progress_converts_ps_to_ns(self, target):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "04_prod" / "prod.log"
            log.parent.mkdir()
            log.write_text("Step Time\n18810000 37620.0\n")
            progress = read_progress(root / "gmx", root, Stage.PROD)
            self.assertAlmostEqual(progress.current_ns, 37.62)
            self.assertEqual(progress.target_ns, 50.0)
            self.assertAlmostEqual(progress.percent, 75.24)

    @mock.patch("gromacs_monitor.gromacs.read_target_time_ps", return_value=None)
    def test_read_progress_keeps_current_when_target_unavailable(self, target):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "02_nvt" / "nvt.log"
            log.parent.mkdir()
            log.write_text("Step Time\n1000 2.0\n")
            progress = read_progress(root / "gmx", root, Stage.NVT)
            self.assertEqual(progress.current_ns, 0.002)
            self.assertIsNone(progress.target_ns)
            self.assertIsNone(progress.percent)


if __name__ == "__main__":
    unittest.main()
