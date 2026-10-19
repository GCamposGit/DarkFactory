"""Automatic catalog registration from ticket deliveries (USR-220).

When a ticket is delivered, the files it created or changed are classified into
reusable components (``/api/catalog/components``) and skill/rule evolutions
(``/api/evolution/status``) and written to the SAME stores those endpoints read:
``<state_root>/catalog/components.json`` (:class:`CrossProjectCatalogManager`) and
``<state_root>/evolution/proposals.json`` (:class:`FactoryEvolutionEngine`). Both are
git-ignored runtime state, so nothing here ever adds a file to a delivery PR.

Heuristic (documented contract)
-------------------------------
Reusable component, one per file, id ``<root>-<path slug>``:
  * NEW ``core/**/*.py`` (not ``__init__``/tests) exposing a public top-level
    function or class -> ``schema_contract`` if it defines a pydantic ``BaseModel``,
    otherwise ``integration_hook``.
  * NEW or CHANGED ``scripts/*`` (``.py``/``.ps1``/``.sh``), public API required for ``.py``
    -> ``script_utility``.
  * NEW skill (``SKILL.md`` created under ``.agents/skills`` or ``skills``) -> one component
    per skill directory (``integration_hook``, tag ``skill``).
  * Explicit ticket tag ``catalog:<name>``: a single component ``<name>`` grouping every
    changed non-test, non-ledger file of the ticket; it replaces the heuristic components.
Evolution (recorded as ``promoted`` proposals with ``metadata.origin == "delivery"``):
  * CHANGED files under ``.agents/skills`` / ``skills`` -> ``skill_instruction``.
  * ``AGENTS.md``, ``CLAUDE.md``, ``FACTORY_RULES.md``, ``MISSION.md`` -> ``context_rule``.
Every entry links the ticket and the PR. Ids are stable, so re-running is idempotent.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from core.catalog.models import ComponentDescriptor, ComponentFile, ComponentKind

logger = logging.getLogger(__name__)

TAG_PREFIX = "catalog:"
ORIGIN = "delivery"
MAX_FILE_BYTES = 200_000
SKILL_ROOTS = (".agents/skills/", "skills/")
RULE_FILES = ("AGENTS.md", "CLAUDE.md", "FACTORY_RULES.md", "MISSION.md")
SCRIPT_SUFFIXES = (".py", ".ps1", ".sh")

# (status letter, normalized path); status is git's name-status letter (A/M/...).
Change = tuple[str, str]
ReadFile = Callable[[str], Optional[str]]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "item"


def _short(path: str) -> str:
    for prefix in ("core/", "scripts/", *SKILL_ROOTS):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    return path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path


def _is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") or "/tests/" in path or name.startswith("test_") or name == "conftest.py"


def _public_api(source: str) -> tuple[bool, bool]:
    """Return (has public top-level def/class, defines a pydantic BaseModel)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False, False
    public = False
    model = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            public = True
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    if name == "BaseModel":
                        model = True
    return public, model


def explicit_catalog_names(tags: Iterable[str]) -> list[str]:
    names: list[str] = []
    for tag in tags or []:
        tag = str(tag).strip()
        if tag.lower().startswith(TAG_PREFIX) and tag[len(TAG_PREFIX):].strip():
            names.append(tag[len(TAG_PREFIX):].strip())
    return names


def _component_files(paths: Sequence[str], read_file: ReadFile) -> list[ComponentFile]:
    files: list[ComponentFile] = []
    for path in paths:
        content = read_file(path)
        if content is None or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            continue
        files.append(ComponentFile(path=path, content=content, sha256=hashlib.sha256(content.encode("utf-8")).hexdigest()))
    return files


def classify_changes(
    ticket_id: str,
    title: str,
    changes: Sequence[Change],
    read_file: ReadFile,
    tags: Iterable[str] = (),
    pr_url: str = "",
    delivered_at: Optional[datetime] = None,
) -> tuple[list[ComponentDescriptor], list[dict]]:
    """Pure classification: returns (component descriptors, evolution records)."""
    when = (delivered_at or datetime.now(timezone.utc)).isoformat()
    meta = {"origin": ORIGIN, "ticket_id": ticket_id, "pr_url": pr_url, "delivered_at": when}
    base_tags = ["auto-registered", ticket_id.lower()]
    norm = [(s[:1].upper(), p.replace("\\", "/").lstrip("/")) for s, p in changes if p]
    candidates = [(s, p) for s, p in norm if s != "D" and not _is_test(p) and p != ".factory/demands/demands.json"]

    components: list[ComponentDescriptor] = []
    names = explicit_catalog_names(tags)
    if names:
        paths = [p for _, p in candidates if not p.startswith(".factory/")]
        for name in names:
            files = _component_files(paths, read_file)
            if not files:
                continue
            components.append(ComponentDescriptor(
                id=_slug(name), name=name, kind=ComponentKind.INTEGRATION_HOOK, source_project_id="darkfac",
                description=f"{title.strip() or name} ({ticket_id})", files=files,
                tags=base_tags + ["explicit"], metadata=dict(meta),
            ))
    else:
        seen_skill_dirs: set[str] = set()
        for status, path in candidates:
            kind: Optional[ComponentKind] = None
            root = ""
            extra: list[str] = []
            paths = [path]
            comp_name = path
            if path.startswith("core/") and path.endswith(".py") and status == "A" \
                    and not path.endswith("__init__.py"):
                src = read_file(path)
                public, model = _public_api(src or "")
                if public:
                    kind = ComponentKind.SCHEMA_CONTRACT if model else ComponentKind.INTEGRATION_HOOK
                    root = "core"
            elif path.startswith("scripts/") and path.endswith(SCRIPT_SUFFIXES) and status in ("A", "M") \
                    and "/" not in path[len("scripts/"):]:
                if path.endswith(".py"):
                    public, _ = _public_api(read_file(path) or "")
                    ok = public
                else:
                    ok = True
                if ok:
                    kind, root = ComponentKind.SCRIPT_UTILITY, "script"
            elif status == "A" and path.endswith("/SKILL.md"):
                for sroot in SKILL_ROOTS:
                    if path.startswith(sroot):
                        skill_dir = path.rsplit("/", 1)[0]
                        if skill_dir in seen_skill_dirs:
                            break
                        seen_skill_dirs.add(skill_dir)
                        paths = [p for _, p in candidates if p.startswith(skill_dir + "/")]
                        kind, root, extra = ComponentKind.INTEGRATION_HOOK, "skill", ["skill"]
                        comp_name = skill_dir
                        break
            if kind is None:
                continue
            files = _component_files(paths, read_file)
            if not files:
                continue
            components.append(ComponentDescriptor(
                id=f"{root}-{_slug(_short(comp_name))}", name=comp_name, kind=kind, source_project_id="darkfac",
                description=f"{title.strip() or comp_name} ({ticket_id})", files=files,
                tags=base_tags + extra, metadata=dict(meta),
            ))

    evolutions: list[dict] = []
    for status, path in norm:
        if status == "D" or status == "A" and path.endswith("/SKILL.md"):
            continue
        if any(path.startswith(r) for r in SKILL_ROOTS):
            target = "skill_instruction"
        elif path in RULE_FILES:
            target = "context_rule"
        else:
            continue
        evolutions.append({
            "proposal_id": f"evo-{ticket_id.lower()}-{_slug(path)}"[:120],
            "target_kind": target,
            "target_path": path,
            "rationale": f"{title.strip()} ({ticket_id})",
            "metadata": dict(meta, recorded_only=True),
        })
    return components, evolutions


def apply_registration(
    components: Sequence[ComponentDescriptor],
    evolutions: Sequence[dict],
    manager=None,
    engine=None,
) -> dict[str, list[str]]:
    """Persist into the stores read by the Hub endpoints (idempotent by id)."""
    from core.evolution.models import EvolutionProposal, EvolutionStatus, EvolutionTrigger

    result: dict[str, list[str]] = {"components": [], "evolutions": []}
    if components:
        if manager is None:
            from core.catalog.manager import CrossProjectCatalogManager
            manager = CrossProjectCatalogManager()
        for comp in components:
            prev = manager.get_component(comp.id)
            if prev is not None and prev.metadata.get("origin") != ORIGIN:
                continue  # never overwrite hand-curated/builtin entries
            history = list(prev.metadata.get("tickets", [])) if prev else []
            entry = {"ticket_id": comp.metadata.get("ticket_id"), "pr_url": comp.metadata.get("pr_url")}
            if entry not in history:
                history.append(entry)
            comp.metadata["tickets"] = history
            if prev is not None:
                comp.created_at = prev.created_at
            manager.register_component(comp)
            result["components"].append(comp.id)
    if evolutions:
        if engine is None:
            from core.evolution.engine import FactoryEvolutionEngine
            engine = FactoryEvolutionEngine()
        added = False
        for ev in evolutions:
            if ev["proposal_id"] in engine._proposals:
                continue
            engine._proposals[ev["proposal_id"]] = EvolutionProposal(
                proposal_id=ev["proposal_id"], target_kind=ev["target_kind"], target_path=ev["target_path"],
                trigger=EvolutionTrigger.MANUAL_PROPOSAL,
                patch_content=f"Delivered by {ev['metadata'].get('ticket_id')} {ev['metadata'].get('pr_url', '')}".strip(),
                rationale=ev["rationale"], status=EvolutionStatus.PROMOTED,
                promoted_at=datetime.now(timezone.utc), metadata=ev["metadata"],
            )
            result["evolutions"].append(ev["proposal_id"])
            added = True
        if added:
            engine._save()
    return result


MAX_SYNC_ITEMS = 200
MAX_SYNC_FILES = 10
DEFAULT_HUB_URL = "https://darkhub.ggcampos.com"
SYNC_PATH = "/api/catalog/delivery-sync"
_META_KEYS = ("ticket_id", "pr_url", "delivered_at")


def _trunc(value: object, limit: int) -> str:
    return str(value or "")[:limit]


def sanitize_payload(payload: dict) -> tuple[list[ComponentDescriptor], list[dict]]:
    """Validate and bound an untrusted sync payload (<= 200 items, truncated text)."""
    comps: list[ComponentDescriptor] = []
    for raw in list(payload.get("components") or [])[:MAX_SYNC_ITEMS]:
        try:
            data = dict(raw)
            data["name"] = _trunc(data.get("name"), 200) or "component"
            data["description"] = _trunc(data.get("description"), 500) or data["name"]
            data["id"] = _slug(_trunc(data.get("id"), 120))
            for key, limit in (("tags", 60), ("dependencies", 80), ("compatible_archetypes", 60)):
                data[key] = [_trunc(t, limit) for t in list(data.get(key) or [])[:20]]
            files = []
            for f in list(data.get("files") or [])[:MAX_SYNC_FILES]:
                f = dict(f)
                files.append({**f, "path": _trunc(f.get("path"), 300), "content": _trunc(f.get("content"), MAX_FILE_BYTES)})
            data["files"] = files
            meta = {k: _trunc(v, 300) for k, v in dict(data.get("metadata") or {}).items() if k in _META_KEYS}
            meta["origin"] = ORIGIN
            data["metadata"] = meta
            comps.append(ComponentDescriptor.model_validate(data))
        except Exception as exc:  # noqa: BLE001 - skip malformed items
            logger.info("catalog sync: skipped malformed component: %s", exc)
    evos: list[dict] = []
    for raw in list(payload.get("evolutions") or [])[:MAX_SYNC_ITEMS]:
        try:
            kind = str(raw.get("target_kind"))
            pid = _trunc(raw.get("proposal_id"), 120)
            path = _trunc(raw.get("target_path"), 300)
            if kind not in ("skill_instruction", "context_rule") or not pid or not path:
                continue
            meta = {k: _trunc(v, 300) for k, v in dict(raw.get("metadata") or {}).items() if k in _META_KEYS}
            meta.update(origin=ORIGIN, recorded_only=True)
            evos.append({
                "proposal_id": pid, "target_kind": kind, "target_path": path,
                "rationale": _trunc(raw.get("rationale"), 500) or "delivery", "metadata": meta,
            })
        except Exception as exc:  # noqa: BLE001
            logger.info("catalog sync: skipped malformed evolution: %s", exc)
    return comps, evos


def merge_payload(payload: dict, manager=None, engine=None) -> dict[str, list[str]]:
    """Hub side: idempotent merge of a pushed payload into the Hub's own stores."""
    comps, evos = sanitize_payload(payload)
    return apply_registration(comps, evos, manager=manager, engine=engine)


def build_payload(components: Sequence[ComponentDescriptor], evolutions: Sequence[dict]) -> dict:
    return {"components": [c.model_dump(mode="json") for c in components], "evolutions": list(evolutions)}


def load_telemetry_key(repo_root: Path) -> str:
    quotes = "\"'"
    key = os.environ.get("DARKFAC_TELEMETRY_KEY", "").strip().strip(quotes)
    if key:
        return key
    try:
        for line in (repo_root / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            name, sep, value = line.partition("=")
            if sep and name.strip() == "DARKFAC_TELEMETRY_KEY":
                return value.strip().strip(quotes)
    except OSError:
        pass
    return ""


def push_to_hub(payload: dict, *, repo_root: Optional[Path] = None, timeout: float = 5.0, post=None) -> bool:
    """Best-effort POST to the production Hub. Never raises; never logs the key."""
    try:
        if not payload.get("components") and not payload.get("evolutions"):
            return False
        if post is None and os.environ.get("PYTEST_CURRENT_TEST"):
            return False  # tests must inject ``post``; never touch the network implicitly
        from core.paths import project_root

        key = load_telemetry_key(repo_root or project_root())
        if not key:
            return False
        base = os.environ.get("DARKHUB_CLOUD_URL", DEFAULT_HUB_URL).rstrip("/")
        if post is None:
            import httpx

            post = httpx.post
        resp = post(
            f"{base}{SYNC_PATH}", json=payload, timeout=timeout,
            headers={"Content-Type": "application/json", "X-DarkFac-Telemetry-Key": key, "User-Agent": "DarkFac-CatalogSync"},
        )
        ok = getattr(resp, "status_code", 0) == 200
        if not ok:
            logger.warning("catalog push to Hub failed: HTTP %s", getattr(resp, "status_code", "?"))
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("catalog push to Hub failed: %s", type(exc).__name__)
        return False


def register_delivery(
    ticket_id: str,
    title: str,
    changes: Sequence[Change],
    read_file: ReadFile,
    tags: Iterable[str] = (),
    pr_url: str = "",
    *,
    manager=None,
    engine=None,
    dry_run: bool = False,
    push: bool = False,
    post=None,
    repo_root: Optional[Path] = None,
) -> dict[str, list[str]]:
    """Best-effort entry point: never raises, never alters the delivery outcome."""
    try:
        components, evolutions = classify_changes(ticket_id, title, changes, read_file, tags, pr_url)
        if dry_run:
            return {"components": [c.id for c in components], "evolutions": [e["proposal_id"] for e in evolutions]}
        result = apply_registration(components, evolutions, manager=manager, engine=engine)
        if push:
            push_to_hub(build_payload(components, evolutions), repo_root=repo_root, post=post)
        return result
    except Exception as exc:  # noqa: BLE001 - catalog is advisory, delivery must not depend on it
        logger.warning("catalog registration skipped for %s: %s", ticket_id, exc)
        return {"components": [], "evolutions": []}


def read_from_worktree(root: Path) -> ReadFile:
    def _read(path: str) -> Optional[str]:
        try:
            return (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    return _read
