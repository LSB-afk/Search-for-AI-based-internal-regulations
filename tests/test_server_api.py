import json
import tempfile
import unittest
from hashlib import sha256
from datetime import date
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
        self.patches = [
            mock.patch.object(server, "DATA_DIR", self.data_dir),
            mock.patch.object(server, "INDEX_FILE", self.index_file),
            mock.patch.object(server, "REGISTRY", self.registry),
            mock.patch.object(server, "configured_source_roots", lambda: [Path(self.tmp.name).resolve()]),
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

        self.assertEqual(result["as_of"], date.today().isoformat())
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

    def test_search_route_validates_malformed_as_of_and_accepts_include_history(self):
        status, body = self.post_json("/api/search", {"query": "징계", "as_of": "2026-99-99"})

        self.assertEqual(status, 400)
        self.assertIn("as_of", body["error"])

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
        event = self.registry.events(1)[0]
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
        event = self.registry.events(1)[0]
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
        event = self.registry.events(1)[0]
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
