"""Typed contract for the owner action backlog (USR-190).

An *owner action* is anything only the human owner can do (a protected-file commit, a secret,
a portal step, a policy decision). The backlog lives in ``.factory/owner_actions/owner_actions.json``
and is shown by the DarkHub priority-interventions queue.

Two kinds exist:

* ``action``   - the owner performs numbered steps and the agent marks it ``done``;
* ``decision`` - the owner picks one of ``options`` (plus a note); the answer is attached to the
  blocked tickets so the production line can resume by itself.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

OWNER_ACTION_ID_PATTERN = re.compile(r"^OA-[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SEQUENTIAL_ID_PATTERN = re.compile(r"^OA-(\d+)$")
TICKET_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9._-]+$")

# Token shapes that must never be stored in (or printed from) the backlog. Placeholders such as
# ``<TOKEN_NOVO>`` or ``USUARIO:SENHA`` are fine: only credential-shaped literals are rejected.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("telegram-bot-token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{24,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("url-password", re.compile(r"://[^/\s:@<>\"']+:(?![A-Z]{3,10}@)(?!<)[^/\s@<>\"']{6,}@")),
)
REDACTED = "[REDACTED]"


def find_secrets(text: str) -> list[str]:
    """Names of credential-shaped patterns found in ``text`` (never the values)."""

    return [name for name, pattern in _SECRET_PATTERNS if pattern.search(text or "")]


def redact(text: str) -> str:
    """Replace credential-shaped literals by a marker; safe to print."""

    out = text or ""
    for _name, pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_z(value: datetime) -> str:
    """Serialise like the seeded file: ``2026-10-09T22:00:00Z``."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ActionKind(str, Enum):
    ACTION = "action"
    DECISION = "decision"


class ActionPriority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ActionStatus(str, Enum):
    OPEN = "open"
    WAITING = "waiting"
    DONE = "done"


PRIORITY_RANK: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
NOTIFY_PRIORITIES = frozenset({ActionPriority.CRITICAL, ActionPriority.HIGH})


class ActionStep(BaseModel):
    """One numbered screen-by-screen step; ``command`` is shown with a copy button."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1)
    command: str | None = None

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("step text must not be blank")
        return value

    @field_validator("command")
    @classmethod
    def _blank_command_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class DecisionOption(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1)
    detail: str = ""


class DecisionAnswer(BaseModel):
    """The owner's answer to a ``decision`` item."""

    model_config = ConfigDict(extra="ignore")

    option_id: str = Field(min_length=1)
    note: str = ""
    answered_at: datetime = Field(default_factory=utc_now)
    answered_by: str = "owner"

    @field_serializer("answered_at")
    def _ser_answered_at(self, value: datetime) -> str:
        return iso_z(value)


class OwnerAction(BaseModel):
    """A single backlog entry. Unknown keys are ignored so older readers survive newer files."""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: ActionKind = ActionKind.ACTION
    title: str = Field(min_length=3)
    priority: ActionPriority = ActionPriority.MEDIUM
    status: ActionStatus = ActionStatus.OPEN
    why: str = ""
    blocks: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    steps: list[ActionStep] = Field(default_factory=list)
    options: list[DecisionOption] = Field(default_factory=list)
    verify: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str = "claude-code"
    resolved_at: datetime | None = None
    answer: DecisionAnswer | None = None

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not OWNER_ACTION_ID_PATTERN.match(value):
            raise ValueError(f"invalid owner action id {value!r}; expected OA-NNN")
        return value

    @field_validator("blocks", "depends_on")
    @classmethod
    def _clean_refs(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in values:
            ref = str(raw).strip()
            if not ref:
                continue
            if not (TICKET_ID_PATTERN.match(ref) or OWNER_ACTION_ID_PATTERN.match(ref)):
                raise ValueError(f"invalid reference {ref!r}")
            if ref not in cleaned:
                cleaned.append(ref)
        return cleaned

    @model_validator(mode="after")
    def _consistent(self) -> "OwnerAction":
        if self.id in self.depends_on:
            raise ValueError("an owner action cannot depend on itself")
        if self.kind is ActionKind.DECISION:
            if len(self.options) < 2:
                raise ValueError("a decision needs at least two options")
            ids = [option.id for option in self.options]
            if len(set(ids)) != len(ids):
                raise ValueError("decision option ids must be unique")
            if self.answer is not None and self.answer.option_id not in ids:
                raise ValueError("answer.option_id is not one of the options")
        else:
            if self.options:
                raise ValueError("only decisions carry options")
            if not self.steps:
                raise ValueError("an action needs at least one step")
        if self.status is ActionStatus.DONE and self.resolved_at is None:
            self.resolved_at = utc_now()
        if self.status is not ActionStatus.DONE and self.resolved_at is not None:
            raise ValueError("resolved_at is only valid for done items")
        leaked = find_secrets(self.model_dump_json())
        if leaked:
            raise ValueError(f"credential-shaped content is not allowed ({', '.join(leaked)}); use a placeholder")
        return self

    @field_serializer("created_at")
    def _ser_created(self, value: datetime) -> str:
        return iso_z(value)

    @field_serializer("resolved_at")
    def _ser_resolved(self, value: datetime | None) -> str | None:
        return iso_z(value) if value else None

    def to_record(self) -> dict[str, Any]:
        """JSON-ready dict in the file's canonical key order (answer/options only when set)."""

        record = self.model_dump(mode="json")
        if not self.options:
            record.pop("options", None)
        if self.answer is None:
            record.pop("answer", None)
        for step in record["steps"]:
            if step.get("command") is None:
                step.pop("command", None)
        for option in record.get("options", []):
            if not option.get("detail"):
                option.pop("detail", None)
        return record


class OwnerActionDraft(BaseModel):
    """Input for ``add``: everything the author provides; id and timestamps are assigned."""

    model_config = ConfigDict(extra="forbid")

    kind: ActionKind = ActionKind.ACTION
    title: str
    priority: ActionPriority = ActionPriority.MEDIUM
    why: str = ""
    blocks: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    steps: list[ActionStep] = Field(default_factory=list)
    options: list[DecisionOption] = Field(default_factory=list)
    verify: str = ""
    created_by: str = "claude-code"
    id: str | None = None

    def build(self, action_id: str) -> OwnerAction:
        return OwnerAction(
            id=action_id,
            kind=self.kind,
            title=self.title.strip(),
            priority=self.priority,
            status=ActionStatus.OPEN,
            why=self.why.strip(),
            blocks=self.blocks,
            depends_on=self.depends_on,
            steps=self.steps,
            options=self.options,
            verify=self.verify.strip(),
            created_by=self.created_by,
        )


class Resolution(BaseModel):
    """Overlay entry persisted by the Hub (production volume) for an item defined in git."""

    model_config = ConfigDict(extra="ignore")

    status: ActionStatus = ActionStatus.DONE
    resolved_at: datetime = Field(default_factory=utc_now)
    answer: DecisionAnswer | None = None
    note: str = ""

    @field_serializer("resolved_at")
    def _ser_resolved(self, value: datetime) -> str:
        return iso_z(value)


__all__ = [
    "ActionKind",
    "ActionPriority",
    "ActionStatus",
    "ActionStep",
    "DecisionAnswer",
    "DecisionOption",
    "NOTIFY_PRIORITIES",
    "OWNER_ACTION_ID_PATTERN",
    "OwnerAction",
    "OwnerActionDraft",
    "PRIORITY_RANK",
    "Resolution",
    "SEQUENTIAL_ID_PATTERN",
    "find_secrets",
    "iso_z",
    "redact",
    "utc_now",
]
