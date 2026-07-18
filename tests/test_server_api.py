import base64
import json
import tempfile
import time
import unittest
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import server
from regulation_registry import RegulationRegistry


class IsolatedServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.index_file = self.data_dir / "index.json"
        self.registry = RegulationRegistry(self.data_dir / "registry.json")
        self.uploads_dir = Path(self.tmp.name) / "uploads"
        self.uploads_dir.mkdir()
        self.patches = [
            mock.patch.object(server, "DATA_DIR", self.data_dir),
            mock.patch.object(server, "INDEX_FILE", self.index_file),
            mock.patch.object(server, "REGISTRY", self.registry),
            mock.patch.object(server, "UPLOADS_DIR", self.uploads_dir),
            mock.patch.object(server, "configured_source_roots", lambda: [Path(self.tmp.name).resolve()]),
            mock.patch.object(server, "demo_mutations_enabled", lambda: True),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def write_chunks(self, chunks):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(json.dumps({"version": 1, "chunks": chunks}), encoding="utf-8")


class AutoIngestConfigurationTest(unittest.TestCase):
    def test_interval_defaults_to_sixty_seconds(self):
        self.assertEqual(server.parse_auto_ingest_interval(None), 60)

    def test_interval_rejects_invalid_and_too_small_values(self):
        for value in ("bad", "0", "9"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    server.parse_auto_ingest_interval(value)


class FakeAutoIngestService:
    def __init__(self, snapshot, result=None, error=None):
        self._snapshot = snapshot
        self._result = result or {}
        self._error = error

    def snapshot(self):
        return dict(self._snapshot)

    def run_once(self, trigger):
        if self._error:
            raise self._error
        return dict(self._result)


class SearchVersionFilterTest(IsolatedServerTest):
    def test_search_audit_event_does_not_store_full_query(self):
        query = "개인정보가 포함될 수 있는 긴 질의"

        event = server.search_audit_payload(query, "employee", "2026-07-12", 3)

        self.assertNotIn(query, event["summary"])
        self.assertNotIn(query, json.dumps(event["metadata"], ensure_ascii=False))
        self.assertEqual(event["metadata"]["query_length"], 19)
        self.assertEqual(event["metadata"]["result_count"], 3)

    def test_search_uses_only_version_ids_allowed_by_registry(self):
        chunks = [
            server.make_chunk(
                doc_title="인사규정",
                section_title="구버전",
                text="징계 구 기준",
                effective_from="2025-01-01",
                source_type="hwp",
            ),
            server.make_chunk(
                doc_title="인사규정",
                section_title="최신본",
                text="징계 최신 기준",
                effective_from="2026-05-27",
                source_type="hwp",
            ),
        ]
        chunks[0]["version_id"] = "old"
        chunks[1]["version_id"] = "new"

        result = server.search_chunks(
            chunks,
            "징계 기준",
            "employee",
            "2026-07-12",
            6,
            allowed_version_ids={"new"},
        )

        self.assertEqual([item["version_id"] for item in result["results"]], ["new"])

    def test_search_results_use_opaque_download_links_without_source_paths(self):
        private_path = "/srv/cheonan/regulations/인사규정.hwpx"
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="징계",
            text="징계 처리 기준",
            source_file="인사규정.hwpx",
            source_type="hwpx",
            source_path=private_path,
        )

        result = server.search_chunks([chunk], "징계", "employee", "2026-07-12", 6)

        item = result["results"][0]
        self.assertNotIn("source_path", item)
        self.assertNotIn(private_path, json.dumps(result, ensure_ascii=False))
        self.assertEqual(item["download"]["source"], f"/api/download/source?id={chunk['id']}")
        self.assertEqual(item["download"]["source_pdf"], f"/api/download/source-pdf?id={chunk['id']}")

    def test_search_index_uses_latest_approved_version_by_default(self):
        old = self.registry.record_detection("인사규정", "/closed/old.hwp", "old-hash", "2025-01-01", ["old-c"])
        self.registry.approve_version(old["version_id"], "감사팀장", "2025-01-01", today="2026-07-12")
        new = self.registry.record_detection("인사규정", "/closed/new.hwp", "new-hash", "2026-05-27", ["new-c"])
        self.registry.approve_version(new["version_id"], "감사팀장", "2026-05-27", today="2026-07-12")
        chunks = [
            server.make_chunk(
                doc_title="인사규정",
                section_title="구버전",
                text="징계 구 기준",
                effective_from="2025-01-01",
                source_type="hwp",
            ),
            server.make_chunk(
                doc_title="인사규정",
                section_title="최신본",
                text="징계 최신 기준",
                effective_from="2026-05-27",
                source_type="hwp",
            ),
        ]
        chunks[0]["version_id"] = old["version_id"]
        chunks[1]["version_id"] = new["version_id"]
        self.write_chunks(chunks)

        result = server.search_index("징계 기준", "employee", "2026-07-12")

        self.assertEqual([item["version_id"] for item in result["results"]], [new["version_id"]])

    def test_search_history_includes_only_matching_approved_windows(self):
        old = self.registry.record_detection("인사규정", "/closed/old.hwp", "old-hash", "2025-01-01", ["old-c"])
        self.registry.approve_version(old["version_id"], "감사팀장", "2025-01-01", today="2026-07-12")
        new = self.registry.record_detection("인사규정", "/closed/new.hwp", "new-hash", "2026-05-27", ["new-c"])
        self.registry.approve_version(new["version_id"], "감사팀장", "2026-05-27", today="2026-07-12")
        rejected = self.registry.record_detection("인사규정", "/closed/rejected.hwp", "reject-hash", "2024-01-01", ["rej-c"])
        self.registry.reject_version(rejected["version_id"], "감사팀장", "duplicate")
        chunks = []
        for label, version in (("구버전", old), ("최신본", new), ("반려", rejected)):
            chunk = server.make_chunk(
                doc_title="인사규정",
                section_title=label,
                text=f"징계 {label} 기준",
                effective_from=version["effective_from"],
                source_type="hwp",
            )
            chunk["version_id"] = version["version_id"]
            chunks.append(chunk)
        self.write_chunks(chunks)

        result = server.search_index("징계 기준", "employee", "2025-06-01", include_history=True)

        self.assertEqual([item["version_id"] for item in result["results"]], [old["version_id"]])

    def test_search_history_returns_complete_newest_first_version_timeline_with_downloads(self):
        old_source = Path(self.tmp.name) / "18. 인사규정(개정 2025.5.27.).hwp"
        new_source = Path(self.tmp.name) / "18. 인사규정(개정 2026.4.13.).hwp"
        old_source.write_bytes(b"old source")
        new_source.write_bytes(b"new source")
        chunks = [
            server.make_chunk(
                doc_title="18. 인사규정",
                section_title="구버전",
                text="징계 구 기준",
                effective_from="2025-05-27",
                source_file=old_source.name,
                source_type="hwp",
                source_path=str(old_source),
            ),
            server.make_chunk(
                doc_title="18. 인사규정",
                section_title="최신본",
                text="징계 최신 기준",
                effective_from="2026-04-13",
                source_file=new_source.name,
                source_type="hwp",
                source_path=str(new_source),
            ),
        ]
        old = self.registry.record_detection(
            "18. 인사규정", str(old_source), "old-timeline-hash", "2025-05-27", [chunks[0]["id"]]
        )
        self.registry.approve_version(old["version_id"], "감사팀장", "2025-05-27", today="2026-07-12")
        new = self.registry.record_detection(
            "18. 인사규정", str(new_source), "new-timeline-hash", "2026-04-13", [chunks[1]["id"]]
        )
        self.registry.approve_version(new["version_id"], "감사팀장", "2026-04-13", today="2026-07-12")
        chunks[0]["version_id"] = old["version_id"]
        chunks[1]["version_id"] = new["version_id"]
        self.write_chunks(chunks)

        result = server.search_index("징계 기준", "employee", "2025-06-01", include_history=True)

        self.assertEqual([item["version_id"] for item in result["results"]], [old["version_id"]])
        self.assertEqual(len(result["timelines"]), 1)
        timeline = result["timelines"][0]
        self.assertEqual(timeline["regulation_id"], old["regulation_id"])
        self.assertEqual(timeline["canonical_title"], "18. 인사규정")
        self.assertEqual(
            [version["version_id"] for version in timeline["versions"]],
            [new["version_id"], old["version_id"]],
        )
        self.assertEqual(
            [version["status"] for version in timeline["versions"]],
            ["approved", "superseded"],
        )
        for version in timeline["versions"]:
            self.assertRegex(version["download"]["source"], r"^/api/download/source\?id=")
            self.assertRegex(version["download"]["source_pdf"], r"^/api/download/source-pdf\?id=")
            self.assertNotIn("source_path", version)
        self.assertNotIn(str(Path(self.tmp.name)), json.dumps(result, ensure_ascii=False))

    def test_search_history_hydrates_stale_regulation_id_from_migrated_version(self):
        legacy_state = {
            "schema_version": 1,
            "regulations": {
                "old-reg": {
                    "regulation_id": "old-reg",
                    "canonical_title": "18. 인사규정(개정 2025.5.27.)",
                    "category": None,
                    "versions": ["old-version"],
                },
                "new-reg": {
                    "regulation_id": "new-reg",
                    "canonical_title": "18. 인사규정(개정 2026.4.13.)",
                    "category": None,
                    "versions": ["new-version"],
                },
            },
            "versions": {
                "new-version": {
                    "version_id": "new-version",
                    "regulation_id": "new-reg",
                    "canonical_title": "18. 인사규정(개정 2026.4.13.)",
                    "source_path": "/closed/new.hwp",
                    "content_hash": "new",
                    "effective_from": "2026-04-13",
                    "effective_to": None,
                    "chunk_ids": ["new-chunk"],
                    "category": None,
                    "change_type": "revision",
                    "status": "approved",
                },
                "old-version": {
                    "version_id": "old-version",
                    "regulation_id": "old-reg",
                    "canonical_title": "18. 인사규정(개정 2025.5.27.)",
                    "source_path": "/closed/old.hwp",
                    "content_hash": "old",
                    "effective_from": "2025-05-27",
                    "effective_to": None,
                    "chunk_ids": ["old-chunk"],
                    "category": None,
                    "change_type": "revision",
                    "status": "approved",
                },
            },
            "scan_runs": [],
            "events": [],
        }
        registry_path = self.data_dir / "legacy-registry.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps(legacy_state, ensure_ascii=False), encoding="utf-8")
        migrated_registry = RegulationRegistry(registry_path)
        old_chunk = server.make_chunk(
            doc_title="18. 인사규정(개정 2025.5.27.)",
            section_title="구버전",
            text="징계 구 기준",
            effective_from="2025-05-27",
            source_type="hwp",
            source_path="/closed/old.hwp",
        )
        old_chunk.update(
            {
                "id": "old-chunk",
                "version_id": "old-version",
                "regulation_id": "old-reg",
            }
        )
        self.write_chunks([old_chunk])

        with mock.patch.object(server, "REGISTRY", migrated_registry):
            result = server.search_index("징계 구 기준", "employee", "2026-07-12", include_history=True)

        self.assertEqual(result["results"], [])
        self.assertEqual(len(result["timelines"]), 1)
        self.assertEqual(result["timelines"][0]["regulation_id"], "new-reg")
        self.assertEqual(result["timelines"][0]["canonical_title"], "18. 인사규정")

    def test_search_hydrates_current_registry_status_over_stale_chunk_metadata(self):
        source = Path(self.tmp.name) / "감사규정_2026.05.27.hwp"
        source.write_bytes(b"approved source")
        scan = self.registry.scan_sources(
            [source],
            lambda path: [
                server.make_chunk(
                    doc_title="감사규정",
                    section_title="본문",
                    text="감사 자료 제출 기준",
                    effective_from="2026-05-27",
                    source_file=path.name,
                    source_type="hwp",
                    source_path=str(path),
                )
            ],
        )
        chunk = scan["chunks"][0]
        self.assertEqual(chunk["version_status"], "pending")
        self.registry.approve_version(chunk["version_id"], "감사팀장", "2026-05-27", today="2026-07-12")
        self.write_chunks([chunk])

        result = server.search_index("자료 제출", "audit", "2026-07-12")

        self.assertEqual(result["results"][0]["version_status"], "approved")

    def test_search_answer_uses_hydrated_registry_date_and_status(self):
        version = self.registry.record_detection(
            "감사규정", "/closed/audit.hwp", "answer-hash", "2026-05-27", ["audit-chunk"]
        )
        self.registry.approve_version(
            version["version_id"], "감사팀장", "2026-05-27", today="2026-07-12"
        )
        chunk = server.make_chunk(
            doc_title="감사규정",
            section_title="자료 제출",
            text="감사 자료 제출 기준",
            effective_from="1999-01-01",
            source_type="hwp",
        )
        chunk.update(
            {
                "id": "audit-chunk",
                "version_id": version["version_id"],
                "version_status": "pending",
            }
        )
        self.write_chunks([chunk])

        result = server.search_index("자료 제출", "audit", "2026-07-12")

        self.assertEqual(result["results"][0]["effective_from"], "2026-05-27")
        self.assertEqual(result["results"][0]["version_status"], "approved")
        self.assertIn("2026-05-27", result["answer"])
        self.assertIn("승인", result["answer"])
        self.assertNotIn("1999-01-01", result["answer"])

    def test_timeline_versions_expose_only_ui_required_fields(self):
        version = self.registry.record_detection(
            "인사규정", "/closed/personnel.hwp", "private-hash", "2026-05-27", ["personnel-chunk"]
        )
        self.registry.approve_version(
            version["version_id"], "감사팀장", "2026-05-27", today="2026-07-12"
        )
        self.registry.state["versions"][version["version_id"]].update(
            {"indexed": True, "indexed_at": "2026-07-12T00:00:00+00:00"}
        )
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="징계",
            text="징계 기준",
            source_file="personnel.hwp",
            source_type="hwp",
            source_path="/closed/personnel.hwp",
        )
        chunk.update({"id": "personnel-chunk", "version_id": version["version_id"]})

        timelines = server.build_version_timelines(
            {version["regulation_id"]}, [chunk], "employee"
        )

        self.assertEqual(
            set(timelines[0]["versions"][0]),
            {
                "version_id",
                "effective_from",
                "effective_to",
                "status",
                "change_type",
                "source_file",
                "download",
            },
        )

    def test_latest_only_search_returns_no_timeline_payload(self):
        self.write_chunks([])

        result = server.search_index("징계", "employee", "2026-07-12", include_history=False)

        self.assertEqual(result["timelines"], [])

    def test_search_history_without_as_of_excludes_2027_scheduled_version_today(self):
        current = self.registry.record_detection("인사규정", "/closed/current.hwp", "current-hash", "2026-01-01", ["cur-c"])
        self.registry.approve_version(current["version_id"], "감사팀장", "2026-01-01", today="2026-07-12")
        future = self.registry.record_detection("인사규정", "/closed/future.hwp", "future-hash", "2027-01-01", ["future-c"])
        self.registry.approve_version(future["version_id"], "감사팀장", "2027-01-01", today="2026-07-12")
        chunks = []
        for label, version in (("현재", current), ("예정", future)):
            chunk = server.make_chunk(
                doc_title="인사규정",
                section_title=label,
                text=f"징계 {label} 기준",
                effective_from=version["effective_from"],
                source_type="hwp",
            )
            chunk["version_id"] = version["version_id"]
            chunks.append(chunk)
        self.write_chunks(chunks)

        result = server.search_index("징계 기준", "employee", None, include_history=True)

        self.assertEqual(result["as_of"], server.business_today_iso())
        self.assertEqual([item["version_id"] for item in result["results"]], [current["version_id"]])

    def test_search_index_uses_query_detected_basis_date_for_version_filter(self):
        old = self.registry.record_detection("인사규정", "/closed/old.hwp", "old-query-date-hash", "2025-01-01", ["old-c"])
        self.registry.approve_version(old["version_id"], "감사팀장", "2025-01-01", today="2026-07-12")
        current = self.registry.record_detection(
            "인사규정", "/closed/current.hwp", "current-query-date-hash", "2026-05-27", ["current-c"]
        )
        self.registry.approve_version(current["version_id"], "감사팀장", "2026-05-27", today="2026-07-12")
        chunks = []
        for label, version in (("구버전", old), ("최신본", current)):
            chunk = server.make_chunk(
                doc_title="인사규정",
                section_title=label,
                text=f"징계 {label} 기준",
                effective_from=version["effective_from"],
                effective_to=version["effective_to"],
                source_type="hwp",
            )
            chunk["version_id"] = version["version_id"]
            chunks.append(chunk)
        self.write_chunks(chunks)

        result = server.search_index("2025년 6월 기준 징계 기준", "employee", None)

        self.assertEqual(result["as_of"], "2025-06-30")
        self.assertEqual([item["version_id"] for item in result["results"]], [old["version_id"]])

    def test_unversioned_sample_chunks_remain_searchable_before_first_approval(self):
        chunks = [
            server.make_chunk(
                doc_title="샘플규정",
                section_title="기본",
                text="징계 샘플 기준",
                effective_from="2023-01-01",
            )
        ]
        self.write_chunks(chunks)

        result = server.search_index("징계 기준", "employee", "2024-01-01")

        self.assertEqual([item["doc_title"] for item in result["results"]], ["샘플규정"])

    def test_add_chunks_upserts_stable_ids_without_retry_duplicates(self):
        self.write_chunks([])
        original = server.make_chunk(
            doc_title="인사규정",
            section_title="징계",
            text="최초 색인 본문",
        )
        original["id"] = "stable-chunk"
        retried = {**original, "text": "재시도로 갱신된 본문"}

        first_count = server.add_chunks([original])
        retry_count = server.add_chunks([retried])

        indexed = server.load_index()["chunks"]
        self.assertEqual(first_count, 1)
        self.assertEqual(retry_count, 1)
        self.assertEqual(len(indexed), 1)
        self.assertEqual(indexed[0]["id"], "stable-chunk")
        self.assertEqual(indexed[0]["text"], "재시도로 갱신된 본문")

    def test_index_ack_failure_retry_replaces_random_ids_for_the_same_version(self):
        source = Path(self.tmp.name) / "인사규정_2026.05.27.hwp"
        source.write_bytes(b"same regulation")
        self.write_chunks([])

        def parse_with_new_chunk_id(path):
            return [
                server.make_chunk(
                    doc_title="인사규정",
                    section_title="징계",
                    text="징계 기준",
                    source_file=path.name,
                    source_type="hwp",
                    source_path=str(path),
                )
            ]

        with mock.patch.object(server, "ingest_file", side_effect=parse_with_new_chunk_id), mock.patch.object(
            self.registry, "mark_versions_indexed", side_effect=RuntimeError("ack write failed")
        ):
            with self.assertRaises(RuntimeError):
                server.ingest_registered_sources([source])

        first_chunks = server.load_index()["chunks"]
        first_chunk_id = first_chunks[0]["id"]
        version_id = first_chunks[0]["version_id"]

        with mock.patch.object(server, "ingest_file", side_effect=parse_with_new_chunk_id):
            server.ingest_registered_sources([source])

        retried_chunks = server.load_index()["chunks"]
        self.assertEqual(len(retried_chunks), 1)
        self.assertNotEqual(retried_chunks[0]["id"], first_chunk_id)
        self.assertEqual(retried_chunks[0]["version_id"], version_id)
        self.assertTrue(self.registry.state["versions"][version_id]["indexed"])

    def test_automatic_scan_keeps_pending_replacement_out_of_latest_search(self):
        current = self.registry.record_detection(
            "인사규정",
            "/closed/인사규정_2025.hwp",
            "old-hash",
            "2025-01-01",
            ["old-chunk"],
        )
        self.registry.approve_version(current["version_id"], "감사팀장", "2025-01-01")
        current_chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="징계",
            text="기존 징계 기준",
            source_file="인사규정_2025.hwp",
            source_type="hwp",
            effective_from="2025-01-01",
        )
        current_chunk.update(
            version_id=current["version_id"],
            regulation_id=current["regulation_id"],
            version_status="approved",
        )
        self.write_chunks([current_chunk])

        replacement_path = Path(self.tmp.name) / "인사규정_2026.07.17.hwp"
        replacement_path.write_bytes(b"changed-regulation")
        replacement_chunks = [
            server.make_chunk(
                doc_title="인사규정",
                section_title="징계",
                text="새 징계 기준",
                source_file=replacement_path.name,
                source_type="hwp",
                source_path=str(replacement_path),
            )
        ]

        service = server.build_auto_ingest_service(enabled=False, interval_seconds=60)
        with mock.patch.object(server, "AUTO_INGEST_SERVICE", service), mock.patch.object(
            server, "local_sources", return_value=[replacement_path]
        ), mock.patch.object(server, "ingest_file", return_value=replacement_chunks):
            service.run_once("automatic")

        before_approval = server.search_index("징계 기준", "employee", "2026-07-17")
        self.assertEqual(
            [item["text"] for item in before_approval["results"]], ["기존 징계 기준"]
        )

        pending = next(
            version
            for version in self.registry.state["versions"].values()
            if version["status"] == "pending"
        )
        self.registry.approve_version(pending["version_id"], "감사팀장", "2026-07-17")
        after_approval = server.search_index("징계 기준", "employee", "2026-07-17")
        self.assertEqual(after_approval["results"][0]["text"], "새 징계 기준")
        self.assertEqual(
            after_approval["results"][0]["download"]["source"],
            f"/api/download/source?id={replacement_chunks[0]['id']}",
        )


class ApiRoutesTest(IsolatedServerTest):
    def setUp(self):
        super().setUp()
        self.log_patcher = mock.patch.object(server.RegRagHandler, "log_message", lambda *args: None)
        self.log_patcher.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.RegRagHandler)
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        self.log_patcher.stop()
        super().tearDown()

    def get_json(self, path, query=None):
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        try:
            with urlopen(url, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def raw_get(self, path):
        connection = HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def post_json(self, path, body):
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post_raw_json(self, path, raw_json):
        request = Request(
            self.base_url + path,
            data=raw_json.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def wait_for_event(self, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.registry.events(1)
            if events:
                return events[0]
            time.sleep(0.01)
        self.fail("audit event was not recorded before timeout")

    def test_dashboard_versions_and_events_return_registry_state(self):
        pending = self.registry.record_detection("인사규정", "/closed/new.hwp", "hash", "2026-05-27", ["c"])
        self.write_chunks([])

        dashboard_status, dashboard = self.get_json("/api/dashboard")
        versions_status, versions = self.get_json("/api/versions", {"status": "pending"})
        events_status, events = self.get_json("/api/events", {"limit": "1"})

        self.assertEqual(dashboard_status, 200)
        self.assertEqual(dashboard["total_regulations"], 1)
        self.assertEqual(dashboard["pending_count"], 1)
        self.assertEqual(dashboard["offline"], True)
        self.assertEqual(versions_status, 200)
        self.assertEqual([item["version_id"] for item in versions["versions"]], [pending["version_id"]])
        self.assertEqual(events_status, 200)
        self.assertEqual(len(events["events"]), 1)

    def test_dashboard_exposes_auto_ingest_status(self):
        fake = FakeAutoIngestService(
            {
                "enabled": True,
                "running": False,
                "interval_seconds": 60,
                "next_run_at": "2026-07-17T01:01:00+00:00",
                "last_result": {
                    "new_count": 1,
                    "changed_count": 0,
                    "error_count": 0,
                },
                "last_error": None,
            }
        )

        with mock.patch.object(server, "AUTO_INGEST_SERVICE", fake):
            status, payload = self.get_json("/api/dashboard")

        self.assertEqual(status, 200)
        self.assertEqual(payload["auto_ingest"]["enabled"], True)
        self.assertEqual(payload["auto_ingest"]["interval_seconds"], 60)

    def test_manual_ingest_returns_conflict_while_scan_is_running(self):
        fake = FakeAutoIngestService(
            {"enabled": True, "running": True, "interval_seconds": 60},
            error=server.IngestAlreadyRunning("regulation ingest already running"),
        )

        with mock.patch.object(server, "AUTO_INGEST_SERVICE", fake):
            status, payload = self.post_json("/api/ingest-local", {})

        self.assertEqual(status, 409)
        self.assertIn("already running", payload["error"])
        self.assertEqual(payload["auto_ingest"]["running"], True)

    def test_public_read_apis_do_not_expose_internal_source_paths(self):
        private_path = "/srv/cheonan/regulations/인사규정.hwpx"
        version = self.registry.record_detection("인사규정", private_path, "hash", "2026-05-27", ["c"])
        self.registry.state["scan_runs"].append(
            {
                "scan_run_id": "scan-private",
                "started_at": "2026-07-13T00:00:00+00:00",
                "finished_at": "2026-07-13T00:00:01+00:00",
                "new_count": 1,
                "changed_count": 0,
                "unchanged_count": 0,
                "error_count": 0,
                "sources": [{"source_path": private_path, "version_id": version["version_id"]}],
            }
        )
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="본문",
            text="징계 처리 기준",
            source_file="인사규정.hwpx",
            source_type="hwpx",
            source_path=private_path,
        )
        self.write_chunks([chunk])

        payloads = []
        for path in ("/api/dashboard", "/api/versions", "/api/events", "/api/chunks"):
            status, payload = self.get_json(path)
            self.assertEqual(status, 200)
            payloads.append(payload)

        serialized = json.dumps(payloads, ensure_ascii=False)
        self.assertNotIn("source_path", serialized)
        self.assertNotIn(private_path, serialized)
        self.assertIn("인사규정.hwpx", serialized)

    def test_static_routes_reject_plain_and_encoded_path_traversal(self):
        static_dir = Path(self.tmp.name) / "static"
        static_dir.mkdir()
        (static_dir / "app.js").write_text("console.log('safe')", encoding="utf-8")
        self.write_chunks([{"text": "private regulation", "source_path": "/srv/private/rule.hwp"}])
        (Path(self.tmp.name) / "server.py").write_text("PRIVATE_SERVER_SOURCE", encoding="utf-8")

        with mock.patch.object(server, "STATIC_DIR", static_dir):
            safe_status, safe_body = self.raw_get("/static/app.js")
            blocked = [
                self.raw_get("/static/../data/index.json"),
                self.raw_get("/static/%2e%2e/data/index.json"),
                self.raw_get("/static/../server.py"),
            ]

        self.assertEqual(safe_status, 200)
        self.assertIn(b"safe", safe_body)
        for status, body in blocked:
            self.assertEqual(status, 404)
            self.assertNotIn(b"private regulation", body)
            self.assertNotIn(b"PRIVATE_SERVER_SOURCE", body)

    def test_events_rejects_limit_below_one(self):
        self.registry.record_detection("인사규정", "/closed/one.hwp", "hash-one", "2026-05-27", ["c1"])

        zero_status, zero_body = self.get_json("/api/events", {"limit": "0"})
        negative_status, negative_body = self.get_json("/api/events", {"limit": "-1"})

        self.assertEqual(zero_status, 400)
        self.assertIn("limit", zero_body["error"])
        self.assertEqual(negative_status, 400)
        self.assertIn("limit", negative_body["error"])

    def test_approval_and_rejection_routes_validate_json_errors(self):
        version = self.registry.record_detection("인사규정", "/closed/new.hwp", "hash", "2026-05-27", ["c"])

        missing_status, missing_body = self.post_json("/api/versions/approve", {"version_id": version["version_id"]})
        bad_date_status, bad_date_body = self.post_json(
            "/api/versions/approve",
            {"version_id": version["version_id"], "effective_from": "2026-13-01"},
        )
        ok_status, ok_body = self.post_json(
            "/api/versions/approve",
            {"version_id": version["version_id"], "effective_from": "2026-05-27"},
        )
        invalid_transition_status, invalid_transition_body = self.post_json(
            "/api/versions/reject",
            {"version_id": version["version_id"], "reason": "late"},
        )
        unknown_status, unknown_body = self.post_json(
            "/api/versions/approve",
            {"version_id": "missing", "effective_from": "2026-05-27"},
        )

        self.assertEqual(missing_status, 400)
        self.assertIn("effective_from", missing_body["error"])
        self.assertEqual(bad_date_status, 400)
        self.assertIn("effective_from", bad_date_body["error"])
        self.assertEqual(ok_status, 200)
        self.assertEqual(ok_body["version"]["status"], "approved")
        self.assertEqual(ok_body["simulation"], True)
        self.assertEqual(invalid_transition_status, 400)
        self.assertIn("cannot reject", invalid_transition_body["error"])
        self.assertEqual(unknown_status, 404)
        self.assertIn("version_id", unknown_body["error"])

    def test_mutation_routes_are_disabled_without_explicit_demo_flag(self):
        version = self.registry.record_detection("인사규정", "/closed/new.hwp", "hash", "2026-05-27", ["c"])

        with mock.patch.object(server, "demo_mutations_enabled", return_value=False):
            status, body = self.post_json(
                "/api/versions/approve",
                {"version_id": version["version_id"], "effective_from": "2026-05-27"},
            )

        self.assertEqual(status, 403)
        self.assertIn("disabled", body["error"])

    def test_cors_is_emitted_only_for_explicitly_allowed_origin(self):
        blocked_request = Request(self.base_url + "/api/health", headers={"Origin": "https://evil.example"})
        allowed_request = Request(
            self.base_url + "/api/health",
            headers={
                "Origin": "https://lsb-afk.github.io",
                "Access-Control-Request-Private-Network": "true",
            },
        )

        with mock.patch.object(server, "allowed_cors_origins", return_value={"https://lsb-afk.github.io"}):
            with urlopen(blocked_request, timeout=5) as response:
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
            with urlopen(allowed_request, timeout=5) as response:
                self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "https://lsb-afk.github.io")
                self.assertEqual(response.headers.get("Access-Control-Allow-Private-Network"), "true")

    def test_search_route_validates_malformed_as_of_and_accepts_include_history(self):
        status, body = self.post_json("/api/search", {"query": "징계", "as_of": "2026-99-99"})

        self.assertEqual(status, 400)
        self.assertIn("as_of", body["error"])

    def test_upload_enters_pending_approval_flow_and_is_not_searchable(self):
        def fake_ingest(path):
            return [
                server.make_chunk(
                    doc_title="인사규정",
                    section_title="본문",
                    text="업로드 징계 기준",
                    effective_from="2026-05-27",
                    source_file=path.name,
                    source_type="hwp",
                    source_path=str(path),
                )
            ]

        with mock.patch.object(server, "ingest_file", side_effect=fake_ingest):
            status, body = self.post_json(
                "/api/upload",
                {
                    "filename": "인사규정_2026.05.27.hwp",
                    "content_base64": base64.b64encode(b"uploaded regulation").decode("ascii"),
                },
            )

        search = server.search_index("업로드 징계", "employee", "2026-07-12")
        pending = list(self.registry.state["versions"].values())
        self.assertEqual(status, 200)
        self.assertEqual(body["imported_chunks"], 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending")
        self.assertEqual(pending[0]["canonical_title"], "인사규정")
        self.assertNotIn(
            pending[0]["version_id"],
            {item.get("version_id") for item in search["results"]},
        )
        self.assertNotIn("업로드 징계 기준", json.dumps(search["results"], ensure_ascii=False))

        approval_status, _ = self.post_json(
            "/api/versions/approve",
            {
                "version_id": pending[0]["version_id"],
                "effective_from": "2026-05-27",
                "actor": "감사팀장",
            },
        )
        approved_search = server.search_index("업로드 징계", "employee", "2026-07-12")

        self.assertEqual(approval_status, 200)
        approved_item = next(
            item for item in approved_search["results"] if item.get("version_id") == pending[0]["version_id"]
        )
        self.assertEqual(approved_item["version_status"], "approved")
        self.assertIn("업로드 징계 기준", approved_item["text"])

    def test_chunkless_upload_still_returns_its_pending_version(self):
        with mock.patch.object(server, "ingest_file", return_value=[]):
            status, body = self.post_json(
                "/api/upload",
                {
                    "filename": "빈규정_2026.05.27.hwp",
                    "content_base64": base64.b64encode(b"empty regulation").decode("ascii"),
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["imported_chunks"], 0)
        self.assertEqual(len(body["versions"]), 1)
        self.assertEqual(body["versions"][0]["status"], "pending")
        self.assertEqual(body["versions"][0]["canonical_title"], "빈규정")
        self.assertNotIn("source_path", json.dumps(body, ensure_ascii=False))

    def test_reset_clears_index_uploads_and_registry_history_together(self):
        upload = self.uploads_dir / "temporary.hwp"
        upload.write_bytes(b"temporary")
        self.registry.record_detection(
            "인사규정", str(upload), "reset-hash", "2026-05-27", ["chunk"]
        )
        self.write_chunks([])

        status, body = self.post_json("/api/reset", {})
        dashboard_status, dashboard = self.get_json("/api/dashboard")
        versions_status, versions = self.get_json("/api/versions")
        events_status, events = self.get_json("/api/events")

        self.assertEqual(status, 200)
        self.assertIn("documents", body)
        self.assertFalse(upload.exists())
        self.assertEqual(dashboard_status, 200)
        self.assertEqual(dashboard["total_regulations"], 0)
        self.assertEqual(dashboard["pending_count"], 0)
        self.assertEqual(versions_status, 200)
        self.assertEqual(versions["versions"], [])
        self.assertEqual(events_status, 200)
        self.assertEqual(events["events"], [])

    def test_successful_search_records_private_audit_event(self):
        chunks = [
            server.make_chunk(
                doc_title="인사규정",
                section_title="본문",
                text="개인정보 감사 기준",
                effective_from="2026-01-01",
            )
        ]
        self.write_chunks(chunks)

        status, body = self.post_json(
            "/api/search",
            {"query": "개인정보가 포함될 수 있는 긴 질의", "role": "audit", "as_of": "2026-07-12"},
        )

        self.assertEqual(status, 200)
        self.assertIn("results", body)
        event = self.wait_for_event()
        event_json = json.dumps(event, ensure_ascii=False)
        self.assertEqual(event["event_type"], "SearchExecuted")
        self.assertNotIn("개인정보가 포함될 수 있는 긴 질의", event_json)
        self.assertEqual(event["metadata"]["query_length"], 19)
        self.assertEqual(event["metadata"]["role"], "audit")
        self.assertEqual(event["metadata"]["as_of"], "2026-07-12")
        self.assertEqual(event["metadata"]["result_count"], len(body["results"]))

    def test_source_download_records_success_without_document_content(self):
        source = Path(self.tmp.name) / "인사규정_2026.01.01.hwp"
        source.write_bytes(b"private source bytes")
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="본문",
            text="문서 본문은 감사 이벤트에 남기지 않는다",
            source_file=source.name,
            source_type="hwp",
            source_path=str(source),
        )
        chunk["version_id"] = "version-success"
        self.write_chunks([chunk])

        with urlopen(f"{self.base_url}/api/download/source?id={chunk['id']}", timeout=5) as response:
            body = response.read()

        self.assertEqual(body, b"private source bytes")
        event = self.wait_for_event()
        event_json = json.dumps(event, ensure_ascii=False)
        self.assertEqual(event["event_type"], "SourceDownloaded")
        self.assertEqual(event["metadata"]["source_file"], source.name)
        self.assertEqual(event["metadata"]["version_id"], "version-success")
        self.assertEqual(event["metadata"]["outcome"], "success")
        self.assertNotIn("문서 본문", event_json)
        self.assertNotIn("private source bytes", event_json)

    def test_source_download_records_failure_without_document_content(self):
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="본문",
            text="실패 이벤트에도 본문은 남기지 않는다",
            source_file="missing.hwp",
            source_type="hwp",
            source_path=str(Path(self.tmp.name) / "missing.hwp"),
        )
        chunk["version_id"] = "version-failed"
        self.write_chunks([chunk])

        with self.assertRaises(HTTPError) as context:
            urlopen(f"{self.base_url}/api/download/source?id={chunk['id']}", timeout=5)

        self.assertEqual(context.exception.code, 404)
        event = self.registry.events(1)[0]
        event_json = json.dumps(event, ensure_ascii=False)
        self.assertEqual(event["event_type"], "SourceDownloadFailed")
        self.assertEqual(event["metadata"]["source_file"], "missing.hwp")
        self.assertEqual(event["metadata"]["version_id"], "version-failed")
        self.assertEqual(event["metadata"]["outcome"], "not_found")
        self.assertNotIn("실패 이벤트", event_json)

    def test_source_download_records_failure_for_unknown_chunk_id(self):
        self.write_chunks([])

        with self.assertRaises(HTTPError) as context:
            urlopen(f"{self.base_url}/api/download/source?id=missing-chunk", timeout=5)

        self.assertEqual(context.exception.code, 404)
        event = self.registry.events(1)[0]
        self.assertEqual(event["event_type"], "SourceDownloadFailed")
        self.assertEqual(event["metadata"]["outcome"], "chunk_not_found")
        self.assertEqual(event["metadata"]["requested_id_length"], len("missing-chunk"))
        self.assertEqual(event["metadata"]["requested_id_sha256"], sha256(b"missing-chunk").hexdigest())
        self.assertEqual(event["metadata"]["requested_id_format_valid"], False)
        self.assertNotIn("missing-chunk", json.dumps(event, ensure_ascii=False))
        self.assertNotIn("requested_id\"", json.dumps(event, ensure_ascii=False))
        self.assertIsNone(event["metadata"]["source_file"])
        self.assertIsNone(event["metadata"]["version_id"])

    def test_source_download_unknown_malicious_id_records_only_safe_diagnostics(self):
        malicious_id = "../../secret?body=private&summary=leak"
        self.write_chunks([])

        with self.assertRaises(HTTPError):
            urlopen(f"{self.base_url}/api/download/source?{urlencode({'id': malicious_id})}", timeout=5)

        event = self.registry.events(1)[0]
        event_json = json.dumps(event, ensure_ascii=False)
        self.assertEqual(event["event_type"], "SourceDownloadFailed")
        self.assertEqual(event["metadata"]["outcome"], "chunk_not_found")
        self.assertEqual(event["metadata"]["requested_id_length"], len(malicious_id))
        self.assertEqual(event["metadata"]["requested_id_sha256"], sha256(malicious_id.encode("utf-8")).hexdigest())
        self.assertEqual(event["metadata"]["requested_id_format_valid"], False)
        self.assertNotIn(malicious_id, event_json)
        self.assertNotIn("../../secret", event_json)
        self.assertNotIn("private", event_json)
        self.assertNotIn("summary=leak", event_json)
        self.assertNotIn("requested_id\"", event_json)

    def test_source_download_read_failure_records_failure_not_success(self):
        source = Path(self.tmp.name) / "인사규정_2026.01.01.hwp"
        source.write_bytes(b"private source bytes")
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="본문",
            text="문서 본문",
            source_file=source.name,
            source_type="hwp",
            source_path=str(source),
        )
        chunk["version_id"] = "version-read-failed"
        self.write_chunks([chunk])

        with mock.patch.object(Path, "read_bytes", side_effect=OSError("cannot read")):
            with self.assertRaises(HTTPError) as context:
                urlopen(f"{self.base_url}/api/download/source?id={chunk['id']}", timeout=5)

        self.assertEqual(context.exception.code, 404)
        events = self.registry.events(10)
        self.assertEqual([event["event_type"] for event in events], ["SourceDownloadFailed"])
        self.assertEqual(events[0]["metadata"]["outcome"], "read_failed")
        self.assertEqual(events[0]["metadata"]["version_id"], "version-read-failed")

    def test_source_download_send_failure_records_failure_not_success(self):
        source = Path(self.tmp.name) / "인사규정_2026.01.01.hwp"
        source.write_bytes(b"private source bytes")
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="본문",
            text="문서 본문?query=private&path=/secret",
            source_file=source.name,
            source_type="hwp",
            source_path=str(source),
        )
        chunk["version_id"] = "version-source-send-failed"

        with mock.patch.object(server, "send_bytes", side_effect=BrokenPipeError("client closed")):
            with self.assertRaises(BrokenPipeError):
                server.source_download(mock.Mock(), chunk)

        events = self.registry.events(10)
        self.assertEqual([event["event_type"] for event in events], ["SourceDownloadFailed"])
        event_json = json.dumps(events[0], ensure_ascii=False)
        self.assertEqual(events[0]["metadata"]["outcome"], "send_failed")
        self.assertEqual(events[0]["metadata"]["version_id"], "version-source-send-failed")
        self.assertNotIn("SourceDownloaded", event_json)
        self.assertNotIn("문서 본문", event_json)
        self.assertNotIn("query=private", event_json)
        self.assertNotIn("/secret", event_json)

    def test_source_pdf_existing_pdf_read_failure_records_failure_not_success(self):
        source = Path(self.tmp.name) / "인사규정_2026.01.01.pdf"
        source.write_bytes(b"%PDF private source bytes")
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="본문",
            text="문서 본문",
            source_file=source.name,
            source_type="pdf",
            source_path=str(source),
        )
        chunk["version_id"] = "version-pdf-read-failed"
        self.write_chunks([chunk])

        with mock.patch.object(Path, "read_bytes", side_effect=OSError("cannot read")):
            with self.assertRaises(HTTPError) as context:
                urlopen(f"{self.base_url}/api/download/source-pdf?id={chunk['id']}", timeout=5)

        self.assertEqual(context.exception.code, 404)
        events = self.registry.events(10)
        self.assertEqual([event["event_type"] for event in events], ["SourceDownloadFailed"])
        self.assertEqual(events[0]["metadata"]["outcome"], "read_failed")
        self.assertEqual(events[0]["metadata"]["version_id"], "version-pdf-read-failed")

    def test_source_pdf_existing_pdf_send_failure_records_failure_not_success(self):
        source = Path(self.tmp.name) / "인사규정_2026.01.01.pdf"
        source.write_bytes(b"%PDF private source bytes")
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="본문",
            text="문서 본문?query=private&path=/secret",
            source_file=source.name,
            source_type="pdf",
            source_path=str(source),
        )
        chunk["version_id"] = "version-pdf-send-failed"

        with mock.patch.object(server, "send_bytes", side_effect=BrokenPipeError("client closed")):
            with self.assertRaises(BrokenPipeError):
                server.source_pdf_download(mock.Mock(), chunk)

        events = self.registry.events(10)
        self.assertEqual([event["event_type"] for event in events], ["SourceDownloadFailed"])
        event_json = json.dumps(events[0], ensure_ascii=False)
        self.assertEqual(events[0]["metadata"]["outcome"], "send_failed")
        self.assertEqual(events[0]["metadata"]["version_id"], "version-pdf-send-failed")
        self.assertNotIn("SourceDownloaded", event_json)
        self.assertNotIn("문서 본문", event_json)
        self.assertNotIn("query=private", event_json)
        self.assertNotIn("/secret", event_json)

    def test_source_pdf_conversion_failure_records_failure_not_success(self):
        source = Path(self.tmp.name) / "인사규정_2026.01.01.hwp"
        source.write_bytes(b"private source bytes")
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="본문",
            text="문서 본문",
            source_file=source.name,
            source_type="hwp",
            source_path=str(source),
        )
        chunk["version_id"] = "version-conversion-failed"
        self.write_chunks([chunk])

        with mock.patch.object(server, "make_source_pdf", side_effect=RuntimeError("cannot convert")):
            with self.assertRaises(HTTPError) as context:
                urlopen(f"{self.base_url}/api/download/source-pdf?id={chunk['id']}", timeout=5)

        self.assertEqual(context.exception.code, 500)
        events = self.registry.events(10)
        self.assertEqual([event["event_type"] for event in events], ["SourceDownloadFailed"])
        self.assertEqual(events[0]["metadata"]["outcome"], "conversion_failed")
        self.assertEqual(events[0]["metadata"]["version_id"], "version-conversion-failed")

    def test_source_pdf_converted_send_failure_records_failure_not_success(self):
        source = Path(self.tmp.name) / "인사규정_2026.01.01.hwp"
        source.write_bytes(b"private source bytes")
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="본문",
            text="문서 본문?query=private&path=/secret",
            source_file=source.name,
            source_type="hwp",
            source_path=str(source),
        )
        chunk["version_id"] = "version-converted-send-failed"

        with (
            mock.patch.object(server, "make_source_pdf", return_value=b"%PDF converted bytes"),
            mock.patch.object(server, "send_bytes", side_effect=BrokenPipeError("client closed")),
        ):
            with self.assertRaises(BrokenPipeError):
                server.source_pdf_download(mock.Mock(), chunk)

        events = self.registry.events(10)
        self.assertEqual([event["event_type"] for event in events], ["SourceDownloadFailed"])
        event_json = json.dumps(events[0], ensure_ascii=False)
        self.assertEqual(events[0]["metadata"]["outcome"], "send_failed")
        self.assertEqual(events[0]["metadata"]["version_id"], "version-converted-send-failed")
        self.assertNotIn("SourceDownloaded", event_json)
        self.assertNotIn("문서 본문", event_json)
        self.assertNotIn("query=private", event_json)
        self.assertNotIn("/secret", event_json)

    def test_source_pdf_existing_pdf_success_records_success_after_reading_bytes(self):
        source = Path(self.tmp.name) / "인사규정_2026.01.01.pdf"
        source.write_bytes(b"%PDF private source bytes")
        chunk = server.make_chunk(
            doc_title="인사규정",
            section_title="본문",
            text="문서 본문은 감사 이벤트에 남기지 않는다",
            source_file=source.name,
            source_type="pdf",
            source_path=str(source),
        )
        chunk["version_id"] = "version-pdf-success"
        self.write_chunks([chunk])

        with urlopen(f"{self.base_url}/api/download/source-pdf?id={chunk['id']}", timeout=5) as response:
            body = response.read()

        self.assertEqual(body, b"%PDF private source bytes")
        event = self.wait_for_event()
        event_json = json.dumps(event, ensure_ascii=False)
        self.assertEqual(event["event_type"], "SourceDownloaded")
        self.assertEqual(event["metadata"]["source_file"], source.name)
        self.assertEqual(event["metadata"]["version_id"], "version-pdf-success")
        self.assertEqual(event["metadata"]["outcome"], "success")
        self.assertNotIn("문서 본문", event_json)
        self.assertNotIn("%PDF private source bytes", event_json)

    def test_post_routes_reject_valid_non_object_json_bodies(self):
        array_status, array_body = self.post_raw_json("/api/search", "[]")
        null_status, null_body = self.post_raw_json("/api/versions/approve", "null")

        self.assertEqual(array_status, 400)
        self.assertIn("JSON object", array_body["error"])
        self.assertEqual(null_status, 400)
        self.assertIn("JSON object", null_body["error"])


if __name__ == "__main__":
    unittest.main()
