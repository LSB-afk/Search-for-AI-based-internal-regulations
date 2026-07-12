from __future__ import annotations

import copy
import json
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
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
        if version["status"] not in {"detected", "scheduled"}:
            raise ValueError(f"cannot approve version in {version['status']} status")

        version["effective_from"] = effective_from
        current_day = _parse_iso_date(today or _today_iso())
        effective_day = _parse_iso_date(effective_from)
        version["status"] = "scheduled" if effective_day > current_day else "approved"

        for previous in self._versions_for_regulation(version["regulation_id"]):
            if previous["version_id"] == version_id or previous["status"] != "approved":
                continue
            if _parse_iso_date(previous["effective_from"]) < effective_day:
                previous["status"] = "superseded"
                previous["effective_to"] = (effective_day - timedelta(days=1)).isoformat()

        self._append_event(
            "approved",
            version_id,
            {"actor": actor, "effective_from": effective_from, "status": version["status"]},
        )
        self._persist()
        return copy.deepcopy(version)

    def reject_version(self, version_id: str, actor: str, reason: str) -> dict[str, Any]:
        version = self._version_or_raise(version_id)
        if version["status"] not in {"detected", "scheduled"}:
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

    def _sorted_versions(self) -> list[dict[str, Any]]:
        return sorted(
            self.state["versions"].values(),
            key=lambda version: (version["canonical_title"], version["effective_from"], version["version_id"]),
        )
