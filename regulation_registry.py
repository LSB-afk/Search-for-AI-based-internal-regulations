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
from zoneinfo import ZoneInfo


EMPTY_STATE = {
    "schema_version": 1,
    "regulations": {},
    "versions": {},
    "scan_runs": [],
    "events": [],
}

BUSINESS_TIMEZONE = ZoneInfo("Asia/Seoul")


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def business_today_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(BUSINESS_TIMEZONE).date().isoformat()


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
        effective_from: str | None,
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
            "status": "pending",
        }
        self.state["versions"][version_id] = version
        self.state["regulations"][regulation_id]["versions"].append(version_id)
        self._append_event(
            "RegulationVersionDetected",
            version_id,
            {"source_path": source_path},
            summary=f"{title} 새 버전 감지",
        )
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
        current_day = _parse_iso_date(today or business_today_iso())
        effective_day = _parse_iso_date(effective_from)
        version["status"] = "scheduled" if effective_day > current_day else "approved"

        transitions = self._recompute_effective_windows(version["regulation_id"], current_day)
        self._record_status_transitions(transitions)

        self._append_event(
            "RegulationVersionScheduled" if version["status"] == "scheduled" else "RegulationVersionApproved",
            version_id,
            {"effective_from": effective_from, "status": version["status"]},
            actor_role=actor,
            actor_name=actor,
            summary=f"{version['canonical_title']} 버전 {version['status']}",
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
        self._append_event(
            "RegulationVersionRejected",
            version_id,
            {"reason": reason},
            actor_role=actor,
            actor_name=actor,
            summary=f"{version['canonical_title']} 버전 반려",
        )
        self._persist()
        return copy.deepcopy(version)

    def mark_versions_indexed(self, version_ids: list[str]) -> None:
        for version_id in version_ids:
            version = self._version_or_raise(version_id)
            version["indexed"] = True
            version["indexed_at"] = datetime.now(timezone.utc).isoformat()
            self._append_event(
                "RegulationVersionIndexed",
                version_id,
                {"version_id": version_id},
                summary=f"{version['canonical_title']} 검색 색인 완료",
            )
        self._persist()

    def versions(self, as_of: str | None = None, include_history: bool = False) -> list[dict[str, Any]]:
        current_day = business_today_iso()
        if as_of is None or as_of == current_day:
            self.refresh_statuses(today=current_day)
        if include_history:
            return [
                copy.deepcopy(version)
                for version in self._sorted_versions()
                if version["status"] in {"approved", "scheduled", "superseded"}
                and version.get("effective_from")
            ]

        as_of_day = _parse_iso_date(as_of or current_day)
        allowed = []
        for version in self._sorted_versions():
            if version["status"] not in {"approved", "scheduled", "superseded"}:
                continue
            if not version.get("effective_from"):
                continue
            effective_from = _parse_iso_date(version["effective_from"])
            effective_to = version.get("effective_to")
            if effective_from > as_of_day:
                continue
            if effective_to is not None and _parse_iso_date(effective_to) < as_of_day:
                continue
            allowed.append(copy.deepcopy(version))
        return allowed

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return copy.deepcopy(self.state["events"][-limit:])

    def record_event(
        self,
        event_type: str,
        *,
        summary: str,
        metadata: dict[str, Any] | None = None,
        version_id: str | None = None,
        actor_role: str = "system",
        actor_name: str = "regulation-system",
        target_type: str | None = None,
        target_id: str | None = None,
        result: str = "success",
    ) -> dict[str, Any]:
        event = self._build_event(
            event_type,
            summary=summary,
            metadata=metadata,
            version_id=version_id,
            actor_role=actor_role,
            actor_name=actor_name,
            target_type=target_type,
            target_id=target_id,
            result=result,
        )
        self.state["events"].append(event)
        self._persist()
        return copy.deepcopy(event)

    def refresh_statuses(self, today: str | None = None) -> int:
        current_day = _parse_iso_date(today or business_today_iso())
        transitions: list[tuple[dict[str, Any], str, str]] = []
        for regulation_id in self.state["regulations"]:
            transitions.extend(self._recompute_effective_windows(regulation_id, current_day))
        if not transitions:
            return 0
        self._record_status_transitions(transitions)
        self._persist()
        return len(transitions)

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
            try:
                source_path = Path(path)
                resolved_source = str(source_path.resolve())
                title = _normalize_title(source_path.name)
                content_hash = sha256_file(source_path)
                previous = self._latest_version_for_title(title)
                if previous is not None and previous["content_hash"] == content_hash:
                    if self._version_needs_index_retry(previous):
                        chunks, chunk_ids = self._chunks_for_source(source_path, ingest)
                        previous["chunk_ids"] = chunk_ids
                        for chunk in chunks:
                            self._inject_version_metadata(chunk, previous)
                        result["chunks"].extend(chunks)
                        scan_run["sources"].append(
                            {
                                "source_path": resolved_source,
                                "status": "index_retry",
                                "version_id": previous["version_id"],
                            }
                        )
                        continue
                    result["unchanged_count"] += 1
                    scan_run["sources"].append(
                        {"source_path": resolved_source, "status": "unchanged", "version_id": previous["version_id"]}
                    )
                    continue

                change_type = "new" if previous is None else "changed"
                ingest_result = ingest(source_path)
                chunks, chunk_ids = self._index_chunks_for_ingest_result(ingest_result)
                version = self._record_scanned_version(
                    title,
                    resolved_source,
                    content_hash,
                    self._effective_date_for(source_path, effective_date),
                    chunk_ids,
                    "pending",
                    change_type,
                    {"indexed": not chunks, "index_required": bool(chunks)},
                )
                for chunk in chunks:
                    self._inject_version_metadata(chunk, version)
                result["chunks"].extend(chunks)
                result[f"{change_type}_count"] += 1
                scan_run["sources"].append(
                    {"source_path": resolved_source, "status": change_type, "version_id": version["version_id"]}
                )
            except Exception as exc:
                error = self._record_scan_error(path, exc)
                result["error_count"] += 1
                result["errors"].append(error)
                scan_run["sources"].append({"status": "error", **error})
                continue

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

    def _build_event(
        self,
        event_type: str,
        *,
        summary: str,
        metadata: dict[str, Any] | None = None,
        version_id: str | None = None,
        actor_role: str = "system",
        actor_name: str = "regulation-registry",
        target_type: str | None = None,
        target_id: str | None = None,
        result: str = "success",
    ) -> dict[str, Any]:
        resolved_target_type = target_type or ("regulation_version" if version_id else "system_action")
        resolved_target_id = target_id or version_id or event_type
        return {
            "event_id": uuid.uuid4().hex,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "actor_role": actor_role,
            "actor_name": actor_name,
            "event_type": event_type,
            "target_type": resolved_target_type,
            "target_id": resolved_target_id,
            "version_id": version_id,
            "summary": summary,
            "result": result,
            "metadata": copy.deepcopy(metadata or {}),
        }

    def _append_event(
        self,
        event_type: str,
        version_id: str,
        details: dict[str, Any],
        *,
        actor_role: str = "system",
        actor_name: str = "regulation-registry",
        summary: str | None = None,
        result: str = "success",
    ) -> None:
        self.state["events"].append(
            self._build_event(
                event_type,
                summary=summary or event_type,
                metadata=details,
                version_id=version_id,
                actor_role=actor_role,
                actor_name=actor_name,
                result=result,
            )
        )

    def _record_scanned_version(
        self,
        canonical_title: str,
        source_path: str,
        content_hash: str,
        effective_from: str | None,
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
            self._append_event(
                "RegulationVersionDetected",
                version_id,
                {"source_path": source_path, "change_type": change_type},
                summary=f"{canonical_title} 새 버전 감지",
            )
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

    def _effective_date_for(self, path: Path, effective_date) -> str | None:
        if effective_date is None:
            return date_from_path(path)
        value = effective_date(path)
        return value or date_from_path(path)

    def _chunks_for_source(self, source_path: Path, ingest) -> tuple[list[dict[str, Any]], list[str]]:
        return self._index_chunks_for_ingest_result(ingest(source_path))

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

    def _inject_version_metadata(self, chunk: dict[str, Any], version: dict[str, Any]) -> None:
        chunk["regulation_id"] = version["regulation_id"]
        chunk["version_id"] = version["version_id"]
        chunk["version_status"] = version["status"]

    def _version_needs_index_retry(self, version: dict[str, Any]) -> bool:
        return bool(version.get("index_required")) and not bool(version.get("indexed"))

    def _record_scan_error(self, path, exc: Exception) -> dict[str, Any]:
        source_path = Path(path)
        try:
            resolved_source = str(source_path.resolve())
        except Exception:
            resolved_source = str(source_path)
        try:
            title = _normalize_title(source_path.name)
        except Exception:
            title = source_path.name
        try:
            content_hash = sha256_file(source_path)
        except Exception:
            content_hash = f"scan-error:{uuid.uuid4().hex}"
        version = self._record_scanned_version(
            title,
            resolved_source,
            content_hash,
            date_from_path(source_path),
            [],
            "scan_error",
            "scan_error",
            {"error": str(exc), "retryable": True},
            append_detected_event=False,
        )
        error = {
            "source_path": resolved_source,
            "error": str(exc),
            "version_id": version["version_id"],
            "retryable": True,
        }
        self._append_event(
            "RegulationVersionScanFailed",
            version["version_id"],
            error,
            summary=f"{title} 파일 처리 실패",
            result="failure",
        )
        return error

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

    def _recompute_effective_windows(
        self, regulation_id: str, current_day: date
    ) -> list[tuple[dict[str, Any], str, str]]:
        versions = [
            version
            for version in self._versions_for_regulation(regulation_id)
            if version["status"] in {"approved", "scheduled", "superseded"}
            and version.get("effective_from")
        ]
        versions.sort(key=lambda version: (_parse_iso_date(version["effective_from"]), version["version_id"]))
        transitions = []

        for index, version in enumerate(versions):
            previous_status = version["status"]
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
            if version["status"] != previous_status:
                transitions.append((version, previous_status, version["status"]))
        return transitions

    def _record_status_transitions(self, transitions: list[tuple[dict[str, Any], str, str]]) -> None:
        for version, previous_status, current_status in transitions:
            if current_status == "superseded":
                event_type = "RegulationVersionSuperseded"
                summary = f"{version['canonical_title']} 이전 버전 종료"
            elif previous_status == "scheduled" and current_status == "approved":
                event_type = "ScheduledVersionActivated"
                summary = f"{version['canonical_title']} 예약 버전 시행"
            else:
                continue
            self._append_event(
                event_type,
                version["version_id"],
                {"previous_status": previous_status, "status": current_status},
                summary=summary,
            )

    def _sorted_versions(self) -> list[dict[str, Any]]:
        return sorted(
            self.state["versions"].values(),
            key=lambda version: (
                version["canonical_title"],
                version.get("effective_from") or "",
                version["version_id"],
            ),
        )


def date_from_path(path: Path) -> str | None:
    match = re.search(r"(20\d{2})[.\-년]\s*(\d{1,2})[.\-월]\s*(\d{1,2})", path.name)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None
