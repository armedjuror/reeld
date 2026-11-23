"""Suite data model (spec §8 and §9).

Every piece of text (voice profile, bible, skills, continuity, learnings,
research notes, scripts, scene plans, automation outputs) is a `Document`
with immutable `DocumentVersion` rows, scoped to a user, a series or an
episode. Media lives in object storage; `MediaAsset` keeps only the key.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, time, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    # Store enum values as plain strings: portable and readable in psql.
    return Enum(
        cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


# ─── Enums ────────────────────────────────────────────────────────────────────


class ScopeType(str, enum.Enum):
    USER = "user"
    SERIES = "series"
    EPISODE = "episode"


class DocKind(str, enum.Enum):
    VOICE = "voice"  # user-level narration/brand profile (key "main")
    BIBLE = "bible"  # series identity (key "main")
    SKILL = "skill"  # key: script | rubric | visuals | auto-<name>
    CONTINUITY = "continuity"  # series (key "main")
    LEARNINGS = "learnings"  # series (key "main")
    RESEARCH = "research"  # episode
    SCRIPT = "script"  # episode
    SCENE_PLAN = "scene_plan"  # episode
    AUTOMATION_OUTPUT = "automation_output"  # episode, key = automation name
    CRITIC = "critic"  # episode: latest critic verdict (JSON)
    ALIGNMENT = "alignment"  # episode: line timings of the clean audio (JSON)
    DIGEST = "digest"  # user: daily insights digest


class VersionSource(str, enum.Enum):
    USER = "user"
    MCP = "mcp"
    PIPELINE = "pipeline"
    ANALYSIS = "analysis"
    DISTIL = "distil"


class SeriesStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class SlotKind(str, enum.Enum):
    PRECISE = "precise"  # slot_date (+ optional slot_time)
    WEEK = "week"  # the week containing slot_date
    MONTH = "month"  # the month containing slot_date
    AFTER = "after"  # after episode `after_episode`
    UNSCHEDULED = "unscheduled"


class EpisodeState(str, enum.Enum):
    PLANNED = "planned"
    SCRIPTING = "scripting"
    SCRIPT_REVIEW = "script_review"
    AWAITING_RECORDING = "awaiting_recording"
    AUDIO_PROCESSING = "audio_processing"
    AUDIO_REVIEW = "audio_review"
    EDITING = "editing"
    VIDEO_REVIEW = "video_review"
    AUTOMATIONS = "automations"
    PUBLISHING = "publishing"
    LIVE = "live"
    FAILED = "failed"


class RunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"  # e.g. Claude Code usage limit; see resume_at
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TaskType(str, enum.Enum):
    APPROVE_SCRIPT = "approve_script"
    RECORD = "record"
    CHECK_AUDIO = "check_audio"
    APPROVE_VIDEO = "approve_video"
    REVIEW_PROPOSAL = "review_proposal"
    READ_INSIGHTS = "read_insights"
    FIX_FAILURE = "fix_failure"
    PUBLISH = "publish"  # manual publishing when SuperProfile isn't set up


class TaskStatus(str, enum.Enum):
    OPEN = "open"
    DONE = "done"
    DISMISSED = "dismissed"


class ProposalStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class MediaKind(str, enum.Enum):
    RAW_TAKE = "raw_take"
    CLEAN_AUDIO = "clean_audio"
    VIDEO = "video"
    BGM = "bgm"
    SFX = "sfx"
    IMAGE = "image"


# ─── Users (legacy table, shared with main.py) ────────────────────────────────


class User(Base):
    """Maps the legacy `users` table created by db.init_db()."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(Text)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text)
    type: Mapped[Optional[str]] = mapped_column(Text, default="free")
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


# ─── Documents ────────────────────────────────────────────────────────────────


class Document(Base):
    __tablename__ = "suite_documents"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "kind", "key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scope_type: Mapped[ScopeType] = mapped_column(_enum(ScopeType, "scope_type"))
    scope_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[DocKind] = mapped_column(_enum(DocKind, "doc_kind"))
    key: Mapped[str] = mapped_column(String(64), default="main")
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentVersion.version",
    )


class DocumentVersion(Base):
    __tablename__ = "suite_document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("suite_documents.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[VersionSource] = mapped_column(_enum(VersionSource, "version_source"))
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="versions")


# ─── Series and episodes ──────────────────────────────────────────────────────


class Series(Base):
    __tablename__ = "suite_series"
    __table_args__ = (UniqueConstraint("user_id", "slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[SeriesStatus] = mapped_column(
        _enum(SeriesStatus, "series_status"), default=SeriesStatus.DRAFT
    )
    # [{"name": "blog", "order": 1, "enabled": true, "config": {...}}, ...]
    automations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    episodes: Mapped[list["Episode"]] = relationship(
        back_populates="series",
        cascade="all, delete-orphan",
        order_by="Episode.number",
    )


class Episode(Base):
    __tablename__ = "suite_episodes"
    __table_args__ = (
        UniqueConstraint("series_id", "number"),
        UniqueConstraint("series_id", "keyword"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(
        ForeignKey("suite_series.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    core_idea: Mapped[str] = mapped_column(Text, default="")
    hook: Mapped[str] = mapped_column(Text, default="")
    keyword: Mapped[str] = mapped_column(String(64))
    slot_kind: Mapped[SlotKind] = mapped_column(
        _enum(SlotKind, "slot_kind"), default=SlotKind.UNSCHEDULED
    )
    slot_date: Mapped[Optional[date]] = mapped_column(Date)
    slot_time: Mapped[Optional[time]] = mapped_column(Time)
    after_episode: Mapped[Optional[int]] = mapped_column(Integer)
    # Section-level overrides of the merged profile, e.g. {"Length": "90 s"}.
    overrides: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    state: Mapped[EpisodeState] = mapped_column(
        _enum(EpisodeState, "episode_state"), default=EpisodeState.PLANNED
    )
    # Publishing/insight bookkeeping: superprofile ids, instagram media id,
    # published_at, approvals count, last error, etc.
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    series: Mapped[Series] = relationship(back_populates="episodes")


# ─── Pipeline ─────────────────────────────────────────────────────────────────


class StageRun(Base):
    """One execution of one pipeline step; the unit of checkpoint and resume."""

    __tablename__ = "suite_stage_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("suite_episodes.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(64))  # e.g. "script"
    step: Mapped[str] = mapped_column(String(64))  # e.g. "draft", "critic-2"
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), default=RunStatus.QUEUED
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    claude_session_id: Mapped[Optional[str]] = mapped_column(String(128))
    resume_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # What the context resolver sent: parts, versions, token estimate.
    context_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("suite_document_versions.id", ondelete="SET NULL")
    )
    output_text: Mapped[Optional[str]] = mapped_column(Text)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float)
    error: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Task(Base):
    """Something that needs the user (spec §7: the home screen is this list)."""

    __tablename__ = "suite_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    series_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("suite_series.id", ondelete="CASCADE")
    )
    episode_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("suite_episodes.id", ondelete="CASCADE")
    )
    type: Mapped[TaskType] = mapped_column(_enum(TaskType, "task_type"))
    status: Mapped[TaskStatus] = mapped_column(
        _enum(TaskStatus, "task_status"), default=TaskStatus.OPEN, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower = sooner
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    estimate_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    stage_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("suite_stage_runs.id", ondelete="SET NULL")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Proposal(Base):
    """A suggested change to a series document; never applied without approval."""

    __tablename__ = "suite_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    series_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("suite_series.id", ondelete="CASCADE")
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("suite_documents.id", ondelete="CASCADE")
    )
    base_version: Mapped[int] = mapped_column(Integer)
    proposed_content: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[VersionSource] = mapped_column(_enum(VersionSource, "proposal_source"))
    status: Mapped[ProposalStatus] = mapped_column(
        _enum(ProposalStatus, "proposal_status"), default=ProposalStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class InsightSnapshot(Base):
    __tablename__ = "suite_insight_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("suite_episodes.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(32), default="instagram")
    external_id: Mapped[Optional[str]] = mapped_column(String(128))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MediaAsset(Base):
    """Pointer to a file in object storage (local disk now, S3 later)."""

    __tablename__ = "suite_media_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    episode_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("suite_episodes.id", ondelete="CASCADE")
    )
    kind: Mapped[MediaKind] = mapped_column(_enum(MediaKind, "media_kind"))
    storage_key: Mapped[str] = mapped_column(Text)
    mime: Mapped[Optional[str]] = mapped_column(String(100))
    duration_sec: Mapped[Optional[float]] = mapped_column(Float)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Integration(Base):
    """Per-user credentials and settings for an outside service (meta, superprofile, git)."""

    __tablename__ = "suite_integrations"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
