import tempfile
import unittest
from pathlib import Path

from gromacs_monitor.gromacs import discover_runs, highest_logged_stage, resolve_run_under_base
from gromacs_monitor.model import Stage


class DiscoveryTests(unittest.TestCase):
    def test_only_exact_stage_logs_make_a_run_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "only-dir" / "04_prod").mkdir(parents=True)
            (base / "nvt-run" / "02_nvt").mkdir(parents=True)
            (base / "nvt-run" / "02_nvt" / "nvt.log").write_text("x")
            (base / "npt-run" / "03_npt").mkdir(parents=True)
            (base / "npt-run" / "03_npt" / "npt.log").write_text("x")
            (base / "prod-run" / "04_prod").mkdir(parents=True)
            (base / "prod-run" / "04_prod" / "prod.log").write_text("x")
            found = discover_runs(base)
            self.assertEqual([r.path.name for r in found], ["npt-run", "nvt-run", "prod-run"])
            self.assertEqual(highest_logged_stage(base / "prod-run"), Stage.PROD)
            self.assertEqual(highest_logged_stage(base / "only-dir"), None)

    def test_resolved_path_outside_base_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base = root / "runs"
            base.mkdir()
            inside = base / "safe"
            inside.mkdir()
            outside = root / "outside"
            outside.mkdir()
            self.assertEqual(resolve_run_under_base(base, inside), inside.resolve())
            self.assertIsNone(resolve_run_under_base(base, outside))

    def test_symlink_child_resolving_outside_is_not_discovered(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base = root / "runs"
            base.mkdir()
            outside = root / "outside"
            (outside / "04_prod").mkdir(parents=True)
            (outside / "04_prod" / "prod.log").write_text("x")
            (base / "escape").symlink_to(outside, target_is_directory=True)
            self.assertEqual(discover_runs(base), [])


if __name__ == "__main__":
    unittest.main()

import os
from unittest import mock

from gromacs_monitor.gromacs import find_mdrun_processes, inspect_pid, parse_stage_from_argv


class ProcessTests(unittest.TestCase):
    def test_parse_supported_deffnm_forms(self):
        self.assertEqual(parse_stage_from_argv(["/x/gmx", "mdrun", "-deffnm", "02_nvt/nvt"]), (Stage.NVT, "02_nvt/nvt"))
        self.assertEqual(parse_stage_from_argv(["/x/gmx", "mdrun", "-deffnm=03_npt/npt"]), (Stage.NPT, "03_npt/npt"))
        self.assertEqual(parse_stage_from_argv(["/x/gmx", "mdrun", "-deffnm", "04_prod/prod"]), (Stage.PROD, "04_prod/prod"))

    def test_unsupported_deffnm_is_not_guessed(self):
        self.assertEqual(parse_stage_from_argv(["gmx", "mdrun", "-deffnm", "01_em/em"]), (None, "01_em/em"))

    def test_parent_traversal_deffnm_is_not_normalized_into_supported_stage(self):
        self.assertEqual(
            parse_stage_from_argv(["gmx", "mdrun", "-deffnm", "../04_prod/prod"]),
            (None, "../04_prod/prod"),
        )

    def _fake_proc(self, root, pid, argv, cwd):
        proc_dir = root / str(pid)
        proc_dir.mkdir(parents=True)
        (proc_dir / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        os.symlink(cwd, proc_dir / "cwd")

    def test_inspect_pid_accepts_supported_process_under_base(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base = root / "runs"
            run = base / "run-A"
            run.mkdir(parents=True)
            proc = root / "proc"
            proc.mkdir()
            self._fake_proc(proc, 123, ["/opt/gmx", "mdrun", "-deffnm", "03_npt/npt"], run)
            info = inspect_pid(123, base, proc_root=proc)
            self.assertIsNotNone(info)
            self.assertEqual(info.stage, Stage.NPT)
            self.assertEqual(info.run_root, run.resolve())

    def test_outside_base_process_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base = root / "runs"
            base.mkdir()
            outside = root / "outside"
            outside.mkdir()
            proc = root / "proc"
            proc.mkdir()
            self._fake_proc(proc, 123, ["/opt/gmx", "mdrun", "-deffnm", "04_prod/prod"], outside)
            self.assertIsNone(inspect_pid(123, base, proc_root=proc))

    def test_unsupported_stage_is_retained_for_diagnostics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base = root / "runs"
            run = base / "run-A"
            run.mkdir(parents=True)
            proc = root / "proc"
            proc.mkdir()
            self._fake_proc(proc, 123, ["/opt/gmx", "mdrun", "-deffnm", "01_em/em"], run)
            info = inspect_pid(123, base, proc_root=proc)
            self.assertIsNotNone(info)
            self.assertIsNone(info.stage)

    @mock.patch("gromacs_monitor.gromacs.subprocess.run")
    @mock.patch("gromacs_monitor.gromacs.inspect_pid")
    def test_find_processes_retains_two_valid_matches(self, inspect, run):
        run.return_value = mock.Mock(returncode=0, stdout="101 /x/gmx mdrun\n202 /x/gmx mdrun\n")
        one = mock.Mock(pid=101)
        two = mock.Mock(pid=202)
        inspect.side_effect = [one, two]
        found = find_mdrun_processes(Path("/tmp/runs"))
        self.assertEqual(found, [one, two])
        self.assertEqual(inspect.call_count, 2)
