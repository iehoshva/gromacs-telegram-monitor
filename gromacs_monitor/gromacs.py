from dataclasses import dataclass
import math
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .model import ProcessInfo, Progress, STAGE_ORDER, STAGE_SPECS, Stage


@dataclass(frozen=True)
class RunCandidate:
    path: Path
    highest_stage: Stage


def resolve_run_under_base(base_dir: Path, path: Path) -> Optional[Path]:
    try:
        base = Path(base_dir).resolve()
        resolved = Path(path).resolve()
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved == base:
        return None
    return resolved


def highest_logged_stage(run_root: Path) -> Optional[Stage]:
    highest = None
    for stage in STAGE_ORDER:
        if (Path(run_root) / STAGE_SPECS[stage].log_rel).is_file():
            highest = stage
    return highest


def discover_runs(base_dir: Path) -> List[RunCandidate]:
    base = Path(base_dir).resolve()
    try:
        children = list(base.iterdir())
    except OSError:
        return []

    candidates: List[RunCandidate] = []
    for child in children:
        resolved = resolve_run_under_base(base, child)
        if resolved is None or not resolved.is_dir():
            continue
        try:
            relative = resolved.relative_to(base)
        except ValueError:
            continue
        if len(relative.parts) != 1:
            continue
        stage = highest_logged_stage(resolved)
        if stage is not None:
            candidates.append(RunCandidate(resolved, stage))
    candidates.sort(key=lambda item: item.path.name.casefold())
    return candidates


_DEFFNM_TO_STAGE = {spec.prefix.as_posix(): stage for stage, spec in STAGE_SPECS.items()}


def parse_stage_from_argv(argv: Sequence[str]) -> Tuple[Optional[Stage], Optional[str]]:
    deffnm = None
    for index, value in enumerate(argv):
        if value == "-deffnm" and index + 1 < len(argv):
            deffnm = argv[index + 1]
            break
        if value.startswith("-deffnm="):
            deffnm = value.split("=", 1)[1]
            break
    if deffnm is None:
        return None, None
    normalized = Path(deffnm).as_posix()
    return _DEFFNM_TO_STAGE.get(normalized), normalized


def _is_gmx_mdrun(argv: Sequence[str]) -> bool:
    return len(argv) >= 2 and Path(argv[0]).name == "gmx" and argv[1] == "mdrun"


def inspect_pid(pid: int, base_dir: Path, *, proc_root: Path = Path("/proc")) -> Optional[ProcessInfo]:
    proc_dir = Path(proc_root) / str(pid)
    try:
        raw = (proc_dir / "cmdline").read_bytes()
        argv = tuple(part.decode(errors="replace") for part in raw.split(b"\0") if part)
        cwd = Path(os.readlink(proc_dir / "cwd")).resolve()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not _is_gmx_mdrun(argv):
        return None
    base = Path(base_dir).resolve()
    try:
        relative = cwd.relative_to(base)
    except ValueError:
        return None
    if not relative.parts:
        return None
    run_root = (base / relative.parts[0]).resolve()
    try:
        run_root.relative_to(base)
    except ValueError:
        return None
    stage, deffnm = parse_stage_from_argv(argv)
    return ProcessInfo(
        pid=pid,
        argv=argv,
        command_text=" ".join(argv),
        cwd=cwd,
        run_root=run_root,
        stage=stage,
        deffnm=deffnm,
    )


def find_mdrun_processes(base_dir: Path, *, proc_root: Path = Path("/proc")) -> List[ProcessInfo]:
    try:
        result = subprocess.run(
            ["pgrep", "-af", "gmx mdrun"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return []
    found: List[ProcessInfo] = []
    for line in result.stdout.splitlines():
        first = line.strip().split(maxsplit=1)
        if not first:
            continue
        try:
            pid = int(first[0])
        except ValueError:
            continue
        info = inspect_pid(pid, base_dir, proc_root=proc_root)
        if info is not None:
            found.append(info)
    return found


_LOG_TAIL_BYTES = 512 * 1024


def _read_log_tail(path: Path) -> str:
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_TAIL_BYTES))
            data = handle.read(_LOG_TAIL_BYTES)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def log_has_finished(log_path: Path) -> bool:
    return "Finished mdrun" in _read_log_tail(log_path)


def read_current_time_ps(log_path: Path) -> Optional[float]:
    lines = _read_log_tail(log_path).splitlines()
    latest = None
    expect_value = False
    for line in lines:
        tokens = line.split()
        if len(tokens) >= 2 and tokens[0] == "Step" and tokens[1] == "Time":
            expect_value = True
            continue
        if expect_value:
            if not tokens:
                continue
            expect_value = False
            if len(tokens) < 2:
                continue
            try:
                int(tokens[0])
                value = float(tokens[1])
            except (ValueError, OverflowError):
                continue
            if math.isfinite(value):
                latest = value
    return latest


def read_target_time_ps(gmx_path: Path, tpr_path: Path, cwd: Path) -> Optional[float]:
    tpr = Path(tpr_path)
    if not tpr.is_file():
        return None
    try:
        result = subprocess.run(
            [str(gmx_path), "dump", "-s", str(tpr)],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    values = {}
    pattern = re.compile(r"^\s*([A-Za-z][A-Za-z-]*)\s*=\s*([^\s]+)")
    for line in result.stdout.splitlines():
        match = pattern.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    try:
        init_step = float(values["init-step"])
        nsteps = float(values["nsteps"])
        tinit_raw = values.get("init-t", values.get("tinit"))
        dt_raw = values.get("delta-t", values.get("dt"))
        if tinit_raw is None or dt_raw is None:
            return None
        tinit = float(tinit_raw)
        dt = float(dt_raw)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(v) for v in (init_step, nsteps, tinit, dt)):
        return None
    if nsteps < 0 or dt <= 0:
        return None
    target = tinit + dt * (init_step + nsteps)
    return target if math.isfinite(target) else None


def read_progress(gmx_path: Path, run_root: Path, stage: Stage) -> Progress:
    spec = STAGE_SPECS[stage]
    current_ps = read_current_time_ps(Path(run_root) / spec.log_rel)
    target_ps = read_target_time_ps(gmx_path, Path(run_root) / spec.tpr_rel, Path(run_root))
    current_ns = current_ps / 1000.0 if current_ps is not None else None
    target_ns = target_ps / 1000.0 if target_ps is not None else None
    percent = None
    if current_ns is not None and target_ns is not None and target_ns > 0:
        percent = max(0.0, min(100.0, 100.0 * current_ns / target_ns))
    return Progress(current_ns, target_ns, percent)


def choose_continuation_stage(run_root: Path, remembered: Optional[Stage]) -> Optional[Stage]:
    root = Path(run_root)
    if remembered is not None and (root / STAGE_SPECS[remembered].log_rel).is_file():
        return remembered
    return highest_logged_stage(root)


def launch_continue(config, run_root: Path, stage: Stage) -> int:
    root = Path(run_root).resolve()
    spec = STAGE_SPECS[stage]
    prefix = spec.prefix.as_posix()
    argv = [
        str(config.gmx_path),
        "mdrun",
        "-deffnm",
        prefix,
        "-cpi",
        prefix + ".cpt",
        "-append",
    ]
    output_path = root / "gromacs-telegram-continue.out"
    with output_path.open("ab") as out_handle:
        process = subprocess.Popen(
            argv,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=out_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return int(process.pid)


def force_kill_pid(pid: int) -> None:
    subprocess.run(
        ["kill", "-KILL", str(int(pid))],
        text=True,
        capture_output=True,
        check=True,
    )
