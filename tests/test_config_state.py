import tempfile
import unittest
from pathlib import Path

from gromacs_monitor.config import load_config
from gromacs_monitor.model import PersistentState, RunStatus, Stage, STAGE_SPECS
from gromacs_monitor.state_store import StateStore


class ConfigStateTests(unittest.TestCase):
    def test_stage_catalog_is_exact(self):
        self.assertEqual(STAGE_SPECS[Stage.NVT].prefix, Path("02_nvt/nvt"))
        self.assertEqual(STAGE_SPECS[Stage.NPT].log_rel, Path("03_npt/npt.log"))
        self.assertEqual(STAGE_SPECS[Stage.PROD].cpt_rel, Path("04_prod/prod.cpt"))
        self.assertEqual(STAGE_SPECS[Stage.PROD].tpr_rel, Path("04_prod/prod.tpr"))

    def test_load_config_parses_required_values_and_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "config.env"
            cfg.write_text(
                "TELEGRAM_BOT_" "TOKEN=123:abc\n"
                "TELEGRAM_USER_ID=4242\n"
                f"BASE_DIR={root / 'runs'}\n"
                f"GMX_PATH={root / 'gmx'}\n",
                encoding="utf-8",
            )
            loaded = load_config(cfg)
            self.assertEqual(loaded.telegram_user_id, 4242)
            self.assertEqual(loaded.poll_seconds, 60)
            self.assertEqual(loaded.stall_seconds, 1800)
            self.assertEqual(loaded.stop_confirm_seconds, 300)

    def test_state_save_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            store = StateStore(path)
            state = PersistentState(selected_run="run-A", last_stage="PROD", last_status=RunStatus.STOPPED.value)
            store.save(state)
            self.assertEqual(store.load(), state)
            self.assertFalse((path.parent / (path.name + ".tmp")).exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_malformed_state_recovers_to_safe_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            state = StateStore(path).load()
            self.assertIsNone(state.selected_run)
            self.assertEqual(state.last_status, RunStatus.IDLE.value)

    def test_schema_incompatible_state_recovers_to_safe_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text(
                '{"selected_run":"/tmp/run","last_status":"RUNNING","last_runs":"not-a-list","user_stop_signature":[]}',
                encoding="utf-8",
            )
            state = StateStore(path).load()
            self.assertIsNone(state.selected_run)
            self.assertEqual(state.last_status, RunStatus.IDLE.value)
            self.assertEqual(state.last_runs, [])
            self.assertIsNone(state.user_stop_signature)

    def test_config_rejects_empty_secret_and_bad_user_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "config.env"
            cfg.write_text(
                "TELEGRAM_BOT_" "TOKEN=\nTELEGRAM_USER_ID=nope\n"
                f"BASE_DIR={root / 'runs'}\nGMX_PATH={root / 'gmx'}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(cfg)


if __name__ == "__main__":
    unittest.main()
