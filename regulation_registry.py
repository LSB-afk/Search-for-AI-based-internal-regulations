from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


EMPTY_STATE = {
    "schema_version": 1,
    "regulations": {},
    "versions": {},
    "scan_runs": [],
    "events": [],
}


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_iso_date(value: str) -> datetime.date:
    return datetime.fromisoformat(value).date()


def _normalize_title(value: str) -> str:
    title = unicodedata.normalize("NFC", value).strip()
    title = re.sub(r"\.(?:hwp|hwpx|pdf|docx?|xlsx?)$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"[\s_-]*(?:\d{4}[.-]\d{1,2}[.-]\d{1,2}|\d{8})$", "", title).strip()
    return title


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class RegulationRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.state = self._load()

    def record_detection(
        self,
        canonical_title: str,
        source_path: str,
        content_hash: str,
        effective_from: str,
        chunk_ids: list[str],
        category: str | None = None,
        change_type: str | None = None,
    ) -> dict[str, Any]:
        title = _normalize_title(canonical_title)
        existing = self._find_duplicate(title, content_hash)
        if existing is not None:
            return copy.deepcopy(existing)

        regulation_id = self._regulation_id_for(title, category)
        version_id = uuid.uuid4().hex
        version = {
            "version_id": version_id,
            "regulation_id": regulation_id,
            "canonical_title": title,
            "source_path": source_path,
            "content_hash": content_hash,
            "effective_from": effective_from,
            "effective_to": None,
            "chunk_ids": list(chunk_ids),
            "category": category,
            "change_type": change_type,
            "status": "detected",
        }
        self.state["versions"][version_id] = version
        self.state["regulations"][regulation_id]["versions"].append(version_id)
        self._append_event("detected", version_id, {"source_path": source_path})
        self._persist()
        return copy.deepcopy(version)

    def approve_version(
        self,
        version_id: str,
        actor: str,
        effective_from: str,
        today: str | None = None,
    ) -> dict[str, Any]:
        version = self._version_or_raise(version_id)
        if version["status"] not in {"detected", "pending", "scheduled"}:
            raise ValueError(f"cannot approve version in {version['status']} status")

        version["effective_from"] = effective_from
        current_day = _parse_iso_date(today or _today_iso())
        effective_day = _parse_iso_date(effective_from)
        version["status"] = "scheduled" if effective_day > current_day else "approved"

        self._recompute_effective_windows(version["regulation_id"], current_day)

        self._append_event(
            "approved",
            version_id,
            {"actor": actor, "effective_from": effective_from, "status": version["status"]},
        )
        self._persist()
        return copy.deepcopy(version)

    def reject_version(self, version_id: str, actor: str, reason: str) -> dict[str, Any]:
        version = self._version_or_raise(version_id)
        if version["status"] not in {"detected", "pending", "scheduled"}:
            raise ValueError(f"cannot reject version in {version['status']} status")

        version["status"] = "rejected"
        version["rejected_by"] = actor
        version["rejection_reason"] = reason
        self._append_event("rejected", version_id, {"actor": actor, "reason": reason})
        self._persist()
        return copy.deepcopy(version)

    def versions(self, as_of: str | None = None, include_history: bool = False) -> list[dict[str, Any]]:
        if include_history:
            return [copy.deepcopy(version) for version in self._sorted_versions()]

        as_of_day = _parse_iso_date(as_of or _today_iso())
        current = []
        for version in self._sorted_versions():
            if version["status"] not in {"approved", "scheduled", "superseded"}:
                continue
            effective_from = _parse_iso_date(version["effective_from"])
            effective_to = version.get("effective_to")
            if effective_from > as_of_day:
                continue
            if effective_to is not None and _parse_iso_date(effective_to) < as_of_day:
                continue
            current.append(copy.deepcopy(version))
        return current

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return copy.deepcopy(self.state["events"][-limit:])

    def scan_sources(self, paths, ingest, effective_date=None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "new_count": 0,
            "changed_count": 0,
            "unchanged_count": 0,
            "error_count": 0,
            "chunks": [],
            "errors": [],
        }
        scan_run = {
            "scan_run_id": uuid.uuid4().hex,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "new_count": 0,
            "changed_count": 0,
            "unchanged_count": 0,
            "error_count": 0,
            "sources": [],
        }

        for path in paths:
            source_path = Path(path)
            resolved_source = str(source_path.resolve())
            title = _normalize_title(source_path.name)
            content_hash = sha256_file(source_path)
            previous = self._latest_version_for_title(title)
            if previous is not None and previous["content_hash"] == content_hash:
                result["unchanged_count"] += 1
                scan_run["sources"].append(
                    {"source_path": resolved_source, "status": "unchanged", "version_id": previous["version_id"]}
                )
                continue

            change_type = "new" if previous is None else "changed"
            try:
                ingest_result = ingest(source_path)
            except Exception as exc:
                version = self._record_scanned_version(
                    title,
                    resolved_source,
                    content_hash,
                    self._effective_date_for(source_path, effective_date),
                    [],
                    "scan_error",
                    "scan_error",
                    {"error": str(exc)},
                    append_detected_event=False,
                )
                error = {"source_path": resolved_source, "error": str(exc), "version_id": version["version_id"]}
                result["error_count"] += 1
                result["errors"].append(error)
                scan_run["sources"].append({"source_path": resolved_source, "status": "error", **error})
                self._append_event("RegulationVersionScanFailed", version["version_id"], error)
                continue

            chunks, chunk_ids = self._index_chunks_for_ingest_result(ingest_result)
            version = self._record_scanned_version(
                title,
                resolved_source,
                content_hash,
                self._effective_date_for(source_path, effective_date),
                chunk_ids,
                "pending",
                change_type,
            )
            for chunk in chunks:
                chunk["regulation_id"] = version["regulation_id"]
                chunk["version_id"] = version["version_id"]
                chunk["version_status"] = version["status"]
            result["chunks"].extend(chunks)
            result[f"{change_type}_count"] += 1
            scan_run["sources"].append(
                {"source_path": resolved_source, "status": change_type, "version_id": version["version_id"]}
            )

        for key in ("new_count", "changed_count", "unchanged_count", "error_count"):
            scan_run[key] = result[key]
        scan_run["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.state["scan_runs"].append(scan_run)
        self._persist()
        return copy.deepcopy(result)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return copy.deepcopy(EMPTY_STATE)
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _persist(self) -> None:
        _write_json_atomic(self.path, self.state)

    def _append_event(self, event_type: str, version_id: str, details: dict[str, Any]) -> None:
        self.state["events"].append(
            {
                "event_id": uuid.uuid4().hex,
                "event_type": event_type,
                "version_id": version_id,
                "details": details,
            }
        )

    def _record_scanned_version(
        self,
        canonical_title: str,
        source_path: str,
        content_hash: str,
        effective_from: str,
        chunk_ids: list[str],
        status: str,
        change_type: str,
        details: dict[str, Any] | None = None,
        append_detected_event: bool = True,
    ) -> dict[str, Any]:
        regulation_id = self._regulation_id_for(canonical_title, None)
        version_id = uuid.uuid4().hex
        version = {
            "version_id": version_id,
            "regulation_id": regulation_id,
            "canonical_title": canonical_title,
            "source_path": source_path,
            "content_hash": content_hash,
            "effective_from": effective_from,
            "effective_to": None,
            "chunk_ids": list(chunk_ids),
            "category": None,
            "change_type": change_type,
            "status": status,
        }
        if details:
            version.update(details)
        self.state["versions"][version_id] = version
        self.state["regulations"][regulation_id]["versions"].append(version_id)
        if append_detected_event:
            self._append_event("detected", version_id, {"source_path": source_path, "change_type": change_type})
        return version

    def _latest_version_for_title(self, canonical_title: str) -> dict[str, Any] | None:
        versions = [
            version
            for version in self.state["versions"].values()
            if version["canonical_title"] == canonical_title and version["status"] != "scan_error"
        ]
        if not versions:
            return None
        return max(versions, key=lambda version: (version.get("effective_from") or "", version["version_id"]))

    def _effective_date_for(self, path: Path, effective_date) -> str:
        if effective_date is None:
            return date_from_path(path)
        value = effective_date(path)
        return value or date_from_path(path)

    def _index_chunks_for_ingest_result(self, ingest_result) -> tuple[list[dict[str, Any]], list[str]]:
        chunks: list[dict[str, Any]] = []
        chunk_ids: list[str] = []
        for item in ingest_result:
            if isinstance(item, dict):
                chunk = copy.deepcopy(item)
                chunks.append(chunk)
                chunk_id = chunk.get("id")
                if chunk_id is not None:
                    chunk_ids.append(str(chunk_id))
            elif isinstance(item, str):
                chunk_ids.append(item)
            else:
                raise TypeError(f"unsupported ingest result item: {type(item).__name__}")
        return chunks, chunk_ids

    def _find_duplicate(self, canonical_title: str, content_hash: str) -> dict[str, Any] | None:
        for version in self.state["versions"].values():
            if version["canonical_title"] == canonical_title and version["content_hash"] == content_hash:
                return version
        return None

    def _regulation_id_for(self, canonical_title: str, category: str | None) -> str:
        for regulation_id, regulation in self.state["regulations"].items():
            if regulation["canonical_title"] == canonical_title:
                return regulation_id

        regulation_id = uuid.uuid4().hex
        self.state["regulations"][regulation_id] = {
            "regulation_id": regulation_id,
            "canonical_title": canonical_title,
            "category": category,
            "versions": [],
        }
        return regulation_id

    def _version_or_raise(self, version_id: str) -> dict[str, Any]:
        try:
            return self.state["versions"][version_id]
        except KeyError:
            raise KeyError(version_id)

    def _versions_for_regulation(self, regulation_id: str) -> list[dict[str, Any]]:
        version_ids = self.state["regulations"][regulation_id]["versions"]
        return [self.state["versions"][version_id] for version_id in version_ids]

    def _recompute_effective_windows(self, regulation_id: str, current_day: date) -> None:
        versions = [
            version
            for version in self._versions_for_regulation(regulation_id)
            if version["status"] in {"approved", "scheduled", "superseded"}
        ]
        versions.sort(key=lambda version: (_parse_iso_date(version["effective_from"]), version["version_id"]))

        for index, version in enumerate(versions):
            effective_day = _parse_iso_date(version["effective_from"])
            next_version = versions[index + 1] if index + 1 < len(versions) else None
            next_effective_day = _parse_iso_date(next_version["effective_from"]) if next_version else None

            version["effective_to"] = (
                (next_effective_day - timedelta(days=1)).isoformat() if next_effective_day else None
            )
            if effective_day > current_day:
                version["status"] = "scheduled"
            elif next_effective_day is not None and next_effective_day <= current_day:
                version["status"] = "superseded"
            else:
                version["status"] = "approved"

    def _sorted_versions(self) -> list[dict[str, Any]]:
        return sorted(
            self.state["versions"].values(),
            key=lambda version: (version["canonical_title"], version["effective_from"], version["version_id"]),
        )


def date_from_path(path: Path) -> str:
    match = re.search(r"(20\d{2})[.\-년]\s*(\d{1,2})[.\-월]\s*(\d{1,2})", path.name)
    if not match:
        return _today_iso()
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return _today_iso()
