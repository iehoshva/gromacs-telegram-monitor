from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple


class Stage(Enum):
    NVT = "NVT"
    NPT = "NPT"
    PROD = "PROD"


class RunStatus(Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STALLED = "STALLED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    MULTIPLE_PROCESS = "MULTIPLE_PROCESS"


@dataclass(frozen=True)
class StageSpec:
    stage: Stage
    prefix: Path
    log_rel: Path
    cpt_rel: Path
    tpr_rel: Path


STAGE_SPECS = {
    Stage.NVT: StageSpec(Stage.NVT, Path("02_nvt/nvt"), Path("02_nvt/nvt.log"), Path("02_nvt/nvt.cpt"), Path("02_nvt/nvt.tpr")),
    Stage.NPT: StageSpec(Stage.NPT, Path("03_npt/npt"), Path("03_npt/npt.log"), Path("03_npt/npt.cpt"), Path("03_npt/npt.tpr")),
    Stage.PROD: StageSpec(Stage.PROD, Path("04_prod/prod"), Path("04_prod/prod.log"), Path("04_prod/prod.cpt"), Path("04_prod/prod.tpr")),
}
STAGE_ORDER = (Stage.NVT, Stage.NPT, Stage.PROD)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    argv: Tuple[str, ...]
    command_text: str
    cwd: Path
    run_root: Path
    stage: Optional[Stage]
    deffnm: Optional[str]


@dataclass(frozen=True)
class Progress:
    current_ns: Optional[float]
    target_ns: Optional[float]
    percent: Optional[float]


@dataclass(frozen=True)
class MonitorSnapshot:
    status: RunStatus
    checked_at: float
    run_root: Optional[Path] = None
    stage: Optional[Stage] = None
    process: Optional[ProcessInfo] = None
    log_mtime: Optional[float] = None
    log_age_seconds: Optional[float] = None
    checkpoint_exists: bool = False
    progress: Progress = Progress(None, None, None)
    diagnostic: str = ""


@dataclass
class PendingStop:
    pid: int
    argv: List[str]
    cwd: str
    run_root: str
    stage: str
    expires_at: float


@dataclass
class PersistentState:
    selected_run: Optional[str] = None
    last_stage: Optional[str] = None
    last_status: str = RunStatus.IDLE.value
    last_log_mtime: Optional[float] = None
    last_notification_state: Optional[str] = None
    pending_stop: Optional[dict] = None
    last_runs: List[str] = field(default_factory=list)
    pending_notification: Optional[dict] = None
    user_stop_signature: Optional[dict] = None
