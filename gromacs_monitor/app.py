import sys
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .config import Config
from .gromacs import (
    choose_continuation_stage,
    discover_runs,
    find_mdrun_processes,
    force_kill_pid,
    inspect_pid,
    launch_continue,
    highest_logged_stage,
    log_has_finished,
    read_progress,
    resolve_run_under_base,
)
from .model import MonitorSnapshot, PendingStop, Progress, RunStatus, STAGE_SPECS, Stage
from .state_store import StateStore
from .telegram_api import TelegramClient, TelegramError, TelegramUpdate


class MonitorEngine:
    def __init__(self, config: Config, store: StateStore, clock=time.time):
        self.config = config
        self.store = store
        self.clock = clock
        self.state = store.load()

    def _stage_from_state(self) -> Optional[Stage]:
        if not self.state.last_stage:
            return None
        try:
            return Stage(self.state.last_stage)
        except ValueError:
            return None

    def _valid_selected_run(self) -> Optional[Path]:
        if not self.state.selected_run:
            return None
        resolved = resolve_run_under_base(self.config.base_dir, Path(self.state.selected_run))
        if resolved is None:
            return None
        base = self.config.base_dir.resolve()
        try:
            rel = resolved.relative_to(base)
        except ValueError:
            return None
        if len(rel.parts) != 1 or not resolved.is_dir():
            return None
        return resolved

    def inspect_once(self) -> MonitorSnapshot:
        now = float(self.clock())
        processes = find_mdrun_processes(self.config.base_dir)
        if len(processes) > 1:
            return MonitorSnapshot(
                RunStatus.MULTIPLE_PROCESS,
                now,
                diagnostic=f"{len(processes)} controllable gmx mdrun processes found",
            )

        if len(processes) == 1:
            process = processes[0]
            if process.stage is None:
                return MonitorSnapshot(
                    RunStatus.IDLE,
                    now,
                    run_root=process.run_root,
                    process=process,
                    diagnostic="unsupported deffnm; remote control disabled",
                )
            stage = process.stage
            spec = STAGE_SPECS[stage]
            log_path = process.run_root / spec.log_rel
            if not log_path.is_file():
                return MonitorSnapshot(
                    RunStatus.IDLE,
                    now,
                    run_root=process.run_root,
                    stage=stage,
                    process=process,
                    checkpoint_exists=(process.run_root / spec.cpt_rel).is_file(),
                    diagnostic="stage log unavailable",
                )
            try:
                mtime = log_path.stat().st_mtime
            except OSError:
                return MonitorSnapshot(
                    RunStatus.IDLE,
                    now,
                    run_root=process.run_root,
                    stage=stage,
                    process=process,
                    diagnostic="stage log unavailable",
                )
            age = now - mtime
            status = RunStatus.RUNNING if age <= self.config.stall_seconds else RunStatus.STALLED
            return MonitorSnapshot(
                status,
                now,
                run_root=process.run_root,
                stage=stage,
                process=process,
                log_mtime=mtime,
                log_age_seconds=age,
                checkpoint_exists=(process.run_root / spec.cpt_rel).is_file(),
                progress=read_progress(self.config.gmx_path, process.run_root, stage),
            )

        run_root = self._valid_selected_run()
        if run_root is None:
            return MonitorSnapshot(RunStatus.IDLE, now, diagnostic="no selected run")
        stage = self._stage_from_state() or highest_logged_stage(run_root)
        if stage is None:
            return MonitorSnapshot(RunStatus.IDLE, now, run_root=run_root, diagnostic="no remembered stage")
        spec = STAGE_SPECS[stage]
        log_path = run_root / spec.log_rel
        if not log_path.is_file():
            return MonitorSnapshot(RunStatus.IDLE, now, run_root=run_root, stage=stage, diagnostic="stage log unavailable")
        try:
            mtime = log_path.stat().st_mtime
            age = now - mtime
        except OSError:
            mtime = None
            age = None
        status = RunStatus.COMPLETED if log_has_finished(log_path) else RunStatus.STOPPED
        return MonitorSnapshot(
            status,
            now,
            run_root=run_root,
            stage=stage,
            log_mtime=mtime,
            log_age_seconds=age,
            checkpoint_exists=(run_root / spec.cpt_rel).is_file(),
            progress=read_progress(self.config.gmx_path, run_root, stage),
        )

    def _matches_user_stop(self, snapshot: MonitorSnapshot) -> bool:
        signature = self.state.user_stop_signature
        if not signature or snapshot.run_root is None or snapshot.stage is None:
            return False
        return (
            signature.get("run_root") == str(snapshot.run_root.resolve())
            and signature.get("stage") == snapshot.stage.value
        )

    def _render_transition(self, old: RunStatus, snapshot: MonitorSnapshot) -> str:
        if snapshot.status == RunStatus.RUNNING and old == RunStatus.STALLED:
            headline = "GROMACS RUNNING AGAIN"
        elif snapshot.status == RunStatus.STOPPED and self._matches_user_stop(snapshot):
            headline = "GROMACS STOPPED BY USER"
            self.state.user_stop_signature = None
        elif snapshot.status == RunStatus.MULTIPLE_PROCESS:
            headline = "GROMACS MULTIPLE PROCESS"
        else:
            headline = f"GROMACS {snapshot.status.value}"
        details = []
        if snapshot.run_root is not None:
            details.append(f"Run: {snapshot.run_root.name}")
        if snapshot.stage is not None:
            details.append(f"Stage: {snapshot.stage.value}")
        progress = snapshot.progress
        if progress.current_ns is not None and progress.target_ns is not None and progress.percent is not None:
            details.append(f"Progress: {progress.current_ns:.3f} / {progress.target_ns:.3f} ns ({progress.percent:.2f}%)")
        elif progress.current_ns is not None:
            details.append(f"Progress: {progress.current_ns:.3f} ns / target unavailable")
        return "\n".join([headline] + details)

    def commit_snapshot(self, snapshot: MonitorSnapshot) -> Optional[str]:
        signature = self.state.user_stop_signature
        if signature and snapshot.process is not None:
            try:
                stopped_pid = int(signature.get("pid"))
            except (TypeError, ValueError):
                stopped_pid = None
            if stopped_pid != snapshot.process.pid:
                self.state.user_stop_signature = None

        try:
            old = RunStatus(self.state.last_status)
        except ValueError:
            old = RunStatus.IDLE
        if snapshot.run_root is not None:
            self.state.selected_run = str(snapshot.run_root.resolve())
        if snapshot.stage is not None:
            self.state.last_stage = snapshot.stage.value
        self.state.last_log_mtime = snapshot.log_mtime
        self.state.last_status = snapshot.status.value

        message = None
        if snapshot.status != old:
            message = self._render_transition(old, snapshot)
            self.state.last_notification_state = snapshot.status.value
        self.store.save(self.state)
        return message

    def record_failed_notification(self, text: str) -> None:
        now = float(self.clock())
        pending = self.state.pending_notification
        if pending is None:
            self.state.pending_notification = {
                "text": text,
                "first_failed_at": now,
                "latest_failed_at": now,
                "replaced_count": 0,
            }
        else:
            self.state.pending_notification = {
                "text": text,
                "first_failed_at": float(pending.get("first_failed_at", now)),
                "latest_failed_at": now,
                "replaced_count": int(pending.get("replaced_count", 0)) + 1,
            }
        self.store.save(self.state)

    def deliver_pending_notification(self, send_func):
        pending = self.state.pending_notification
        if pending is None:
            return None
        replaced = int(pending.get("replaced_count", 0))
        summary = (
            "Telegram connection recovered.\n"
            f"Latest undelivered update:\n{pending.get('text', '')}\n"
            f"Superseded offline transitions: {replaced}"
        )
        send_func(summary)
        self.state.pending_notification = None
        self.store.save(self.state)
        return summary


def _format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unavailable"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def _format_snapshot(snapshot: MonitorSnapshot) -> str:
    lines = [f"Status: {snapshot.status.value}"]
    if snapshot.run_root is not None:
        lines.append(f"Run: {snapshot.run_root.name}")
    if snapshot.stage is not None:
        lines.append(f"Stage: {snapshot.stage.value}")
    p = snapshot.progress
    if p.current_ns is not None and p.target_ns is not None and p.percent is not None:
        lines.append(f"Progress: {p.current_ns:.3f} ns / {p.target_ns:.3f} ns ({p.percent:.2f}%)")
    elif p.current_ns is not None:
        lines.append(f"Progress: {p.current_ns:.3f} ns / target unavailable")
    else:
        lines.append("Progress: unavailable")
    if snapshot.process is not None:
        lines.append(f"PID: {snapshot.process.pid}")
        lines.append(f"Command: {snapshot.process.command_text}")
    if snapshot.log_age_seconds is not None:
        lines.append(f"Last log update: {_format_age(snapshot.log_age_seconds)} ago")
    else:
        lines.append("Last log update: unavailable")
    lines.append(f"Checkpoint: {'yes' if snapshot.checkpoint_exists else 'no'}")
    lines.append(f"Last monitor check: {int(snapshot.checked_at)}")
    if snapshot.diagnostic:
        lines.append(f"Diagnostic: {snapshot.diagnostic}")
    return "\n".join(lines)


class BotApplication:
    def __init__(self, config: Config, store: StateStore, *, engine: Optional[MonitorEngine] = None, clock=None, telegram: Optional[TelegramClient] = None, shutdown_event: Optional[threading.Event] = None):
        self.config = config
        self.store = store
        self.engine = engine or MonitorEngine(config, store)
        self.clock = clock or self.engine.clock
        self.telegram = telegram
        self.shutdown_event = shutdown_event or threading.Event()
        self.lock = threading.RLock()
        self.offset = None

    def _save_state(self) -> None:
        self.store.save(self.engine.state)

    def _handle_runs(self) -> str:
        candidates = discover_runs(self.config.base_dir)
        self.engine.state.last_runs = [str(item.path.resolve()) for item in candidates]
        self._save_state()
        if not candidates:
            return "No valid GROMACS runs found."
        lines = ["Available GROMACS runs:"]
        for index, item in enumerate(candidates, start=1):
            lines.append(f"{index}. {item.path.name} [{item.highest_stage.value}]")
        return "\n".join(lines)

    def _handle_use(self, args) -> str:
        if len(args) != 1:
            return "Invalid /use selection. Run /runs, then use /use <n>."
        try:
            index = int(args[0])
        except ValueError:
            return "Invalid /use selection. Run /runs, then use /use <n>."
        paths = self.engine.state.last_runs
        if index < 1 or index > len(paths):
            return "Invalid /use selection. Run /runs again."
        resolved = resolve_run_under_base(self.config.base_dir, Path(paths[index - 1]))
        if resolved is None:
            return "Selected run is no longer valid. Run /runs again."
        base = self.config.base_dir.resolve()
        try:
            relative = resolved.relative_to(base)
        except ValueError:
            return "Selected run is no longer valid. Run /runs again."
        stage = highest_logged_stage(resolved)
        if len(relative.parts) != 1 or stage is None:
            return "Selected run is no longer valid. Run /runs again."
        self.engine.state.selected_run = str(resolved)
        self.engine.state.last_stage = stage.value
        self._save_state()
        return f"Selected run: {resolved.name} [{stage.value}]"

    def _handle_where(self) -> str:
        path = self.engine.state.selected_run
        return path if path else "No run selected."

    def _selected_run_for_control(self) -> Optional[Path]:
        selected = self.engine.state.selected_run
        if not selected:
            return None
        resolved = resolve_run_under_base(self.config.base_dir, Path(selected))
        if resolved is None or not resolved.is_dir():
            return None
        base = self.config.base_dir.resolve()
        try:
            relative = resolved.relative_to(base)
        except ValueError:
            return None
        if len(relative.parts) != 1:
            return None
        return resolved

    def _remembered_stage(self) -> Optional[Stage]:
        raw = self.engine.state.last_stage
        if not raw:
            return None
        try:
            return Stage(raw)
        except ValueError:
            return None

    def _handle_continue(self) -> str:
        processes = find_mdrun_processes(self.config.base_dir)
        if processes:
            if len(processes) > 1:
                return "Cannot continue: multiple gmx mdrun processes are active."
            return "Cannot continue: a gmx mdrun process is already active."
        if not self.engine.state.selected_run:
            return "No run selected. Use /runs and /use <n> first."
        run_root = self._selected_run_for_control()
        if run_root is None:
            return "Selected run is invalid or outside the configured base directory."
        stage = choose_continuation_stage(run_root, self._remembered_stage())
        if stage is None:
            return "Cannot continue: no supported stage log is available."
        spec = STAGE_SPECS[stage]
        if not (run_root / spec.cpt_rel).is_file():
            return f"Cannot continue {stage.value}: checkpoint {spec.cpt_rel} is missing."
        pid = launch_continue(self.config, run_root, stage)
        self.engine.state.selected_run = str(run_root)
        self.engine.state.last_stage = stage.value
        self.engine.state.pending_stop = None
        self._save_state()
        return f"Continuation launched for {stage.value}. PID: {pid}"

    def _handle_stop(self) -> str:
        processes = find_mdrun_processes(self.config.base_dir)
        if len(processes) > 1:
            self.engine.state.pending_stop = None
            self._save_state()
            return "Cannot stop: multiple gmx mdrun processes are active."
        if not processes:
            self.engine.state.pending_stop = None
            self._save_state()
            return "No active gmx mdrun process found."
        process = processes[0]
        if process.stage is None:
            self.engine.state.pending_stop = None
            self._save_state()
            return "Cannot stop: active mdrun uses an unsupported -deffnm stage."
        pending = PendingStop(
            pid=process.pid,
            argv=list(process.argv),
            cwd=str(process.cwd.resolve()),
            run_root=str(process.run_root.resolve()),
            stage=process.stage.value,
            expires_at=float(self.clock()) + self.config.stop_confirm_seconds,
        )
        self.engine.state.pending_stop = asdict(pending)
        self._save_state()
        return (
            "Pending stop\n"
            f"PID: {process.pid}\n"
            f"Command: {process.command_text}\n"
            f"Run: {process.run_root}\n"
            f"Stage: {process.stage.value}\n"
            f"Confirm within {self.config.stop_confirm_seconds // 60} minutes with /confirmstop"
        )

    def _clear_pending_stop(self) -> None:
        self.engine.state.pending_stop = None
        self._save_state()

    def _handle_confirmstop(self) -> str:
        pending = self.engine.state.pending_stop
        if not isinstance(pending, dict):
            return "No pending stop confirmation. Use /stop first."
        now = float(self.clock())
        try:
            expires_at = float(pending["expires_at"])
            pid = int(pending["pid"])
        except (KeyError, TypeError, ValueError):
            self._clear_pending_stop()
            return "Pending stop confirmation is invalid; cancelled."
        if now > expires_at:
            self._clear_pending_stop()
            return "Pending stop confirmation expired; run /stop again."
        processes = find_mdrun_processes(self.config.base_dir)
        if len(processes) > 1:
            self._clear_pending_stop()
            return "Cannot confirm stop: multiple gmx mdrun processes are active."
        current = inspect_pid(pid, self.config.base_dir)
        if current is None:
            self._clear_pending_stop()
            return "Target PID is no longer a valid controllable gmx mdrun process; stop cancelled."
        expected_argv = tuple(str(item) for item in pending.get("argv", []))
        expected_cwd = str(Path(str(pending.get("cwd", ""))).resolve())
        expected_root = str(Path(str(pending.get("run_root", ""))).resolve())
        expected_stage = str(pending.get("stage", ""))
        identity_matches = (
            tuple(current.argv) == expected_argv
            and str(current.cwd.resolve()) == expected_cwd
            and str(current.run_root.resolve()) == expected_root
            and current.stage is not None
            and current.stage.value == expected_stage
        )
        if not identity_matches:
            self._clear_pending_stop()
            return "Target process identity changed; stop cancelled."
        force_kill_pid(pid)
        self.engine.state.pending_stop = None
        self.engine.state.user_stop_signature = {
            "run_root": str(current.run_root.resolve()),
            "stage": current.stage.value,
            "pid": pid,
            "time": now,
        }
        self._save_state()
        return f"GROMACS process {pid} killed with SIGKILL."

    def _help(self) -> str:
        return "\n".join([
            "/status — show current GROMACS state and progress",
            "/runs — list valid run directories",
            "/use <n> — select a run from the latest /runs list",
            "/where — show the selected run path",
            "/continue — continue the selected stage from checkpoint",
            "/stop — preview the active process to stop",
            "/confirmstop — confirm the pending stop within 5 minutes",
            "/help — show this command list; /stop always requires confirmation",
        ])

    def handle_update(self, update: TelegramUpdate) -> Optional[str]:
        if update.user_id != self.config.telegram_user_id:
            return "Unauthorized."
        text = update.text.strip()
        if not text:
            return None
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        if command == "/help":
            return self._help()
        if command == "/runs":
            return self._handle_runs()
        if command == "/use":
            return self._handle_use(args)
        if command == "/where":
            return self._handle_where()
        if command == "/status":
            return _format_snapshot(self.engine.inspect_once())
        if command == "/continue":
            return self._handle_continue()
        if command == "/stop":
            return self._handle_stop()
        if command == "/confirmstop":
            return self._handle_confirmstop()
        return "Unknown command. Use /help."

    def monitor_cycle(self) -> Optional[str]:
        with self.lock:
            snapshot = self.engine.inspect_once()
            transition = self.engine.commit_snapshot(snapshot)
            if self.telegram is None:
                return transition
            try:
                self.engine.deliver_pending_notification(
                    lambda text: self.telegram.send_message(self.config.telegram_user_id, text)
                )
            except TelegramError:
                pass
            if transition:
                try:
                    self.telegram.send_message(self.config.telegram_user_id, transition)
                except TelegramError:
                    self.engine.record_failed_notification(transition)
            return transition

    def poll_once(self) -> int:
        if self.telegram is None:
            raise RuntimeError("Telegram client is required")
        updates = self.telegram.get_updates(self.offset, timeout=25)
        processed = 0
        for update in updates:
            self.offset = update.update_id + 1 if self.offset is None else max(self.offset, update.update_id + 1)
            with self.lock:
                reply = self.handle_update(update)
            if reply:
                self.telegram.send_message(update.chat_id, reply)
            processed += 1
        if self.engine.state.pending_notification is not None:
            with self.lock:
                self.engine.deliver_pending_notification(
                    lambda text: self.telegram.send_message(self.config.telegram_user_id, text)
                )
        return processed

    def _monitor_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                self.monitor_cycle()
            except Exception:
                traceback.print_exc(file=sys.stderr)
            if self.shutdown_event.wait(self.config.poll_seconds):
                break

    def _telegram_loop(self) -> None:
        failures = 0
        while not self.shutdown_event.is_set():
            try:
                self.poll_once()
                failures = 0
            except TelegramError:
                failures += 1
                delay = min(60, 2 ** failures)
                self.shutdown_event.wait(delay)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self.shutdown_event.wait(1)

    def run_forever(self) -> None:
        if self.telegram is None:
            raise RuntimeError("Telegram client is required")
        monitor_thread = threading.Thread(target=self._monitor_loop, name="gromacs-monitor", daemon=True)
        monitor_thread.start()
        try:
            self._telegram_loop()
        finally:
            self.shutdown_event.set()
            monitor_thread.join(timeout=max(2, self.config.poll_seconds + 1))
