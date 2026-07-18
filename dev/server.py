#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import calendar
import hashlib
import html
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import uuid
import zipfile
from collections import Counter, defaultdict
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from auto_ingest import AutomaticIngestService, IngestAlreadyRunning
from regulation_registry import RegulationRegistry, business_today_iso

try:
    import pdfplumber  # type: ignore
except Exception:  # pragma: no cover - handled at runtime
    pdfplumber = None


ROOT = Path(__file__).resolve().parent
APP_ROOT = ROOT.parent
STATIC_DIR = APP_ROOT / "static"
DATA_DIR = APP_ROOT / "data"
UPLOADS_DIR = APP_ROOT / "uploads"
INDEX_FILE = DATA_DIR / "index.json"
REGISTRY_FILE = DATA_DIR / "regulation_registry.json"
WORKSPACE_DIR = APP_ROOT.parent
HWP5TXT = Path(os.environ.get("HWP5TXT_BIN") or shutil.which("hwp5txt") or "/opt/anaconda3/bin/hwp5txt")
BUNDLED_PYTHON = Path(
    os.environ.get("REG_RAG_PDF_PYTHON")
    or "/Users/leeseungbo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
)

ROLE_LEVEL = {"employee": 1, "audit": 2, "admin": 3}
PERMISSION_LEVEL = {"public": 0, "internal": 1, "audit": 2, "admin": 3}

ROLE_LABEL = {"employee": "일반직원", "audit": "감사실", "admin": "관리자"}
PERMISSION_LABEL = {"public": "공개", "internal": "내부", "audit": "감사", "admin": "관리자"}
VERSION_STATUS_LABEL = {"approved": "승인", "scheduled": "시행 예정", "superseded": "이전 버전"}

STOPWORDS = {
    "그리고",
    "그러나",
    "대한",
    "관련",
    "기준",
    "경우",
    "무엇",
    "어떻게",
    "어떤",
    "있는",
    "없는",
    "한다",
    "된다",
    "합니다",
    "주세요",
    "알려줘",
    "대해",
    "으로",
    "에서",
    "에게",
    "까지",
    "부터",
    "으로써",
}

REGISTRY = RegulationRegistry(REGISTRY_FILE)
DATA_LOCK = RLock()
AUTO_INGEST_SERVICE: AutomaticIngestService | None = None
MUTATION_PATHS = {
    "/api/versions/approve",
    "/api/versions/reject",
    "/api/upload",
    "/api/ingest-local",
    "/api/reset",
}


def allowed_cors_origins() -> set[str]:
    return {
        origin.strip().rstrip("/")
        for origin in os.environ.get("REG_RAG_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    }


def demo_mutations_enabled() -> bool:
    return os.environ.get("REG_RAG_ENABLE_DEMO_MUTATIONS") == "1"


def parse_auto_ingest_interval(value: str | None) -> int:
    raw_value = value or "60"
    try:
        interval = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            "REG_RAG_AUTO_INGEST_INTERVAL_SECONDS must be an integer"
        ) from exc
    if interval < 10:
        raise ValueError(
            "REG_RAG_AUTO_INGEST_INTERVAL_SECONDS must be at least 10"
        )
    return interval


def add_api_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    origin = handler.headers.get("Origin")
    if not origin or origin.rstrip("/") not in allowed_cors_origins():
        return
    handler.send_header("Access-Control-Allow-Origin", origin)
    handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    if handler.headers.get("Access-Control-Request-Private-Network", "").lower() == "true":
        handler.send_header("Access-Control-Allow-Private-Network", "true")


def configured_source_roots() -> list[Path]:
    roots: list[Path] = []
    env_value = os.environ.get("REG_RAG_SOURCE_DIRS", "")
    for raw_path in env_value.split(os.pathsep):
        raw_path = raw_path.strip()
        if raw_path:
            roots.append(Path(raw_path).expanduser())
    roots.extend([WORKSPACE_DIR, APP_ROOT])

    resolved_roots: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except Exception:
            continue
        if resolved in seen or not resolved.exists() or not resolved.is_dir():
            continue
        seen.add(resolved)
        resolved_roots.append(resolved)
    return resolved_roots


SENSITIVE_QUERY_TERMS = {"대외비", "비밀", "개인정보"}
SENSITIVE_MATCH_TERMS = {"대외비", "비밀", "개인정보", "보안", "비공개"}

SYNONYMS = {
    "징계": ["징계", "문책", "양정", "인사", "복무", "감사"],
    "감사": ["감사", "조사", "점검", "내부통제", "대외비", "자료제출"],
    "권한": ["권한", "열람", "보안", "접근", "등급", "허가"],
    "보안": ["보안", "대외비", "비공개", "접근", "권한", "열람"],
    "시행": ["시행", "개정", "부칙", "효력", "적용", "시점"],
    "개정": ["개정", "시행", "부칙", "효력", "적용", "시점"],
    "계약": ["계약", "회계", "지출", "예산", "입찰"],
    "심의": ["심의", "위원회", "검토", "의결"],
    "휴가": ["휴가", "복무", "근무", "근태"],
    "재난": ["재난", "안전", "대책본부", "비상", "위기"],
    "안전": ["안전", "재난", "대책본부", "비상", "위기"],
    "표": ["표", "별표", "기준표", "양정", "서식"],
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_space(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text)
    raw = re.findall(r"[가-힣A-Za-z0-9]{2,}", text.lower())
    tokens: list[str] = []
    for token in raw:
        if token in STOPWORDS:
            continue
        if re.search(r"[가-힣]", token) and len(token) > 2:
            token = re.sub(r"(으로써|으로|에게|에서|부터|까지|에는|에게는|은|는|이|가|을|를|의|와|과|도|만|로)$", "", token)
        if len(token) >= 2 and token not in STOPWORDS:
            tokens.append(token)
    return tokens


def expand_terms(terms: list[str]) -> list[str]:
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        for key, values in SYNONYMS.items():
            if term == key or key in term or term in key:
                expanded.extend(values)
    seen: set[str] = set()
    result: list[str] = []
    for term in expanded:
        if term not in seen:
            seen.add(term)
            result.append(term)
    return result


def parse_date_value(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            parts = value.replace("/", "-").replace(".", "-").split("-")
            if len(parts) == 3:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            pass
    return None


def detect_date(query: str, explicit: str | None) -> str | None:
    patterns = [
        r"(20\d{2})\s*[년./-]\s*(\d{1,2})\s*[월./-]\s*(\d{1,2})",
        r"(20\d{2})\s*[년./-]\s*(\d{1,2})",
        r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})",
        r"(20\d{2})[-./](\d{1,2})",
        r"(20\d{2})\s*년",
    ]
    for pattern in patterns:
        match = re.search(pattern, query)
        if not match:
            continue
        year = int(match.group(1))
        month = int(match.group(2)) if len(match.groups()) >= 2 and match.group(2) else 12
        day = (
            int(match.group(3))
            if len(match.groups()) >= 3 and match.group(3)
            else calendar.monthrange(year, month)[1]
        )
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    parsed = parse_date_value(explicit)
    if parsed:
        return parsed.isoformat()
    return None


def date_from_text(text: str) -> str | None:
    match = re.search(r"(20\d{2})[.\-년]\s*(\d{1,2})[.\-월]\s*(\d{1,2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def chunk_id() -> str:
    return uuid.uuid4().hex[:12]


def make_chunk(
    *,
    doc_title: str,
    section_title: str,
    text: str,
    permission: str = "internal",
    effective_from: str | None = None,
    effective_to: str | None = None,
    page: int | None = None,
    source_file: str = "sample",
    source_type: str = "sample",
    source_path: str | None = None,
) -> dict[str, Any]:
    clean_text = normalize_space(text)
    return {
        "id": chunk_id(),
        "doc_title": doc_title,
        "section_title": section_title,
        "text": clean_text,
        "permission": permission,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "page": page,
        "source_file": source_file,
        "source_type": source_type,
        "source_path": source_path,
        "tokens": tokenize(f"{doc_title} {section_title} {clean_text}"),
        "created_at": now_ms(),
    }


def sample_chunks() -> list[dict[str, Any]]:
    return [
        make_chunk(
            doc_title="정관",
            section_title="제1장 총칙",
            permission="public",
            effective_from="2023-07-31",
            text=(
                "공사는 지방공기업법과 설립 조례에 따라 공공성과 효율성을 함께 추구한다. "
                "정관은 조직, 임원, 이사회, 사업 범위, 회계의 최상위 기준으로 적용된다. "
                "하위 규정이 정관과 충돌하는 경우 정관을 우선한다."
            ),
        ),
        make_chunk(
            doc_title="인사규정",
            section_title="제28조 복무와 근태",
            permission="internal",
            effective_from="2023-07-31",
            text=(
                "직원은 근무시간, 휴가, 출장, 교육훈련 등 복무 기준을 준수해야 한다. "
                "복무 위반 사항은 사안의 경중과 반복 여부를 고려하여 인사위원회 심의를 거친다."
            ),
        ),
        make_chunk(
            doc_title="인사규정",
            section_title="별표1 징계양정 기준",
            permission="internal",
            effective_from="2023-07-31",
            effective_to="2025-12-30",
            text=(
                "2025년 12월 30일까지 적용되는 징계양정 기준은 비위 유형, 고의성, 피해 규모, "
                "반복 여부를 종합하여 견책, 감봉, 정직, 해임으로 구분한다. 감사 결과가 확정되기 전에는 "
                "징계 수위를 단정하지 않는다."
            ),
        ),
        make_chunk(
            doc_title="인사규정",
            section_title="별표1 징계양정 기준",
            permission="internal",
            effective_from="2025-12-31",
            text=(
                "2025년 12월 31일부터 개정 징계양정 기준을 적용한다. 금품수수, 개인정보 유출, "
                "반복적 복무 위반은 가중 사유로 보며, 감사부서의 사실관계 확인 자료와 인사위원회 의결을 "
                "함께 반영한다."
            ),
        ),
        make_chunk(
            doc_title="감사규정",
            section_title="제12조 감사자료 제출 및 조사",
            permission="audit",
            effective_from="2024-05-20",
            text=(
                "감사부서는 감사 목적 달성에 필요한 자료 제출을 요구할 수 있다. 감사자료, 제보자 정보, "
                "조사계획, 중간 감사 의견은 감사 권한을 가진 사용자에게만 공개한다. 일반 직원에게는 "
                "비식별 처리된 결과만 제공한다."
            ),
        ),
        make_chunk(
            doc_title="문서보안관리규정",
            section_title="제7조 대외비 문서 열람",
            permission="admin",
            effective_from="2025-03-21",
            text=(
                "대외비 문서는 지정된 보안 등급과 직무상 필요성이 동시에 확인된 경우에만 열람할 수 있다. "
                "열람, 다운로드, 출력 이력은 감사 로그로 남겨야 하며 권한이 없는 청크는 검색 단계에서 "
                "제외한다."
            ),
        ),
        make_chunk(
            doc_title="재난안전대책본부 운영지침",
            section_title="제5조 특별지침 우선 적용",
            permission="internal",
            effective_from="2024-01-08",
            text=(
                "재난 또는 비상 대응 상황에서는 일반 복무 기준보다 재난안전대책본부 운영지침을 우선 적용한다. "
                "상위 규정과 특별지침이 함께 검색되는 경우 사안의 성격, 특별 규정 여부, 시행일을 비교하여 "
                "우선순위를 판단한다."
            ),
        ),
        make_chunk(
            doc_title="회계규정",
            section_title="제31조 계약과 지출",
            permission="internal",
            effective_from="2023-07-31",
            text=(
                "계약과 지출은 예산 편성 목적, 계약 절차, 검수 자료, 지출 증빙을 기준으로 처리한다. "
                "예산 외 지출 또는 수의계약 예외 적용은 승인권자와 근거 조항을 함께 확인해야 한다."
            ),
        ),
        make_chunk(
            doc_title="내부통제 운영지침",
            section_title="제9조 위험 점검",
            permission="audit",
            effective_from="2025-12-31",
            text=(
                "내부통제 점검은 회계, 인사, 계약, 정보보안 영역의 위험 신호를 주기적으로 확인한다. "
                "중대한 위험은 감사계획과 연계하며, 개선조치 이행 여부를 별도 관리한다."
            ),
        ),
    ]


def seed_index(force: bool = False) -> dict[str, Any]:
    ensure_dirs()
    if INDEX_FILE.exists() and not force:
        return read_json(INDEX_FILE, {"chunks": []})
    payload = {
        "version": 1,
        "seeded_at": now_ms(),
        "chunks": sample_chunks(),
    }
    write_json(INDEX_FILE, payload)
    return payload


def load_index() -> dict[str, Any]:
    return seed_index(force=False)


def save_chunks(chunks: list[dict[str, Any]]) -> None:
    payload = load_index()
    payload["chunks"] = chunks
    payload["updated_at"] = now_ms()
    write_json(INDEX_FILE, payload)


def can_access(role: str, permission: str) -> bool:
    return ROLE_LEVEL.get(role, 1) >= PERMISSION_LEVEL.get(permission, 1)


def is_effective(chunk: dict[str, Any], as_of: str | None) -> bool:
    if not as_of:
        return True
    point = parse_date_value(as_of)
    if not point:
        return True
    start = parse_date_value(chunk.get("effective_from"))
    end = parse_date_value(chunk.get("effective_to"))
    if start and point < start:
        return False
    if end and point > end:
        return False
    return True


def version_is_effective(version: dict[str, Any], as_of: str | None) -> bool:
    if version.get("status") not in {"approved", "scheduled", "superseded"}:
        return False
    if not as_of:
        return True
    point = parse_date_value(as_of)
    if not point:
        return True
    start = parse_date_value(version.get("effective_from"))
    end = parse_date_value(version.get("effective_to"))
    if start and point < start:
        return False
    if end and point > end:
        return False
    return True


def build_idf(chunks: list[dict[str, Any]]) -> dict[str, float]:
    doc_count = max(len(chunks), 1)
    df: Counter[str] = Counter()
    for chunk in chunks:
        df.update(set(tokenize(f"{chunk.get('doc_title','')} {chunk.get('section_title','')} {chunk.get('text','')}")))
    return {term: math.log((doc_count + 1) / (freq + 0.5)) + 1 for term, freq in df.items()}


def validate_api_date(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"{field_name} must be YYYY-MM-DD")


def score_chunk(chunk: dict[str, Any], terms: list[str], idf: dict[str, float]) -> tuple[float, list[str]]:
    tokens = tokenize(f"{chunk.get('doc_title','')} {chunk.get('section_title','')} {chunk.get('text','')}")
    counts = Counter(tokens)
    title = unicodedata.normalize("NFC", str(chunk.get("doc_title", "")))
    section = unicodedata.normalize("NFC", str(chunk.get("section_title", "")))
    body = unicodedata.normalize("NFC", str(chunk.get("text", "")))
    searchable = f"{title} {section} {body}".lower()
    score = 0.0
    matched: list[str] = []
    for term in terms:
        tf = counts.get(term, 0)
        if tf:
            score += (1 + math.log(tf)) * idf.get(term, 1.0)
            matched.append(term)
        elif term.lower() in searchable:
            score += 0.55 * idf.get(term, 1.0)
            matched.append(term)
    if title and any(term in title.lower() for term in terms):
        score += 1.5
    if section and any(term in section.lower() for term in terms):
        score += 1.2
    return score, sorted(set(matched))


def make_snippet(text: str, terms: list[str], size: int = 210) -> str:
    if not text:
        return ""
    lowered = text.lower()
    positions = [lowered.find(term.lower()) for term in terms if lowered.find(term.lower()) >= 0]
    if positions:
        start = max(min(positions) - 60, 0)
    else:
        start = 0
    snippet = text[start : start + size]
    if start > 0:
        snippet = "..." + snippet
    if start + size < len(text):
        snippet += "..."
    return snippet


def split_sentences(text: str) -> list[str]:
    cleaned = normalize_space(text)
    if not cleaned:
        return []
    marked = re.sub(r"([.!?。])\s+", r"\1\n", cleaned)
    marked = re.sub(r"(다\.|함\.|음\.|요\.)\s+", r"\1\n", marked)
    marked = re.sub(r"\)\s+(?=제\d+조)", ")\n", marked)
    parts = marked.splitlines()
    sentences = [part.strip(" -") for part in parts if len(part.strip()) >= 12]
    if sentences:
        return sentences
    return [cleaned[:260]]


def summarize_text(text: str, terms: list[str], max_sentences: int = 3) -> str:
    sentences = split_sentences(text)
    if not sentences:
        return "요약할 본문이 없습니다."
    scored: list[tuple[float, int, str]] = []
    for idx, sentence in enumerate(sentences):
        lower = sentence.lower()
        score = 0.0
        for term in terms:
            if term and term.lower() in lower:
                score += 2.0
        if re.search(r"목적|적용|심의|의결|제출|권한|위원회|계약|징계|보안|안전|자료", sentence):
            score += 1.0
        score += min(len(sentence), 180) / 300
        scored.append((score, idx, sentence))
    picked = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_sentences]
    ordered = sorted(picked, key=lambda item: item[1])
    bullets = []
    for _, _, sentence in ordered:
        sentence = sentence.strip()
        if len(sentence) > 240:
            sentence = sentence[:237].rstrip() + "..."
        bullets.append(f"- {sentence}")
    return "\n".join(bullets)


def search_chunks(
    chunks: list[dict[str, Any]],
    query: str,
    role: str,
    as_of: str | None,
    limit: int,
    allowed_version_ids: set[str] | None = None,
    include_history: bool = False,
    apply_effective_date_filter: bool = True,
) -> dict[str, Any]:
    explicit_or_detected_date = detect_date(query, as_of)
    raw_query_terms = set(tokenize(query))
    query_terms = expand_terms(tokenize(query))
    idf = build_idf(chunks)
    real_source_available = any(chunk.get("source_type") != "sample" for chunk in chunks)
    blocked_count = 0
    date_filtered_count = 0
    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        version_id = chunk.get("version_id")
        if version_id and allowed_version_ids is not None and version_id not in allowed_version_ids:
            date_filtered_count += 1
            continue
        if not can_access(role, chunk.get("permission", "internal")):
            blocked_count += 1
            continue
        registry_filtered = bool(version_id and allowed_version_ids is not None)
        if apply_effective_date_filter and not registry_filtered and not is_effective(chunk, explicit_or_detected_date):
            date_filtered_count += 1
            continue
        score, matched = score_chunk(chunk, query_terms, idf)
        if score <= 0 and query_terms:
            continue
        if real_source_available:
            if chunk.get("source_type") == "sample":
                score *= 0.35
            else:
                score += 1.5
        if SENSITIVE_QUERY_TERMS.intersection(raw_query_terms) and not SENSITIVE_MATCH_TERMS.intersection(matched):
            continue
        download = None
        if chunk.get("source_path"):
            download = {
                "source": f"/api/download/source?id={chunk.get('id')}",
                "source_pdf": f"/api/download/source-pdf?id={chunk.get('id')}",
            }
        scored.append(
            {
                **{k: v for k, v in chunk.items() if k not in {"tokens", "source_path"}},
                "score": round(score, 4),
                "matched_terms": matched,
                "snippet": make_snippet(chunk.get("text", ""), matched or query_terms),
                "summary": summarize_text(chunk.get("text", ""), matched or query_terms),
                "download": download,
            }
        )

    scored.sort(key=lambda item: item["score"], reverse=True)
    results = scored[:limit]
    return {
        "query": query,
        "role": role,
        "role_label": ROLE_LABEL.get(role, role),
        "as_of": explicit_or_detected_date,
        "query_terms": query_terms,
        "results": results,
        "answer": generate_answer(query, results, role, explicit_or_detected_date, blocked_count, date_filtered_count),
        "blocked_count": blocked_count,
        "date_filtered_count": date_filtered_count,
        "total_chunks": len(chunks),
        "include_history": include_history,
    }


def hydrate_chunk_version_metadata(chunk: dict[str, Any]) -> dict[str, Any]:
    hydrated = dict(chunk)
    version_id = chunk.get("version_id")
    version = REGISTRY.state.get("versions", {}).get(version_id)
    if not version:
        return hydrated
    hydrated.update(
        {
            "regulation_id": version["regulation_id"],
            "canonical_title": version["canonical_title"],
            "version_status": version["status"],
            "effective_from": version.get("effective_from"),
            "effective_to": version.get("effective_to"),
        }
    )
    return hydrated


def version_index_chunks(version: dict[str, Any], chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunk_ids = {str(chunk_id) for chunk_id in version.get("chunk_ids", [])}
    version_id = version.get("version_id")
    return [
        chunk
        for chunk in chunks
        if str(chunk.get("id")) in chunk_ids or chunk.get("version_id") == version_id
    ]


def version_download(chunks: list[dict[str, Any]]) -> dict[str, str] | None:
    source_chunk = next(
        (chunk for chunk in chunks if chunk.get("source_path")),
        None,
    )
    if source_chunk is None:
        return None
    chunk_id_value = source_chunk.get("id")
    return {
        "source": f"/api/download/source?id={chunk_id_value}",
        "source_pdf": f"/api/download/source-pdf?id={chunk_id_value}",
    }


def version_chunks_are_accessible(chunks: list[dict[str, Any]], role: str) -> bool:
    if not chunks:
        return True
    required_level = max(
        (PERMISSION_LEVEL.get(chunk.get("permission", "internal"), 1) for chunk in chunks),
        default=1,
    )
    return ROLE_LEVEL.get(role, 1) >= required_level


def build_version_timelines(
    regulation_ids: set[str],
    chunks: list[dict[str, Any]],
    role: str,
) -> list[dict[str, Any]]:
    published_statuses = {"approved", "scheduled", "superseded"}
    timelines: list[dict[str, Any]] = []
    for regulation_id in regulation_ids:
        regulation = REGISTRY.state.get("regulations", {}).get(regulation_id)
        if not regulation:
            continue
        candidate_versions = [
            REGISTRY.state["versions"][version_id]
            for version_id in regulation.get("versions", [])
            if version_id in REGISTRY.state.get("versions", {})
        ]
        versions_with_chunks = []
        for version in candidate_versions:
            if version.get("status") not in published_statuses or not version.get("effective_from"):
                continue
            indexed_chunks = version_index_chunks(version, chunks)
            if version_chunks_are_accessible(indexed_chunks, role):
                versions_with_chunks.append((version, indexed_chunks))
        versions_with_chunks.sort(
            key=lambda item: (item[0].get("effective_from") or "", item[0].get("version_id") or ""),
            reverse=True,
        )
        if not versions_with_chunks:
            continue
        public_versions = []
        for version, indexed_chunks in versions_with_chunks:
            source_file = version.get("source_file")
            if not source_file and version.get("source_path"):
                source_file = Path(str(version["source_path"])).name
            public_versions.append(
                {
                    "version_id": version.get("version_id"),
                    "effective_from": version.get("effective_from"),
                    "effective_to": version.get("effective_to"),
                    "status": version.get("status"),
                    "change_type": version.get("change_type"),
                    "source_file": source_file,
                    "download": version_download(indexed_chunks),
                }
            )
        timelines.append(
            {
                "regulation_id": regulation_id,
                "canonical_title": regulation.get("canonical_title")
                or versions_with_chunks[0][0]["canonical_title"],
                "versions": public_versions,
            }
        )
    timelines.sort(key=lambda item: str(item["canonical_title"]))
    return timelines


def search_index(
    query: str,
    role: str,
    as_of: str | None,
    limit: int = 6,
    include_history: bool = False,
) -> dict[str, Any]:
    with DATA_LOCK:
        return search_index_snapshot(query, role, as_of, limit, include_history)


def search_index_snapshot(
    query: str,
    role: str,
    as_of: str | None,
    limit: int = 6,
    include_history: bool = False,
) -> dict[str, Any]:
    payload = load_index()
    chunks = payload.get("chunks", [])
    effective_search_date = detect_date(query, as_of) or business_today_iso()
    allowed_versions = REGISTRY.versions(effective_search_date, include_history=False)
    allowed_version_ids = {
        version["version_id"] for version in allowed_versions if version_is_effective(version, effective_search_date)
    }
    result = search_chunks(
        chunks,
        query,
        role,
        effective_search_date,
        limit,
        allowed_version_ids=allowed_version_ids,
        include_history=include_history,
    )
    result["results"] = [hydrate_chunk_version_metadata(item) for item in result["results"]]
    result["answer"] = generate_answer(
        query,
        result["results"],
        role,
        result["as_of"],
        result["blocked_count"],
        result["date_filtered_count"],
    )
    result["timelines"] = []
    if not include_history:
        return result

    historical_versions = REGISTRY.versions(include_history=True)
    historical_version_ids = {version["version_id"] for version in historical_versions}
    historical_matches = search_chunks(
        chunks,
        query,
        role,
        effective_search_date,
        max(len(chunks), 1),
        allowed_version_ids=historical_version_ids,
        include_history=True,
        apply_effective_date_filter=False,
    )
    hydrated_historical_results = [
        hydrate_chunk_version_metadata(item) for item in historical_matches["results"]
    ]
    regulation_ids = {
        item["regulation_id"]
        for item in hydrated_historical_results
        if item.get("regulation_id")
    }
    regulation_ids.update(
        item["regulation_id"] for item in result["results"] if item.get("regulation_id")
    )
    result["timelines"] = build_version_timelines(regulation_ids, chunks, role)
    return result


def search_audit_payload(query: str, role: str, as_of: str | None, result_count: int) -> dict[str, Any]:
    return {
        "summary": f"{ROLE_LABEL.get(role, role)} 권한으로 규정 검색 실행",
        "metadata": {
            "query_length": len(query),
            "role": role,
            "as_of": as_of,
            "result_count": result_count,
        },
    }


def generate_answer(
    query: str,
    results: list[dict[str, Any]],
    role: str,
    as_of: str | None,
    blocked_count: int,
    date_filtered_count: int,
) -> str:
    role_label = ROLE_LABEL.get(role, role)
    date_part = f"{as_of} 기준" if as_of else "전체 시행시점"
    if not results:
        parts = [f"{role_label} 권한과 {date_part}으로 확인 가능한 근거를 찾지 못했습니다."]
        if blocked_count:
            parts.append(f"권한 때문에 제외된 청크가 {blocked_count}개 있습니다.")
        if date_filtered_count:
            parts.append(f"시행일 조건 때문에 제외된 청크가 {date_filtered_count}개 있습니다.")
        return " ".join(parts)

    top = results[0]
    lead = (
        f"{role_label} 권한과 {date_part}으로 보면 가장 직접적인 근거는 "
        f"{top['doc_title']}의 {top['section_title']}입니다."
    )
    evidence = []
    for i, item in enumerate(results[:3], 1):
        page = f", p.{item['page']}" if item.get("page") else ""
        period = item.get("effective_from") or "시행일 미상"
        if item.get("effective_to"):
            period += f"~{item['effective_to']}"
        version_status = VERSION_STATUS_LABEL.get(item.get("version_status"))
        status = f", {version_status}" if version_status else ""
        summary = item.get("summary") or summarize_text(item.get("text", ""), item.get("matched_terms", []))
        evidence.append(
            f"{i}. {item['doc_title']} / {item['section_title']}{page} "
            f"[{PERMISSION_LABEL.get(item.get('permission'), item.get('permission'))}, {period}{status}]\n"
            f"요약:\n{summary}\n"
            f"원문 일부: {item['snippet']}"
        )

    caution = []
    if blocked_count:
        caution.append(f"권한 필터로 {blocked_count}개 청크가 제외되었습니다.")
    if date_filtered_count:
        caution.append(f"시행일 필터로 {date_filtered_count}개 청크가 제외되었습니다.")
    suffix = "\n\n필터 결과: " + " ".join(caution) if caution else ""
    return lead + "\n\n근거:\n" + "\n".join(evidence) + suffix


def split_text_into_chunks(text: str, max_chars: int = 900) -> list[tuple[str, str]]:
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+(제\s*\d+\s*조\s*\()", r"\n\1", text)
    text = re.sub(r"\s+(부\s*칙)", r"\n\1", text)
    text = re.sub(r"\s+(\[별(?:표|지)\s*[^\]]+\])", r"\n\1", text)
    lines = [normalize_space(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []

    chunks: list[tuple[str, str]] = []
    current_title = "본문"
    current: list[str] = []
    heading_re = re.compile(r"^(제\s*\d+\s*[장절]|부\s*칙|\[별(?:표|지)\s*[^\]]+\]|[가-힣A-Za-z ]{2,40}(규정|지침|기준|총칙|부칙))")
    article_re = re.compile(r"^(제\s*\d+\s*조\s*\([^)]{1,40}\))\s*(.*)$")

    def flush() -> None:
        nonlocal current
        if not current:
            return
        body = normalize_space(" ".join(current))
        if body:
            chunks.append((current_title, body))
        current = []

    for line in lines:
        article = article_re.match(line)
        if article:
            flush()
            current_title = article.group(1)
            rest = article.group(2).strip()
            if rest:
                current.append(rest)
            continue
        is_heading = len(line) <= 60 and bool(heading_re.search(line))
        if is_heading and current:
            flush()
            current_title = line
            continue
        if is_heading and not current:
            current_title = line
            continue
        current.append(line)
        if sum(len(x) for x in current) >= max_chars:
            flush()
    flush()
    return chunks


def guess_permission(title: str, text: str) -> str:
    source = f"{title} {text}"
    if re.search(r"대외비|비밀|관리자|보안등급|개인정보|열람 이력", source):
        return "admin"
    if re.search(r"감사|조사|제보|징계|내부통제", source):
        return "audit"
    if re.search(r"정관|공개|총칙", source):
        return "public"
    return "internal"


def extract_pdf(path: Path) -> list[dict[str, Any]]:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is not available in this runtime")
    chunks: list[dict[str, Any]] = []
    doc_title = path.stem
    effective_from = date_from_text(path.name)
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            text = page.extract_text(x_tolerance=1, y_tolerance=3, layout=False) or ""
            for section_title, body in split_text_into_chunks(text):
                chunks.append(
                    make_chunk(
                        doc_title=doc_title,
                        section_title=section_title,
                        text=body,
                        permission=guess_permission(doc_title + " " + section_title, body),
                        effective_from=effective_from,
                        page=page_number,
                        source_file=path.name,
                        source_type="pdf",
                        source_path=str(path.resolve()),
                    )
                )
    return chunks


def extract_hwpx(path: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    doc_title = path.stem
    effective_from = date_from_text(path.name)
    parts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".xml"):
                continue
            if "content" not in name.lower() and "section" not in name.lower():
                continue
            try:
                data = zf.read(name).decode("utf-8", errors="ignore")
            except Exception:
                continue
            texts = re.findall(r">([^<>]{2,})<", data)
            parts.extend(t.strip() for t in texts if t.strip())
    text = "\n".join(parts)
    for section_title, body in split_text_into_chunks(text):
        chunks.append(
            make_chunk(
                doc_title=doc_title,
                section_title=section_title,
                text=body,
                permission=guess_permission(doc_title + " " + section_title, body),
                effective_from=effective_from,
                source_file=path.name,
                source_type="hwpx",
                source_path=str(path.resolve()),
            )
        )
    return chunks


def extract_hwp_text(path: Path) -> str:
    if not HWP5TXT.exists():
        raise RuntimeError("hwp5txt is not available")
    completed = subprocess.run(
        [str(HWP5TXT), str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    text = completed.stdout.strip()
    if not text:
        raise RuntimeError("hwp5txt returned empty text")
    return text


def extract_hwp(path: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    doc_title = path.stem
    effective_from = date_from_text(path.name)
    text = extract_hwp_text(path)
    for section_title, body in split_text_into_chunks(text, max_chars=1000):
        chunks.append(
            make_chunk(
                doc_title=doc_title,
                section_title=section_title,
                text=body,
                permission=guess_permission(doc_title + " " + section_title, body),
                effective_from=effective_from,
                source_file=path.name,
                source_type="hwp",
                source_path=str(path.resolve()),
            )
        )
    if chunks:
        return chunks
    return [
        make_chunk(
            doc_title=doc_title,
            section_title="본문",
            text=text,
            permission=guess_permission(doc_title, text),
            effective_from=effective_from,
            source_file=path.name,
            source_type="hwp",
            source_path=str(path.resolve()),
        )
    ]


def ingest_file(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".hwpx":
        return extract_hwpx(path)
    if suffix == ".hwp":
        return extract_hwp(path)
    if suffix == ".zip":
        return ingest_zip(path)
    raise ValueError(f"Unsupported file type: {suffix}")


def ingest_zip(path: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="reg-rag-zip-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        with zipfile.ZipFile(path) as zf:
            zf.extractall(tmp_path)
        for file_path in tmp_path.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in {".pdf", ".hwpx", ".hwp"}:
                continue
            try:
                extracted = ingest_file(file_path)
                for chunk in extracted:
                    chunk["batch_source"] = path.name
                chunks.extend(extracted)
            except Exception:
                chunks.append(
                    make_chunk(
                        doc_title=file_path.stem,
                        section_title="색인 실패",
                        text=f"{file_path.name} 파일은 현재 프로토타입에서 전문을 추출하지 못했습니다.",
                        permission=guess_permission(file_path.stem, file_path.stem),
                        source_file=file_path.name,
                        source_type=file_path.suffix.lower().lstrip("."),
                        source_path=str(path.resolve()),
                    )
                )
    return chunks


def add_chunks(new_chunks: list[dict[str, Any]]) -> int:
    payload = load_index()
    incoming_version_ids = {
        str(chunk["version_id"])
        for chunk in new_chunks
        if chunk.get("version_id") is not None
    }
    existing_chunks = [
        chunk
        for chunk in payload.get("chunks", [])
        if chunk.get("version_id") is None
        or str(chunk["version_id"]) not in incoming_version_ids
    ]
    merged: list[dict[str, Any]] = []
    position_by_id: dict[str, int] = {}
    for chunk in [*existing_chunks, *new_chunks]:
        chunk_id_value = chunk.get("id")
        if chunk_id_value is None:
            merged.append(chunk)
            continue
        stable_id = str(chunk_id_value)
        position = position_by_id.get(stable_id)
        if position is None:
            position_by_id[stable_id] = len(merged)
            merged.append(chunk)
        else:
            merged[position] = chunk
    payload["chunks"] = merged
    payload["updated_at"] = now_ms()
    write_json(INDEX_FILE, payload)
    return len(new_chunks)


def document_summary() -> list[dict[str, Any]]:
    chunks = load_index().get("chunks", [])
    grouped: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        title = chunk.get("doc_title", "문서")
        item = grouped.setdefault(
            title,
            {
                "doc_title": title,
                "chunk_count": 0,
                "source_types": set(),
                "permissions": set(),
                "effective_from": None,
                "effective_to": None,
            },
        )
        item["chunk_count"] += 1
        item["source_types"].add(chunk.get("source_type", "sample"))
        item["permissions"].add(chunk.get("permission", "internal"))
        start = chunk.get("effective_from")
        end = chunk.get("effective_to")
        if start and (item["effective_from"] is None or start < item["effective_from"]):
            item["effective_from"] = start
        if end and (item["effective_to"] is None or end > item["effective_to"]):
            item["effective_to"] = end
    result: list[dict[str, Any]] = []
    for item in grouped.values():
        item["source_types"] = sorted(item["source_types"])
        item["permissions"] = sorted(item["permissions"], key=lambda p: PERMISSION_LEVEL.get(p, 9))
        result.append(item)
    result.sort(key=lambda x: x["doc_title"])
    return result


def local_sources() -> list[Path]:
    regulation_dirs = [
        path
        for root in configured_source_roots()
        for path in root.iterdir()
        if path.is_dir()
        and "정관" in unicodedata.normalize("NFC", path.name)
        and "규정" in unicodedata.normalize("NFC", path.name)
        and path.name != APP_ROOT.name
    ]
    if regulation_dirs:
        files: list[Path] = []
        for directory in regulation_dirs:
            files.extend(
                path
                for path in directory.rglob("*")
                if path.is_file() and path.suffix.lower() in {".pdf", ".hwpx", ".hwp"}
            )
        return sorted(files, key=lambda p: str(p))

    files = []
    for root in configured_source_roots():
        for path in root.iterdir():
            if path.is_file() and path.suffix.lower() in {".pdf", ".hwpx", ".hwp", ".zip"}:
                if "내부망 규정 검색" in path.name or "내부망 규정 검색" in path.name:
                    continue
                files.append(path)
    return sorted(files, key=lambda p: str(p))


def ingest_registered_sources(paths, *, canonical_title=None) -> dict[str, Any]:
    with DATA_LOCK:
        return ingest_registered_sources_snapshot(
            paths, canonical_title=canonical_title
        )


def ingest_registered_sources_snapshot(
    paths, *, canonical_title=None
) -> dict[str, Any]:
    result = REGISTRY.scan_sources(
        paths,
        ingest=ingest_file,
        effective_date=lambda path: date_from_text(path.name),
        canonical_title=canonical_title,
    )
    chunks = result.pop("chunks")
    result["imported_chunks"] = len(chunks)
    indexed_version_ids = {chunk["version_id"] for chunk in chunks if chunk.get("version_id")}
    version_ids = sorted(set(result.pop("version_ids", [])) | indexed_version_ids)
    if chunks:
        add_chunks(chunks)
        REGISTRY.mark_versions_indexed(sorted(indexed_version_ids))
    result["versions"] = [
        public_version(REGISTRY.state["versions"][version_id])
        for version_id in version_ids
        if version_id in REGISTRY.state.get("versions", {})
    ]
    result["documents"] = document_summary()
    return result


def ingest_local_sources() -> dict[str, Any]:
    return ingest_registered_sources(local_sources())


def build_auto_ingest_service(
    *, enabled: bool, interval_seconds: int
) -> AutomaticIngestService:
    return AutomaticIngestService(
        ingest_local_sources,
        enabled=enabled,
        interval_seconds=interval_seconds,
    )


AUTO_INGEST_SERVICE = build_auto_ingest_service(
    enabled=False, interval_seconds=60
)


def safe_upload_name(filename: str) -> str:
    stem = Path(filename).stem[:80] or "upload"
    suffix = Path(filename).suffix.lower()
    stem = re.sub(r"[^\w가-힣(). -]+", "_", stem, flags=re.UNICODE).strip()
    return f"{stem or 'upload'}{suffix}"


def ingest_uploaded_file(filename: str, content: bytes) -> dict[str, Any]:
    safe_name = safe_upload_name(filename)
    upload_dir = UPLOADS_DIR / uuid.uuid4().hex
    upload_dir.mkdir(parents=True, exist_ok=False)
    target = upload_dir / safe_name
    target.write_bytes(content)
    return ingest_registered_sources(
        [target],
        canonical_title=lambda _: safe_name,
    )


def clear_uploads() -> None:
    if not UPLOADS_DIR.exists():
        return
    for child in UPLOADS_DIR.iterdir():
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


def get_chunk_by_id(chunk_id_value: str) -> dict[str, Any] | None:
    with DATA_LOCK:
        return get_chunk_by_id_snapshot(chunk_id_value)


def get_chunk_by_id_snapshot(chunk_id_value: str) -> dict[str, Any] | None:
    for chunk in load_index().get("chunks", []):
        if chunk.get("id") == chunk_id_value:
            return chunk
    return None


def safe_download_name(name: str, suffix: str) -> str:
    stem = Path(name).stem[:80] or "download"
    stem = re.sub(r"[^\w가-힣(). -]+", "_", stem, flags=re.UNICODE).strip() or "download"
    return f"{stem}{suffix}"


def is_safe_source(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    allowed_roots = configured_source_roots() + [UPLOADS_DIR.resolve()]
    return any(resolved == root or root in resolved.parents for root in allowed_roots)


def send_bytes(
    handler: BaseHTTPRequestHandler,
    data: bytes,
    *,
    filename: str,
    content_type: str,
) -> None:
    encoded_name = quote(filename)
    handler.send_response(HTTPStatus.OK)
    add_api_cors_headers(handler)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}")
    handler.end_headers()
    handler.wfile.write(data)


def source_download_audit_payload(chunk: dict[str, Any], outcome: str) -> dict[str, Any]:
    return {
        "summary": "규정 원본 다운로드 요청 처리",
        "metadata": {
            "source_file": chunk.get("source_file"),
            "version_id": chunk.get("version_id"),
            "outcome": outcome,
        },
    }


def record_source_download_event(chunk: dict[str, Any], event_type: str, outcome: str) -> None:
    with DATA_LOCK:
        record_source_download_event_snapshot(chunk, event_type, outcome)


def record_source_download_event_snapshot(
    chunk: dict[str, Any], event_type: str, outcome: str
) -> None:
    payload = source_download_audit_payload(chunk, outcome)
    REGISTRY.record_event(
        event_type,
        summary=payload["summary"],
        metadata=payload["metadata"],
        version_id=chunk.get("version_id"),
        target_type="regulation_version" if chunk.get("version_id") else "document_chunk",
        target_id=chunk.get("version_id") or chunk.get("id"),
        result="success" if outcome == "success" else "failure",
    )


def record_missing_source_download_event(requested_id: str) -> None:
    with DATA_LOCK:
        record_missing_source_download_event_snapshot(requested_id)


def record_missing_source_download_event_snapshot(requested_id: str) -> None:
    requested_id_bytes = requested_id.encode("utf-8", errors="replace")
    REGISTRY.record_event(
        "SourceDownloadFailed",
        summary="규정 원본 다운로드 요청 처리",
        metadata={
            "source_file": None,
            "version_id": None,
            "outcome": "chunk_not_found",
            "requested_id_length": len(requested_id),
            "requested_id_sha256": hashlib.sha256(requested_id_bytes).hexdigest(),
            "requested_id_format_valid": bool(re.fullmatch(r"[0-9a-f]{12}", requested_id)),
        },
        target_type="document_chunk",
        target_id=hashlib.sha256(requested_id_bytes).hexdigest(),
        result="failure",
    )


def send_source_download_bytes(
    handler: BaseHTTPRequestHandler,
    chunk: dict[str, Any],
    data: bytes,
    *,
    filename: str,
    content_type: str,
) -> None:
    try:
        send_bytes(handler, data, filename=filename, content_type=content_type)
    except OSError:
        record_source_download_event(chunk, "SourceDownloadFailed", "send_failed")
        raise


def source_download(handler: BaseHTTPRequestHandler, chunk: dict[str, Any]) -> None:
    source_path = chunk.get("source_path")
    if not source_path:
        record_source_download_event(chunk, "SourceDownloadFailed", "not_available")
        handler.send_error(HTTPStatus.NOT_FOUND, "source file is not available")
        return
    path = Path(source_path)
    if not path.exists() or not path.is_file() or not is_safe_source(path):
        record_source_download_event(chunk, "SourceDownloadFailed", "not_found")
        handler.send_error(HTTPStatus.NOT_FOUND, "source file is not available")
        return
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        data = path.read_bytes()
    except OSError:
        record_source_download_event(chunk, "SourceDownloadFailed", "read_failed")
        handler.send_error(HTTPStatus.NOT_FOUND, "source file is not available")
        return
    send_source_download_bytes(handler, chunk, data, filename=path.name, content_type=content_type)
    record_source_download_event(chunk, "SourceDownloaded", "success")


def source_chunks(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    with DATA_LOCK:
        return source_chunks_snapshot(chunk)


def source_chunks_snapshot(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    source_path = chunk.get("source_path")
    if not source_path:
        return [chunk]
    chunks = [
        item
        for item in load_index().get("chunks", [])
        if item.get("source_path") == source_path
    ]
    return chunks or [chunk]


def source_pdf_download(handler: BaseHTTPRequestHandler, chunk: dict[str, Any]) -> None:
    source_path = chunk.get("source_path")
    if not source_path:
        record_source_download_event(chunk, "SourceDownloadFailed", "not_available")
        handler.send_error(HTTPStatus.NOT_FOUND, "source file is not available")
        return
    path = Path(source_path)
    if not path.exists() or not path.is_file() or not is_safe_source(path):
        record_source_download_event(chunk, "SourceDownloadFailed", "not_found")
        handler.send_error(HTTPStatus.NOT_FOUND, "source file is not available")
        return
    if path.suffix.lower() == ".pdf":
        try:
            data = path.read_bytes()
        except OSError:
            record_source_download_event(chunk, "SourceDownloadFailed", "read_failed")
            handler.send_error(HTTPStatus.NOT_FOUND, "source file is not available")
            return
        send_source_download_bytes(handler, chunk, data, filename=path.name, content_type="application/pdf")
        record_source_download_event(chunk, "SourceDownloaded", "success")
        return
    filename = safe_download_name(f"{path.stem}_원본", ".pdf")
    try:
        data = make_source_pdf(chunk)
    except Exception:
        record_source_download_event(chunk, "SourceDownloadFailed", "conversion_failed")
        handler.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "source file could not be converted")
        return
    send_source_download_bytes(handler, chunk, data, filename=filename, content_type="application/pdf")
    record_source_download_event(chunk, "SourceDownloaded", "success")


def result_payload(chunk: dict[str, Any]) -> dict[str, str]:
    summary = summarize_text(chunk.get("text", ""), chunk.get("tokens", [])[:12])
    period = chunk.get("effective_from") or "시행일 미상"
    if chunk.get("effective_to"):
        period += f" ~ {chunk.get('effective_to')}"
    source = chunk.get("source_file") or "sample"
    return {
        "title": str(chunk.get("doc_title") or "검색 결과"),
        "section": str(chunk.get("section_title") or "본문"),
        "permission": PERMISSION_LABEL.get(chunk.get("permission"), str(chunk.get("permission"))),
        "period": period,
        "source": source,
        "summary": summary,
        "text": str(chunk.get("text") or ""),
    }


def register_pdf_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_candidates = [
        "/Users/leeseungbo/Library/Fonts/NanumSquareNeo-Variable.ttf",
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
        "/System/Library/Fonts/Supplemental/NotoSansGothic-Regular.ttf",
    ]
    for font_path in font_candidates:
        if Path(font_path).exists():
            try:
                pdfmetrics.registerFont(TTFont("RegRagKorean", font_path))
                return "RegRagKorean"
            except Exception:
                continue
    return "Helvetica"


def make_result_pdf_with_bundled_python(chunk: dict[str, Any]) -> bytes:
    if os.environ.get("REG_RAG_PDF_CHILD") == "1" or not BUNDLED_PYTHON.exists():
        raise ModuleNotFoundError("reportlab")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        input_path = tmp_path / "chunk.json"
        output_path = tmp_path / "result.pdf"
        input_path.write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")
        script = """
import json
import os
import sys
from pathlib import Path

os.environ["REG_RAG_PDF_CHILD"] = "1"
import server

chunk = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
Path(sys.argv[2]).write_bytes(server.make_result_pdf(chunk))
"""
        proc = subprocess.run(
            [str(BUNDLED_PYTHON), "-c", script, str(input_path), str(output_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if proc.returncode != 0 or not output_path.exists():
            detail = (proc.stderr or proc.stdout or "PDF generator failed").strip()
            raise RuntimeError(f"PDF 생성 실패: {detail[:500]}")
        return output_path.read_bytes()


def make_result_pdf(chunk: dict[str, Any]) -> bytes:
    try:
        return make_result_pdf_reportlab(chunk)
    except ImportError:
        return make_result_pdf_with_bundled_python(chunk)


def make_source_pdf_with_bundled_python(chunk: dict[str, Any]) -> bytes:
    if os.environ.get("REG_RAG_PDF_CHILD") == "1" or not BUNDLED_PYTHON.exists():
        raise ModuleNotFoundError("reportlab")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        input_path = tmp_path / "chunk.json"
        output_path = tmp_path / "source.pdf"
        input_path.write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")
        script = """
import json
import os
import sys
from pathlib import Path

os.environ["REG_RAG_PDF_CHILD"] = "1"
import server

chunk = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
Path(sys.argv[2]).write_bytes(server.make_source_pdf(chunk))
"""
        proc = subprocess.run(
            [str(BUNDLED_PYTHON), "-c", script, str(input_path), str(output_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0 or not output_path.exists():
            detail = (proc.stderr or proc.stdout or "PDF generator failed").strip()
            raise RuntimeError(f"원본 PDF 생성 실패: {detail[:500]}")
        return output_path.read_bytes()


def make_source_pdf(chunk: dict[str, Any]) -> bytes:
    try:
        return make_source_pdf_reportlab(chunk)
    except ImportError:
        return make_source_pdf_with_bundled_python(chunk)


def make_source_pdf_reportlab(chunk: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    chunks = source_chunks(chunk)
    source_path = Path(str(chunk.get("source_path") or "source"))
    title = source_path.stem if source_path.name else str(chunk.get("doc_title") or "원본문서")
    font_name = register_pdf_font()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "SourceTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=16,
        leading=22,
        alignment=0,
        textColor=colors.HexColor("#202321"),
    )
    meta_style = ParagraphStyle(
        "SourceMeta",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9,
        leading=14,
        textColor=colors.HexColor("#68726b"),
    )
    section_style = ParagraphStyle(
        "SourceSection",
        parent=styles["Heading3"],
        fontName=font_name,
        fontSize=11,
        leading=16,
        spaceBefore=8,
        spaceAfter=4,
        textColor=colors.HexColor("#202321"),
    )
    body_style = ParagraphStyle(
        "SourceBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9.5,
        leading=15,
        spaceAfter=7,
    )
    story = [
        Paragraph(html.escape(unicodedata.normalize("NFC", title)), title_style),
        Paragraph(
            html.escape(unicodedata.normalize("NFC", f"원본 파일: {source_path.name or chunk.get('source_file', 'source')}")),
            meta_style,
        ),
        Spacer(1, 8),
    ]
    for item in chunks:
        section = str(item.get("section_title") or "본문")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        story.append(Paragraph(html.escape(unicodedata.normalize("NFC", section)), section_style))
        story.append(Paragraph(html.escape(unicodedata.normalize("NFC", text)).replace("\n", "<br/>"), body_style))
    doc.build(story)
    return buffer.getvalue()


def make_result_pdf_reportlab(chunk: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    payload = result_payload(chunk)
    font_name = register_pdf_font()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "RegTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=17,
        leading=23,
        alignment=0,
        textColor=colors.HexColor("#202321"),
    )
    meta_style = ParagraphStyle(
        "RegMeta",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9,
        leading=14,
        textColor=colors.HexColor("#68726b"),
    )
    body_style = ParagraphStyle(
        "RegBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=17,
        spaceAfter=8,
    )
    story = [
        Paragraph(html.escape(payload["title"]), title_style),
        Paragraph(html.escape(payload["section"]), body_style),
        Paragraph(
            html.escape(f"권한: {payload['permission']} | 시행: {payload['period']} | 출처: {payload['source']}"),
            meta_style,
        ),
        Spacer(1, 8),
        Paragraph("요약", body_style),
        Paragraph(html.escape(payload["summary"]).replace("\n", "<br/>"), body_style),
        Spacer(1, 8),
        Paragraph("원문", body_style),
    ]
    text = payload["text"][:7000]
    for paragraph in re.split(r"\n{2,}", text):
        story.append(Paragraph(html.escape(paragraph).replace("\n", "<br/>"), body_style))
    doc.build(story)
    return buffer.getvalue()


def make_result_hwp_html(chunk: dict[str, Any]) -> bytes:
    payload = result_payload(chunk)
    body = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>{html.escape(payload['title'])}</title>
  <style>
    body {{ font-family: "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; line-height: 1.7; padding: 28px; color: #202321; }}
    h1 {{ font-size: 22px; }}
    h2 {{ font-size: 16px; margin-top: 24px; }}
    .meta {{ color: #68726b; border-top: 1px solid #d7dbd2; border-bottom: 1px solid #d7dbd2; padding: 10px 0; }}
    pre {{ white-space: pre-wrap; font-family: inherit; }}
  </style>
</head>
<body>
  <h1>{html.escape(payload['title'])}</h1>
  <h2>{html.escape(payload['section'])}</h2>
  <p class="meta">권한: {html.escape(payload['permission'])} | 시행: {html.escape(payload['period'])} | 출처: {html.escape(payload['source'])}</p>
  <h2>요약</h2>
  <pre>{html.escape(payload['summary'])}</pre>
  <h2>원문</h2>
  <pre>{html.escape(payload['text'][:12000])}</pre>
</body>
</html>
"""
    return body.encode("utf-8")


def operation_error(message: str) -> dict[str, str]:
    return {"error": message}


def redact_source_paths(value: Any) -> Any:
    if isinstance(value, list):
        return [redact_source_paths(item) for item in value]
    if not isinstance(value, dict):
        return value

    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if key == "source_path":
            if item and not value.get("source_file"):
                redacted["source_file"] = Path(str(item)).name
            continue
        redacted[key] = redact_source_paths(item)
    return redacted


def public_version(version: dict[str, Any]) -> dict[str, Any]:
    return redact_source_paths(version)


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    return redact_source_paths(event)


def public_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    return {k: redact_source_paths(v) for k, v in chunk.items() if k not in {"tokens", "source_path"}}


def dashboard_payload() -> dict[str, Any]:
    with DATA_LOCK:
        return dashboard_payload_snapshot()


def dashboard_payload_snapshot() -> dict[str, Any]:
    versions = list(REGISTRY.state.get("versions", {}).values())
    current_count = len(REGISTRY.versions(None, include_history=False))
    pending_count = sum(1 for version in versions if version.get("status") in {"detected", "pending"})
    error_count = sum(1 for version in versions if version.get("status") == "scan_error")
    scan_runs = REGISTRY.state.get("scan_runs", [])
    return {
        "total_regulations": len(REGISTRY.state.get("regulations", {})),
        "current_count": current_count,
        "pending_count": pending_count,
        "error_count": error_count,
        "last_scan": redact_source_paths(scan_runs[-1]) if scan_runs else None,
        "offline": True,
        "auto_ingest": AUTO_INGEST_SERVICE.snapshot(),
    }


def versions_payload(status: str | None = None, regulation_id: str | None = None) -> dict[str, Any]:
    with DATA_LOCK:
        return versions_payload_snapshot(status=status, regulation_id=regulation_id)


def versions_payload_snapshot(
    status: str | None = None, regulation_id: str | None = None
) -> dict[str, Any]:
    versions = []
    for version in REGISTRY.state.get("versions", {}).values():
        if regulation_id and version.get("regulation_id") != regulation_id:
            continue
        version_status = version.get("status")
        if status:
            if status == "pending":
                if version_status not in {"detected", "pending"}:
                    continue
            elif version_status != status:
                continue
        versions.append(public_version(version))
    versions.sort(key=lambda item: (item.get("canonical_title", ""), item.get("effective_from", ""), item["version_id"]))
    return {"versions": versions}


def bounded_limit(raw_value: str | None, default: int = 100, maximum: int = 500) -> int:
    if raw_value is None:
        return default
    try:
        limit = int(raw_value)
    except ValueError:
        raise ValueError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, maximum)


def json_response(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    add_api_cors_headers(handler)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def static_response(handler: BaseHTTPRequestHandler, path: Path) -> None:
    if not path.exists() or not path.is_file():
        handler.send_error(HTTPStatus.NOT_FOUND)
        return
    content_type = "text/plain; charset=utf-8"
    if path.suffix == ".html":
        content_type = "text/html; charset=utf-8"
    elif path.suffix == ".css":
        content_type = "text/css; charset=utf-8"
    elif path.suffix == ".js":
        content_type = "application/javascript; charset=utf-8"
    data = path.read_bytes()
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def static_asset_path(request_path: str) -> Path | None:
    if not request_path.startswith("/static/"):
        return None
    static_root = STATIC_DIR.resolve()
    candidate = (static_root / request_path.removeprefix("/static/")).resolve()
    if candidate == static_root or static_root not in candidate.parents:
        return None
    return candidate


class RegRagHandler(BaseHTTPRequestHandler):
    server_version = "RegRAGPrototype/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        body = json.loads(data.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("JSON object body is required")
        return body

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        add_api_cors_headers(self)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/":
            static_response(self, STATIC_DIR / "index.html")
            return
        if path.startswith("/static/"):
            asset_path = static_asset_path(path)
            if asset_path is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            static_response(self, asset_path)
            return
        if path == "/api/health":
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "chunks": len(load_index().get("chunks", [])),
                    "pdfplumber": pdfplumber is not None,
                },
            )
            return
        if path == "/api/documents":
            json_response(self, HTTPStatus.OK, {"documents": document_summary()})
            return
        if path == "/api/dashboard":
            json_response(self, HTTPStatus.OK, dashboard_payload())
            return
        if path == "/api/versions":
            qs = parse_qs(parsed.query)
            status = (qs.get("status") or [None])[0]
            regulation_id = (qs.get("regulation_id") or [None])[0]
            json_response(self, HTTPStatus.OK, versions_payload(status=status, regulation_id=regulation_id))
            return
        if path == "/api/events":
            qs = parse_qs(parsed.query)
            try:
                limit = bounded_limit((qs.get("limit") or [None])[0])
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, operation_error(str(exc)))
                return
            json_response(self, HTTPStatus.OK, {"events": [public_event(event) for event in REGISTRY.events(limit)]})
            return
        if path == "/api/chunks":
            qs = parse_qs(parsed.query)
            limit = int((qs.get("limit") or ["25"])[0])
            chunks = [public_chunk(chunk) for chunk in load_index().get("chunks", [])[:limit]]
            json_response(self, HTTPStatus.OK, {"chunks": chunks})
            return
        if path == "/api/download/source":
            qs = parse_qs(parsed.query)
            chunk_id_value = (qs.get("id") or [""])[0]
            chunk = get_chunk_by_id(chunk_id_value)
            if not chunk:
                record_missing_source_download_event(chunk_id_value)
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            source_download(self, chunk)
            return
        if path == "/api/download/source-pdf":
            qs = parse_qs(parsed.query)
            chunk_id_value = (qs.get("id") or [""])[0]
            chunk = get_chunk_by_id(chunk_id_value)
            if not chunk:
                record_missing_source_download_event(chunk_id_value)
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            source_pdf_download(self, chunk)
            return
        if path == "/api/download/result":
            qs = parse_qs(parsed.query)
            chunk = get_chunk_by_id((qs.get("id") or [""])[0])
            fmt = (qs.get("format") or ["pdf"])[0].lower()
            if not chunk:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if fmt == "hwp":
                filename = safe_download_name(f"{chunk.get('doc_title', 'result')}_검색결과", ".hwp")
                send_bytes(
                    self,
                    make_result_hwp_html(chunk),
                    filename=filename,
                    content_type="application/x-hwp; charset=utf-8",
                )
                return
            filename = safe_download_name(f"{chunk.get('doc_title', 'result')}_검색결과", ".pdf")
            send_bytes(self, make_result_pdf(chunk), filename=filename, content_type="application/pdf")
            return
        if path.startswith("/api/"):
            json_response(self, HTTPStatus.NOT_FOUND, operation_error("not found"))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in MUTATION_PATHS and not demo_mutations_enabled():
                json_response(self, HTTPStatus.FORBIDDEN, operation_error("demo mutations disabled"))
                return
            if path == "/api/search":
                body = self.read_json_body()
                try:
                    as_of = validate_api_date(body.get("as_of"), "as_of")
                except ValueError as exc:
                    json_response(self, HTTPStatus.BAD_REQUEST, operation_error(str(exc)))
                    return
                result = search_index(
                    query=str(body.get("query", "")),
                    role=str(body.get("role", "employee")),
                    as_of=as_of,
                    limit=int(body.get("limit", 6)),
                    include_history=bool(body.get("include_history", False)),
                )
                payload = search_audit_payload(
                    str(body.get("query", "")),
                    result["role"],
                    result["as_of"],
                    len(result["results"]),
                )
                with DATA_LOCK:
                    REGISTRY.record_event(
                        "SearchExecuted",
                        summary=payload["summary"],
                        metadata=payload["metadata"],
                        actor_role=result["role"],
                        actor_name="search-user",
                        target_type="regulation_search",
                        target_id="search",
                    )
                json_response(self, HTTPStatus.OK, result)
                return
            if path == "/api/versions/approve":
                body = self.read_json_body()
                version_id = body.get("version_id")
                effective_from = body.get("effective_from")
                actor = str(body.get("actor") or "감사팀장(시연)")
                if not version_id:
                    json_response(self, HTTPStatus.BAD_REQUEST, operation_error("version_id is required"))
                    return
                if not effective_from:
                    json_response(self, HTTPStatus.BAD_REQUEST, operation_error("effective_from is required"))
                    return
                try:
                    effective_from = validate_api_date(effective_from, "effective_from")
                    with DATA_LOCK:
                        version = REGISTRY.approve_version(
                            str(version_id), actor, effective_from
                        )
                except KeyError:
                    json_response(self, HTTPStatus.NOT_FOUND, operation_error("version_id not found"))
                    return
                except ValueError as exc:
                    json_response(self, HTTPStatus.BAD_REQUEST, operation_error(str(exc)))
                    return
                json_response(self, HTTPStatus.OK, {"version": public_version(version), "simulation": True})
                return
            if path == "/api/versions/reject":
                body = self.read_json_body()
                version_id = body.get("version_id")
                reason = body.get("reason")
                actor = str(body.get("actor") or "감사팀장(시연)")
                if not version_id:
                    json_response(self, HTTPStatus.BAD_REQUEST, operation_error("version_id is required"))
                    return
                if not reason:
                    json_response(self, HTTPStatus.BAD_REQUEST, operation_error("reason is required"))
                    return
                try:
                    with DATA_LOCK:
                        version = REGISTRY.reject_version(
                            str(version_id), actor, str(reason)
                        )
                except KeyError:
                    json_response(self, HTTPStatus.NOT_FOUND, operation_error("version_id not found"))
                    return
                except ValueError as exc:
                    json_response(self, HTTPStatus.BAD_REQUEST, operation_error(str(exc)))
                    return
                json_response(self, HTTPStatus.OK, {"version": public_version(version), "simulation": True})
                return
            if path == "/api/upload":
                body = self.read_json_body()
                filename = str(body.get("filename", "upload.pdf"))
                content_b64 = str(body.get("content_base64", ""))
                if not content_b64:
                    json_response(self, HTTPStatus.BAD_REQUEST, {"error": "content_base64 is required"})
                    return
                result = ingest_uploaded_file(filename, base64.b64decode(content_b64))
                json_response(self, HTTPStatus.OK, redact_source_paths(result))
                return
            if path == "/api/ingest-local":
                try:
                    result = AUTO_INGEST_SERVICE.run_once("manual")
                except IngestAlreadyRunning as exc:
                    json_response(
                        self,
                        HTTPStatus.CONFLICT,
                        {
                            "error": str(exc),
                            "auto_ingest": AUTO_INGEST_SERVICE.snapshot(),
                        },
                    )
                    return
                json_response(self, HTTPStatus.OK, redact_source_paths(result))
                return
            if path == "/api/reset":
                with DATA_LOCK:
                    seed_index(force=True)
                    clear_uploads()
                    REGISTRY.reset()
                    documents = document_summary()
                json_response(self, HTTPStatus.OK, {"documents": documents})
                return
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
        except json.JSONDecodeError:
            json_response(self, HTTPStatus.BAD_REQUEST, operation_error("invalid JSON body"))
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, operation_error(str(exc)))
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


def main() -> None:
    global AUTO_INGEST_SERVICE

    parser = argparse.ArgumentParser(description="Internal regulation RAG prototype")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--reset", action="store_true", help="reset sample index before serving")
    parser.add_argument("--ingest-local", action="store_true", help="index local regulation source folders before serving")
    args = parser.parse_args()

    auto_enabled = os.environ.get("REG_RAG_AUTO_INGEST") == "1"
    interval_seconds = (
        parse_auto_ingest_interval(os.environ.get("REG_RAG_AUTO_INGEST_INTERVAL_SECONDS"))
        if auto_enabled
        else 60
    )
    AUTO_INGEST_SERVICE = build_auto_ingest_service(
        enabled=auto_enabled,
        interval_seconds=interval_seconds,
    )

    seed_index(force=args.reset)
    if args.ingest_local or auto_enabled:
        result = AUTO_INGEST_SERVICE.run_once("startup")
        print(
            "Local ingest: "
            f"{result['imported_chunks']} chunks, "
            f"{len(result['documents'])} documents, "
            f"{len(result['errors'])} errors"
        )
    server = ThreadingHTTPServer((args.host, args.port), RegRagHandler)
    AUTO_INGEST_SERVICE.start()
    print(f"RegRAG prototype running at http://{args.host}:{args.port}")
    print(f"Workspace: {WORKSPACE_DIR}")
    if auto_enabled:
        print(f"Automatic regulation refresh every {interval_seconds}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        AUTO_INGEST_SERVICE.stop()
        server.server_close()


if __name__ == "__main__":
    main()
