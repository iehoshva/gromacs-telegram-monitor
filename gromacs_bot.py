#!/usr/bin/env python3
import argparse
import signal
import threading
from pathlib import Path

from gromacs_monitor.app import BotApplication
from gromacs_monitor.config import load_config
from gromacs_monitor.state_store import StateStore
from gromacs_monitor.telegram_api import TelegramClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor GROMACS through a private Telegram bot")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("~/.config/gromacs-telegram/config.env").expanduser(),
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("~/.local/state/gromacs-telegram/state.json").expanduser(),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    store = StateStore(args.state)
    shutdown = threading.Event()
    client = TelegramClient(config.telegram_bot_token)
    app = BotApplication(config, store, telegram=client, shutdown_event=shutdown)

    def stop_handler(signum, frame):
        shutdown.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    app.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
