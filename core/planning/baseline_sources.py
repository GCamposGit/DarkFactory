"""Bounded, read-only ingestion of the HF-01 source catalog."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .baseline_models import (
    BaselineIssue,
    BaselineSourceKind,
    BaselineSourceSpec,
    CollectedBaseline,
    IssueSeverity,
    PlannedItem,
    SourceObservation,
    SourceStatus,
    utc_now,
)

logger = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 2 * 1024 * 1024


class BaselineCatalogError(ValueError):
    """Raised when the catalog itself cannot satisfy the closed contract."""


class _SourceParseError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ReadFailure(Exception):
    def __init__(self, status: SourceStatus, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class _StableRead:
    status: SourceStatus
    content: bytes | None
    sha256: str | None
    error_code: str | None


def load_catalog(path: Path) -> list[BaselineSourceSpec]:
    """Load and validate the explicit source catalog without executing it."""

    catalog_path = Path(path)
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BaselineCatalogError("CATALOG_MISSING") from exc
    except (OSError, UnicodeError) as exc:
        raise BaselineCatalogError("CATALOG_UNREADABLE") from exc
    except json.JSONDecodeError as exc:
        raise BaselineCatalogError("CATALOG_INVALID_JSON") from exc

    if not isinstance(raw, list) or not raw:
        raise BaselineCatalogError("CATALOG_SCHEMA_CHANGED")

    specs: list[BaselineSourceSpec] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise BaselineCatalogError(f"CATALOG_SCHEMA_CHANGED:{index}")
        try:
            spec = BaselineSourceSpec.model_validate(entry)
        except ValueError as exc:
            raise BaselineCatalogError(f"CATALOG_SCHEMA_CHANGED:{index}") from exc
        if spec.source_id in seen_ids:
            raise BaselineCatalogError("CATALOG_DUPLICATE_SOURCE_ID")
        seen_ids.add(spec.source_id)
        specs.append(spec)

    return sorted(specs, key=lambda item: item.source_id)


def collect_sources(root: Path, catalog: list[BaselineSourceSpec]) -> CollectedBaseline:
    """Read only catalogued sources and extract explicit item declarations.

    No source content is returned.  Every readable source is hashed twice,
    before and after parsing; a mismatch causes one bounded re-read and then a
    conservative ``unstable`` observation.
    """

    root_path = Path(root)
    try:
        resolved_root = root_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("root must be an existing directory") from exc
    if not resolved_root.is_dir():
        raise ValueError("root must be an existing directory")

    observations: list[SourceObservation] = []
    planned_items: list[PlannedItem] = []
    issues: list[BaselineIssue] = []

    for spec in sorted(catalog, key=lambda item: item.source_id):
        observed_at = utc_now()
        safe_path, path_error = _resolve_source_path(resolved_root, spec.relative_path)
        if path_error is not None:
            observation = SourceObservation(
                source_id=spec.source_id,
                relative_path=spec.relative_path,
                status=SourceStatus.ACCESS_DENIED,
                observed_at=observed_at,
                error_code=path_error,
            )
            observations.append(observation)
            issues.append(_issue_for(spec, observation))
            continue

        read_result = _read_stable(safe_path, spec.source_id)
        if read_result.status != SourceStatus.READ:
            observation = SourceObservation(
                source_id=spec.source_id,
                relative_path=spec.relative_path,
                sha256=read_result.sha256,
                status=read_result.status,
                observed_at=observed_at,
                error_code=read_result.error_code,
            )
            observations.append(observation)
            issues.append(_issue_for(spec, observation))
            continue

        assert read_result.content is not None
        try:
            text = read_result.content.decode("utf-8")
            items = _extract_items(spec, text)
        except UnicodeDecodeError:
            observation = SourceObservation(
                source_id=spec.source_id,
                relative_path=spec.relative_path,
                sha256=read_result.sha256,
                status=SourceStatus.INVALID,
                observed_at=observed_at,
                error_code="SOURCE_INVALID_UTF8",
            )
            observations.append(observation)
            issues.append(_issue_for(spec, observation))
            continue
        except json.JSONDecodeError:
            observation = SourceObservation(
                source_id=spec.source_id,
                relative_path=spec.relative_path,
                sha256=read_result.sha256,
                status=SourceStatus.INVALID,
                observed_at=observed_at,
                error_code="SOURCE_INVALID_JSON",
            )
            observations.append(observation)
            issues.append(_issue_for(spec, observation))
            continue
        except _SourceParseError as exc:
            observation = SourceObservation(
                source_id=spec.source_id,
                relative_path=spec.relative_path,
                sha256=read_result.sha256,
                status=SourceStatus.INVALID,
                observed_at=observed_at,
                error_code=exc.code,
            )
            observations.append(observation)
            issues.append(_issue_for(spec, observation))
            continue

        observation = SourceObservation(
            source_id=spec.source_id,
            relative_path=spec.relative_path,
            sha256=read_result.sha256,
            status=SourceStatus.READ,
            observed_at=observed_at,
        )
        observations.append(observation)
        planned_items.extend(items)

    return CollectedBaseline(
        observations=sorted(observations, key=lambda item: item.source_id),
        planned_items=sorted(
            planned_items,
            key=lambda item: (item.source_id, item.item_id),
        ),
        claims=[],
        issues=sorted(issues, key=lambda item: item.issue_id),
    )


def _resolve_source_path(root: Path, relative_path: Path) -> tuple[Path | None, str | None]:
    value = str(relative_path)
    candidate = root / relative_path
    windows_path = PureWindowsPath(value)
    if (
        relative_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ".." in relative_path.parts
    ):
        return None, "SOURCE_OUTSIDE_ROOT"
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None, "SOURCE_PATH_RESOLUTION_FAILED"
    if not resolved.is_relative_to(root):
        return None, "SOURCE_OUTSIDE_ROOT"
    return resolved, None


def _read_stable(path: Path, source_id: str) -> _StableRead:
    try:
        first = _read_bounded(path)
    except _ReadFailure as exc:
        return _StableRead(exc.status, None, None, exc.code)

    first_hash = hashlib.sha256(first).hexdigest()
    try:
        second = _read_bounded(path)
    except _ReadFailure:
        logger.warning("Source %s changed or became unavailable during collection", source_id)
        return _StableRead(SourceStatus.UNSTABLE, None, None, "SOURCE_UNSTABLE")

    second_hash = hashlib.sha256(second).hexdigest()
    if first_hash != second_hash:
        logger.warning("Source %s changed during collection; retrying once", source_id)
        try:
            _read_bounded(path)
        except _ReadFailure:
            pass
        return _StableRead(SourceStatus.UNSTABLE, None, None, "SOURCE_UNSTABLE")

    return _StableRead(SourceStatus.READ, first, first_hash, None)


def _read_bounded(path: Path) -> bytes:
    try:
        if not path.is_file():
            if not path.exists():
                raise _ReadFailure(SourceStatus.MISSING, "SOURCE_MISSING")
            raise _ReadFailure(SourceStatus.INVALID, "SOURCE_NOT_FILE")
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise _ReadFailure(SourceStatus.INVALID, "SOURCE_TOO_LARGE")
        content = path.read_bytes()
    except _ReadFailure:
        raise
    except FileNotFoundError as exc:
        raise _ReadFailure(SourceStatus.MISSING, "SOURCE_MISSING") from exc
    except PermissionError as exc:
        raise _ReadFailure(SourceStatus.ACCESS_DENIED, "SOURCE_ACCESS_DENIED") from exc
    except OSError as exc:
        raise _ReadFailure(SourceStatus.INVALID, "SOURCE_READ_ERROR") from exc
    if len(content) > MAX_SOURCE_BYTES:
        raise _ReadFailure(SourceStatus.INVALID, "SOURCE_TOO_LARGE")
    return content


def _extract_items(spec: BaselineSourceSpec, text: str) -> list[PlannedItem]:
    if spec.kind == BaselineSourceKind.JSON_ITEMS:
        return _extract_json_items(spec, text)
    if spec.kind == BaselineSourceKind.INVENTORY:
        _validate_inventory(text)
        return []
    if spec.kind == BaselineSourceKind.MARKDOWN_TABLE:
        return _extract_markdown_items(spec, text)
    return []


def _extract_json_items(spec: BaselineSourceSpec, text: str) -> list[PlannedItem]:
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise _SourceParseError("SOURCE_SCHEMA_CHANGED")

    items: list[PlannedItem] = []
    seen_ids: set[str] = set()
    for raw in payload["items"]:
        if not isinstance(raw, dict):
            raise _SourceParseError("SOURCE_SCHEMA_CHANGED")
        item_id = _required_text(raw.get("id"))
        title = _required_text(raw.get("title"))
        if not item_id or not title or item_id in seen_ids:
            raise _SourceParseError("SOURCE_SCHEMA_CHANGED")
        dependencies = _parse_json_dependencies(raw.get("dependencies", []))
        status = _first_text(raw, ("delivery_status", "status", "state", "horizon")) or "unknown"
        items.append(
            PlannedItem(
                item_id=item_id,
                title=title,
                declared_status=status,
                dependencies=dependencies,
                source_id=spec.source_id,
                locator=f"{spec.relative_path.as_posix()}#{item_id}",
            )
        )
        seen_ids.add(item_id)
    return items


def _validate_inventory(text: str) -> None:
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise _SourceParseError("SOURCE_SCHEMA_CHANGED")


def _extract_markdown_items(spec: BaselineSourceSpec, text: str) -> list[PlannedItem]:
    lines = text.splitlines()
    scoped_lines = _section_lines(lines, spec.section_heading)
    table = _find_declared_table(scoped_lines, spec.table_header or [])
    if table is None:
        raise _SourceParseError("SOURCE_SCHEMA_CHANGED")
    headers, rows = table
    header_indexes = {header: index for index, header in enumerate(headers)}
    id_index = header_indexes[spec.item_id_column or ""]
    title_index = _find_column(headers, ("Entrega", "Escopo", "Escopo e arquivos principais", "Item"))
    status_index = _find_column(headers, ("Status", "Horizonte"))
    dependency_index = _find_column(headers, ("Depende de", "Depende", "Pré-requisito"))
    prefix = ""
    items: list[PlannedItem] = []
    seen_ids: set[str] = set()
    for row in rows:
        if len(row) != len(headers):
            raise _SourceParseError("SOURCE_SCHEMA_CHANGED")
        item_id = _strip_markdown(row[id_index])
        if not item_id or item_id in seen_ids:
            raise _SourceParseError("SOURCE_SCHEMA_CHANGED")
        if "-" in item_id:
            prefix = item_id.split("-", 1)[0]
        title = _strip_markdown(row[title_index]) if title_index is not None else item_id
        status = _strip_markdown(row[status_index]) if status_index is not None else "planned"
        dependencies = _parse_markdown_dependencies(
            row[dependency_index] if dependency_index is not None else "",
            prefix,
        )
        items.append(
            PlannedItem(
                item_id=item_id,
                title=title or item_id,
                declared_status=status or "planned",
                dependencies=dependencies,
                source_id=spec.source_id,
                locator=f"{spec.relative_path.as_posix()}#{item_id}",
            )
        )
        seen_ids.add(item_id)
    return items


def _section_lines(lines: list[str], heading: str | None) -> list[str]:
    if heading is None:
        return lines
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading)
    except StopIteration as exc:
        raise _SourceParseError("SOURCE_SCHEMA_CHANGED") from exc
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index].strip()
        if line.startswith("#"):
            next_level = len(line) - len(line.lstrip("#"))
            if next_level <= level:
                end = index
                break
    return lines[start + 1 : end]


def _find_declared_table(lines: list[str], expected_header: list[str]) -> tuple[list[str], list[list[str]]] | None:
    expected = [_normalize_cell(cell) for cell in expected_header]
    for index in range(len(lines) - 1):
        if not lines[index].strip().startswith("|"):
            continue
        headers = _split_row(lines[index])
        separator = _split_row(lines[index + 1]) if lines[index + 1].strip().startswith("|") else []
        if len(headers) != len(expected) or headers != expected:
            continue
        if len(separator) != len(expected) or not all(_is_separator(cell) for cell in separator):
            raise _SourceParseError("SOURCE_SCHEMA_CHANGED")
        rows: list[list[str]] = []
        row_index = index + 2
        while row_index < len(lines) and lines[row_index].strip().startswith("|"):
            rows.append(_split_row(lines[row_index]))
            row_index += 1
        return headers, rows
    return None


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _normalize_cell(value: str) -> str:
    return _strip_markdown(value).strip()


def _strip_markdown(value: str) -> str:
    return re.sub(r"[*_`~]", "", value).strip()


def _is_separator(value: str) -> bool:
    return bool(re.fullmatch(r":?-{3,}:?", value.strip()))


def _find_column(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    normalized = {_normalize_cell(value): index for index, value in enumerate(headers)}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    return None


def _required_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_text(raw: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_json_dependencies(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _SourceParseError("SOURCE_SCHEMA_CHANGED")
    dependencies: list[str] = []
    for dependency in value:
        if isinstance(dependency, str) and dependency.strip():
            dependencies.append(dependency.strip())
        elif isinstance(dependency, dict):
            item_id = dependency.get("item_id", dependency.get("id"))
            if not isinstance(item_id, str) or not item_id.strip():
                raise _SourceParseError("SOURCE_SCHEMA_CHANGED")
            dependencies.append(item_id.strip())
        else:
            raise _SourceParseError("SOURCE_SCHEMA_CHANGED")
    return list(dict.fromkeys(dependencies))


_FULL_ID = re.compile(r"\b(?:DF|RM|HF|USR|INFRA)-\d{1,3}[A-Z]?\b", re.IGNORECASE)
_PREFIXED_RANGE = re.compile(
    r"\b(?P<prefix>DF|RM|HF|USR|INFRA)-(?P<start>\d{1,3})\s*[–-]\s*(?P<end>\d{1,3})(?P<suffix>[A-Z]?)\b",
    re.IGNORECASE,
)
_BARE_RANGE = re.compile(r"(?<![A-Za-z0-9])(?P<start>\d{1,3})\s*[–-]\s*(?P<end>\d{1,3})(?![A-Za-z0-9])")
_BARE_ID = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d{1,3})(?![A-Za-z0-9])")


def _parse_markdown_dependencies(value: str, prefix: str) -> list[str]:
    text = _strip_markdown(value)
    if not text or text in {"—", "-"}:
        return []
    dependencies: list[str] = []
    for match in _PREFIXED_RANGE.finditer(text):
        dependencies.extend(
            f"{match.group('prefix').upper()}-{number:02d}"
            for number in range(int(match.group("start")), int(match.group("end")) + 1)
        )
    without_ranges = _PREFIXED_RANGE.sub("", text)
    dependencies.extend(item.upper() for item in _FULL_ID.findall(without_ranges))
    for match in _BARE_RANGE.finditer(without_ranges):
        if prefix:
            dependencies.extend(
                f"{prefix.upper()}-{number:02d}"
                for number in range(int(match.group("start")), int(match.group("end")) + 1)
            )
    without_ranges = _BARE_RANGE.sub("", without_ranges)
    if prefix:
        dependencies.extend(
            f"{prefix.upper()}-{int(match.group('number')):02d}"
            for match in _BARE_ID.finditer(without_ranges)
        )
    return list(dict.fromkeys(dependencies))


def _issue_for(spec: BaselineSourceSpec, observation: SourceObservation) -> BaselineIssue:
    code = observation.error_code or "SOURCE_INVALID"
    severity = IssueSeverity.ERROR if spec.required else IssueSeverity.WARNING
    action = {
        "SOURCE_MISSING": "Restore the source or mark it optional before reconciliation.",
        "SOURCE_ACCESS_DENIED": "Make the source readable within the repository root.",
        "SOURCE_OUTSIDE_ROOT": "Replace the source path with an explicit repository-relative path.",
        "SOURCE_TOO_LARGE": "Reduce the source or split it before baseline collection.",
        "SOURCE_UNSTABLE": "Repeat collection after the source stops changing.",
        "SOURCE_INVALID_JSON": "Repair the source JSON without changing the catalog contract.",
        "SOURCE_INVALID_UTF8": "Convert the source to strict UTF-8.",
        "SOURCE_SCHEMA_CHANGED": "Review the declared source schema before reconciliation.",
    }.get(code, "Review the source and collect it again.")
    return BaselineIssue(
        issue_id=f"{code}:{spec.source_id}",
        code=code,
        severity=severity,
        item_ids=[],
        source_ids=[spec.source_id],
        required_action=action,
        target_package="HF-01",
    )
