import json
import os
from dataclasses import asdict, fields
from pathlib import Path

from .model import PersistentState, RunStatus, Stage


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> PersistentState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state must be object")
            allowed = {field.name for field in fields(PersistentState)}
            if any(key not in allowed for key in raw):
                raise ValueError("unknown state field")
            self._validate_payload(raw)
            return PersistentState(**raw)
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return PersistentState()

    @staticmethod
    def _validate_payload(raw: dict) -> None:
        optional_strings = ("selected_run", "last_stage", "last_notification_state")
        for key in optional_strings:
            value = raw.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"invalid state field: {key}")

        if raw.get("last_stage") is not None:
            Stage(raw["last_stage"])

        status = raw.get("last_status", RunStatus.IDLE.value)
        if not isinstance(status, str):
            raise ValueError("invalid state field: last_status")
        RunStatus(status)

        notification_state = raw.get("last_notification_state")
        if notification_state is not None:
            RunStatus(notification_state)

        mtime = raw.get("last_log_mtime")
        if mtime is not None and (isinstance(mtime, bool) or not isinstance(mtime, (int, float))):
            raise ValueError("invalid state field: last_log_mtime")

        last_runs = raw.get("last_runs", [])
        if not isinstance(last_runs, list) or not all(isinstance(item, str) for item in last_runs):
            raise ValueError("invalid state field: last_runs")

        for key in ("pending_stop", "pending_notification", "user_stop_signature"):
            value = raw.get(key)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"invalid state field: {key}")

    def save(self, state: PersistentState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / (self.path.name + ".tmp")
        data = json.dumps(asdict(state), indent=2, sort_keys=True)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(data)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)
