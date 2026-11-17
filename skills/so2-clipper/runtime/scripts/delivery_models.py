"""Static shapes for the curated delivery files; these declarations do not validate at runtime.

``plan.json`` stores content decisions. ``delivery.json`` is the resumable
execution journal. ``scan_results.json`` remains the scan index, and each run
receipt is an immutable snapshot of that run's outcome. ``round_id`` on a clip
is retained for file compatibility; candidates currently copy a ``view_id``
there and it is not a confirmed round until reviewed by the Agent.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict


ClipStatus = Literal['pending', 'unresolved', 'keep', 'reject', 'duplicate']
SourceReviewStatus = Literal['pending', 'reviewed']
OutputReviewStatus = Literal['pending', 'confirmed']
JournalItemStatus = Literal['encoding', 'generated', 'reused']
JournalStatus = Literal['delivering', 'partial', 'complete']


class SourceReview(TypedDict, total=False):
    status: SourceReviewStatus
    reason: str
    evidence: list[str]
    unresolved: list[str]


class FileIdentity(TypedDict):
    path: str
    size: int
    mtime_ns: int


class SourceIdentity(FileIdentity):
    """The same stable path/size/mtime identity, named for source recordings."""


class FileRecord(TypedDict):
    identity: FileIdentity
    sha256: str


class KillDecision(TypedDict, total=False):
    ts: float
    actor: str
    weapon: str
    evidence: list[str]


class ClipDecision(TypedDict, total=False):
    clip_id: str
    source_id: str
    # Kept as "round_id" on disk; candidate values presently come from view_id.
    round_id: str
    status: ClipStatus
    reason: str
    actor: str
    start: float
    end: float
    scan_times: list[float]
    scan_event_ids: list[str]
    kills: list[KillDecision]
    boundary_evidence: list[str]
    duplicate_of: str | None


class OutputReview(TypedDict, total=False):
    status: OutputReviewStatus
    reason: str
    identity: FileIdentity
    sha256: str
    evidence_manifest: str
    cards: list[str]


class DeliverySource(TypedDict, total=False):
    source_id: str
    path: str
    identity: SourceIdentity
    scan_valid: bool
    snapshot: str
    snapshot_sha256: str
    review: SourceReview
    review_queue: list[dict[str, Any]]
    media_info: dict[str, Any]


class DeliverySettings(TypedDict, total=False):
    min_kills: int
    actors: list[str]
    grouping: Literal['round']
    categories: dict[str, str]


class DeliveryPlan(TypedDict, total=False):
    version: Literal[1]
    kind: Literal['so2-curated-delivery']
    task_id: str
    input: str
    input_kind: Literal['file', 'directory']
    output_dir: str
    state_dir: str
    date_zone: str
    date: str
    settings: DeliverySettings
    sources: list[DeliverySource]
    clips: list[ClipDecision]
    evidence_manifests: list[str]
    output_reviews: dict[str, OutputReview]


class EvidenceFrame(TypedDict, total=False):
    path: str
    ts: float
    sha256: str


class EvidenceManifest(TypedDict, total=False):
    # Shape returned by evidence.bundle; the manifest path is also persisted in plans.
    manifest: str
    cards: list[str]
    frames: list[EvidenceFrame]
    source_identity: FileIdentity
    visual_review: Literal['pending']


PendingMove = TypedDict('PendingMove', {
    'from': str,
    'to': str,
    'record': FileRecord,
}, total=False)


class JournalItem(TypedDict, total=False):
    key: str
    path: str
    status: JournalItemStatus
    identity: FileIdentity
    sha256: str
    category: str
    full_decode: bool
    source_id: str
    start: float
    end: float
    kill_count: int
    actor: str
    pending_move: PendingMove
    previous: 'JournalItem'
    superseded: 'JournalItem'
    backup: str
    error: str


class DeliveryJournal(TypedDict, total=False):
    version: Literal[1]
    items: dict[str, JournalItem]
    retired: list[JournalItem]
    run_id: str
    status: JournalStatus
    plan: str
    snapshot: str
    receipt: str
    exit_code: int
    unresolved: list[dict[str, Any]]
    failures: list[dict[str, Any]]


class PreparedClip(TypedDict, total=False):
    clip: ClipDecision
    source: DeliverySource
    key: str
    category: str
    target: str


@dataclass
class DeliveryRun:
    """Fixed in-memory inputs and progress for one locked deliver() call."""
    plan_path: Path
    plan: DeliveryPlan
    cfg: Any
    prepared: list[PreparedClip]
    unresolved: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    folder: Path
    journal_path: Path
    journal: DeliveryJournal
    run_id: str
    run_folder: Path
    frozen: Path
    output_reviews: dict[str, OutputReview]
    current_ids: set[str]
