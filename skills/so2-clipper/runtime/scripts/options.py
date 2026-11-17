"""Explicit configuration and per-run/per-source contexts for the SO2 pipeline."""
from dataclasses import dataclass, field, fields, replace
from types import SimpleNamespace
from typing import Any


@dataclass
class Options:
    """Normalized user/CLI options returned by ``config.load_config``.

    Field names intentionally match the former config namespace so existing
    helpers can keep using ``option.name`` and ``vars(option)``.
    """

    video_dir: str | None
    output_dir: str
    state_dir: str
    video: str | None
    hits: str | None
    keywords: list[str]
    identity_mode: str
    date_zone: str
    run_id: str | None
    merge: bool
    decisions: str | None
    source_registry: str | None
    profile: str
    languages: list[str]
    pre: float
    post: float
    kill_gap: float
    min_kills: int
    interval: int
    scan_segments: int
    scan_engine: str
    roi_fps: float
    workers: int
    extract_workers: int
    mode: str
    min_conf: float
    min_change_ratio: float
    region: dict[str, int]
    max_x_ratio: float | None
    cluster_window: float
    sig_window: float
    suppress_after_hit: float
    no_gpu: bool
    rescan: bool
    scan_only: bool
    extract_only: bool
    defer_report_images: bool
    keep_warmup: bool
    registered_sources: list[dict[str, Any]]
    gpu: bool
    allowlist: str | None
    filter_warmup: bool
    scan_interval: int
    region_override: bool
    max_x_override: float | None
    base_w: int
    base_h: int
    max_x_center_ratio: float
    result_file: str
    log_file: str
    tmp_dir: str

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "Options":
        """Build options from the normalized mapping assembled by config."""
        allowed = {field.name for field in fields(cls)}
        unexpected = values.keys() - allowed
        if unexpected:
            raise TypeError(f"unexpected Options fields: {', '.join(sorted(unexpected))}")
        missing = allowed - values.keys()
        if missing:
            raise TypeError(f"missing Options fields: {', '.join(sorted(missing))}")
        return cls(**values)


@dataclass
class RunContext(Options):
    """Options plus state owned by one invocation of the pipeline."""

    current_run_id: str | None = None
    run_date: str | None = None
    merge_result: dict[str, Any] | None = field(default_factory=dict)

    @classmethod
    def from_options(cls, options: Options, **runtime: Any) -> "RunContext":
        """Copy normalized options and attach only explicitly supplied run state."""
        values = _option_values(options)
        values.update(current_run_id=None, run_date=None, merge_result={})
        values.update(runtime)
        return cls(**values)


@dataclass
class ResolvedSource(RunContext):
    """Per-source media and layout decisions, with the owning run context."""

    profile: str
    path: str = ""
    media_info: dict[str, Any] | None = None
    effective_engine: str | None = None
    layout_evidence: dict[str, Any] | None = None
    feed_rect: tuple[int, int, int, int] | None = None
    phase_rect: tuple[int, int, int, int] | None = None
    identity_rects: dict[str, tuple[int, int, int, int]] | None = None

    @classmethod
    def from_options(
        cls,
        options: Options | RunContext,
        path: str,
        media_info: dict[str, Any],
        profile: str,
        effective_engine: str,
        layout_evidence: dict[str, Any] | None = None,
        **resolved_values: Any,
    ) -> "ResolvedSource":
        """Create a source while retaining a supplied RunContext unchanged."""
        context = (options if isinstance(options, RunContext)
                   else RunContext.from_options(options))
        values = {field.name: getattr(context, field.name) for field in fields(RunContext)}
        values.update(profile=profile, path=path, media_info=media_info,
                      effective_engine=effective_engine,
                      layout_evidence=layout_evidence or {})
        values.update(resolved_values)
        return cls(**values)


def _option_values(options: Options) -> dict[str, Any]:
    if isinstance(options, Options):
        return {field.name: getattr(options, field.name) for field in fields(Options)}
    # Keep test doubles such as SimpleNamespace usable without forwarding attrs.
    values = vars(options).copy()
    allowed = {field.name for field in fields(Options)}
    return {name: value for name, value in values.items() if name in allowed}


def copy_options(options: Any, **changes: Any) -> Any:
    """Make an explicit dataclass copy, retaining SimpleNamespace test fixtures."""
    if isinstance(options, (Options, RunContext, ResolvedSource)):
        return replace(options, **changes)
    values = vars(options).copy()
    values.update(changes)
    return SimpleNamespace(**values)
