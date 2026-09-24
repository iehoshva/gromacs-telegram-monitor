from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_user_id: int
    base_dir: Path
    gmx_path: Path
    poll_seconds: int = 60
    stall_seconds: int = 1800
    stop_confirm_seconds: int = 300


def _require(values, key):
    value = values.get(key, "").strip()
    if not value:
        raise ValueError(f"Missing required configuration key: {key}")
    return value


def _positive_int(values, key, default):
    raw = values.get(key, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def load_config(path: Path) -> Config:
    values = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("Invalid configuration line")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    token = _require(values, "TELEGRAM_BOT_TOKEN")
    user_raw = _require(values, "TELEGRAM_USER_ID")
    try:
        user_id = int(user_raw)
    except ValueError as exc:
        raise ValueError("TELEGRAM_USER_ID must be an integer") from exc

    return Config(
        telegram_bot_token=token,
        telegram_user_id=user_id,
        base_dir=Path(_require(values, "BASE_DIR")).expanduser().resolve(),
        gmx_path=Path(_require(values, "GMX_PATH")).expanduser().resolve(),
        poll_seconds=_positive_int(values, "POLL_SECONDS", 60),
        stall_seconds=_positive_int(values, "STALL_SECONDS", 1800),
        stop_confirm_seconds=_positive_int(values, "STOP_CONFIRM_SECONDS", 300),
    )
