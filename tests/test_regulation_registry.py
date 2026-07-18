import json
import tempfile
import unittest
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from regulation_registry import RegulationRegistry, business_today_iso, date_from_path


class RegulationRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = RegulationRegistry(Path(self.tmp.name) / "registry.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_approval_supersedes_previous_version(self):
        old = self.registry.record_detection(
            canonical_title="인사규정",
            source_path="/closed/인사규정_2025.01.01.hwp",
            content_hash="old-hash",
            effective_from="2025-01-01",
            chunk_ids=["old-1"],
        )
        self.registry.approve_version(old["version_id"], "감사팀장", "2025-01-01")
        new = self.registry.record_detection(
            canonical_title="인사규정",
            source_path="/closed/인사규정_2026.05.27.hwp",
            content_hash="new-hash",
            effective_from="2026-05-27",
            chunk_ids=["new-1"],
        )
        self.registry.approve_version(new["version_id"], "감사팀장", "2026-05-27")

        current = self.registry.versions(as_of="2026-07-12", include_history=False)
        history = self.registry.versions(as_of="2026-07-12", include_history=True)
        self.assertEqual([item["version_id"] for item in current], [new["version_id"]])
        self.assertEqual({item["status"] for item in history}, {"approved", "superseded"})
        old_version = next(item for item in history if item["version_id"] == old["version_id"])
        self.assertEqual(old_version["effective_to"], "2026-05-26")

    def test_business_day_uses_korea_timezone_at_utc_boundary(self):
        utc_time = datetime(2026, 7, 12, 15, 30, tzinfo=timezone.utc)

        self.assertEqual(business_today_iso(utc_time), "2026-07-13")

    def test_missing_effective_date_stays_pending_and_is_not_current(self):
        source = Path(self.tmp.name) / "시행일없는규정.hwp"
        source.write_bytes(b"undated")

        result = self.registry.scan_sources(
            [source],
            lambda path: [{"id": "undated-chunk", "text": "시행일 확인 필요"}],
            effective_date=lambda path: None,
        )

        version = next(iter(self.registry.state["versions"].values()))
        self.assertIsNone(date_from_path(source))
        self.assertIsNone(version["effective_from"])
        self.assertEqual(version["status"], "pending")
        self.assertEqual(result["chunks"][0]["version_status"], "pending")
        self.assertEqual(self.registry.versions(as_of="2026-07-13"), [])

    def test_audit_events_use_normalized_schema_and_transition_names(self):
        old = self.registry.record_detection("인사규정", "/closed/old.hwp", "old", "2025-01-01", ["old"])
        self.registry.approve_version(old["version_id"], "감사팀장", "2025-01-01", today="2026-07-13")
        new = self.registry.record_detection("인사규정", "/closed/new.hwp", "new", "2026-01-01", ["new"])
        self.registry.approve_version(new["version_id"], "감사팀장", "2026-01-01", today="2026-07-13")
        rejected = self.registry.record_detection("복무규정", "/closed/rejected.hwp", "rejected", None, [])
        self.registry.reject_version(rejected["version_id"], "감사팀장", "시행일 미상")
        scheduled = self.registry.record_detection("감사규정", "/closed/future.hwp", "future", "2027-01-01", [])
        self.registry.approve_version(
            scheduled["version_id"], "감사팀장", "2027-01-01", today="2026-07-13"
        )
        self.registry.refresh_statuses(today="2027-01-01")

        events = self.registry.events(100)
        event_types = {event["event_type"] for event in events}
        self.assertTrue(
            {
                "RegulationVersionDetected",
                "RegulationVersionApproved",
                "RegulationVersionScheduled",
                "RegulationVersionRejected",
                "RegulationVersionSuperseded",
                "ScheduledVersionActivated",
            }.issubset(event_types)
        )
        for event in events:
            self.assertTrue(event["occurred_at"])
            self.assertIn("actor_role", event)
            self.assertIn("actor_name", event)
            self.assertEqual(event["target_type"], "regulation_version")
            self.assertTrue(event["target_id"])
            self.assertTrue(event["result"])

    def test_historical_approval_after_current_version_closes_old_window(self):
        new = self.registry.record_detection(
            canonical_title="인사규정",
            source_path="/closed/인사규정_2026.05.27.hwp",
            content_hash="new-hash",
            effective_from="2026-05-27",
            chunk_ids=["new-1"],
        )
        self.registry.approve_version(new["version_id"], "감사팀장", "2026-05-27", today="2026-07-12")
        old = self.registry.record_detection(
            canonical_title="인사규정",
            source_path="/closed/인사규정_2025.01.01.hwp",
            content_hash="old-hash",
            effective_from="2025-01-01",
            chunk_ids=["old-1"],
        )
        self.registry.approve_version(old["version_id"], "감사팀장", "2025-01-01", today="2026-07-12")

        current = self.registry.versions(as_of="2026-07-12", include_history=False)
        history = self.registry.versions(as_of="2026-07-12", include_history=True)
        old_version = next(item for item in history if item["version_id"] == old["version_id"])

        self.assertEqual([item["version_id"] for item in current], [new["version_id"]])
        self.assertEqual(old_version["status"], "superseded")
        self.assertEqual(old_version["effective_to"], "2026-05-26")

    def test_future_version_is_scheduled_until_effective_date(self):
        version = self.registry.record_detection(
            canonical_title="감사규정",
            source_path="/closed/감사규정_2027.01.01.hwp",
            content_hash="future-hash",
            effective_from="2027-01-01",
            chunk_ids=["future-1"],
        )
        approved = self.registry.approve_version(
            version["version_id"], "감사팀장", "2027-01-01", today="2026-07-12"
        )
        self.assertEqual(approved["status"], "scheduled")
        self.assertEqual(self.registry.versions(as_of="2026-07-12", include_history=False), [])
        self.assertEqual(len(self.registry.versions(as_of="2027-01-01", include_history=False)), 1)

    def test_scheduled_replacement_does_not_hide_current_version_before_effective_date(self):
        current = self.registry.record_detection(
            canonical_title="복무규정",
            source_path="/closed/복무규정_2026.01.01.hwp",
            content_hash="current-hash",
            effective_from="2026-01-01",
            chunk_ids=["current-1"],
        )
        self.registry.approve_version(current["version_id"], "감사팀장", "2026-01-01", today="2026-07-12")
        replacement = self.registry.record_detection(
            canonical_title="복무규정",
            source_path="/closed/복무규정_2027.01.01.hwp",
            content_hash="replacement-hash",
            effective_from="2027-01-01",
            chunk_ids=["replacement-1"],
        )
        self.registry.approve_version(
            replacement["version_id"], "감사팀장", "2027-01-01", today="2026-07-12"
        )

        before_effective = self.registry.versions(as_of="2026-12-31", include_history=False)
        on_effective = self.registry.versions(as_of="2027-01-01", include_history=False)
        after_effective = self.registry.versions(as_of="2027-02-01", include_history=False)

        self.assertEqual([item["version_id"] for item in before_effective], [current["version_id"]])
        self.assertEqual([item["version_id"] for item in on_effective], [replacement["version_id"]])
        self.assertEqual([item["version_id"] for item in after_effective], [replacement["version_id"]])

    def test_duplicate_hash_reuses_detected_version(self):
        first = self.registry.record_detection("회계규정", "/closed/a.hwp", "same", "2026-05-27", ["a"])
        second = self.registry.record_detection("회계규정", "/closed/b.hwp", "same", "2026-05-27", ["b"])
        self.assertEqual(first["version_id"], second["version_id"])

    def test_scan_detects_new_unchanged_and_changed_files(self):
        source = Path(self.tmp.name) / "인사규정_2026.05.27.hwp"
        source.write_bytes(b"version-one")

        first = self.registry.scan_sources(
            [source],
            lambda path: ["chunk-v1"],
            effective_date=lambda path: "2026-05-27",
        )
        second = self.registry.scan_sources(
            [source],
            lambda path: ["chunk-v1"],
            effective_date=lambda path: "2026-05-27",
        )
        source.write_bytes(b"version-two")
        third = self.registry.scan_sources(
            [source],
            lambda path: ["chunk-v2"],
            effective_date=lambda path: "2026-06-01",
        )

        self.assertEqual(first["new_count"], 1)
        self.assertEqual(second["unchanged_count"], 1)
        self.assertEqual(third["changed_count"], 1)
        self.assertEqual(first["chunks"], [])

    def test_scan_injects_version_metadata_into_chunk_dicts(self):
        source = Path(self.tmp.name) / "감사규정_2026.05.27.pdf"
        source.write_bytes(b"pdf-version")

        result = self.registry.scan_sources(
            [source],
            lambda path: [{"id": "chunk-1", "text": "감사 자료"}],
            effective_date=lambda path: "2026-05-27",
        )

        self.assertEqual(result["new_count"], 1)
        self.assertEqual(len(result["chunks"]), 1)
        chunk = result["chunks"][0]
        version = next(iter(self.registry.state["versions"].values()))
        self.assertEqual(chunk["regulation_id"], version["regulation_id"])
        self.assertEqual(chunk["version_id"], version["version_id"])
        self.assertEqual(chunk["version_status"], "pending")

    def test_real_revision_filenames_share_one_canonical_regulation(self):
        old_source = Path(self.tmp.name) / "18. 인사규정(개정 2025.5.27.).hwp"
        new_source = Path(self.tmp.name) / unicodedata.normalize(
            "NFD", "18. 인사규정(개정 2026.4.13.).hwp"
        )
        old_source.write_bytes(b"old revision")
        new_source.write_bytes(b"new revision")

        old_result = self.registry.scan_sources(
            [old_source],
            lambda path: [{"id": "old-chunk", "text": "구 인사규정"}],
        )
        new_result = self.registry.scan_sources(
            [new_source],
            lambda path: [{"id": "new-chunk", "text": "신 인사규정"}],
        )

        versions = list(self.registry.state["versions"].values())
        self.assertEqual(old_result["new_count"], 1)
        self.assertEqual(new_result["changed_count"], 1)
        self.assertEqual(len(self.registry.state["regulations"]), 1)
        self.assertEqual({version["canonical_title"] for version in versions}, {"18. 인사규정"})
        self.assertEqual(len({version["regulation_id"] for version in versions}), 1)
        self.assertEqual(
            {version["effective_from"] for version in versions},
            {"2025-05-27", "2026-04-13"},
        )

    def test_rescanning_multiple_revision_files_reuses_all_historical_versions(self):
        old_source = Path(self.tmp.name) / "18. 인사규정(개정 2025.5.27.).hwp"
        new_source = Path(self.tmp.name) / unicodedata.normalize(
            "NFD", "18. 인사규정(개정 2026.4.13.).hwp"
        )
        old_source.write_bytes(b"old revision")
        new_source.write_bytes(b"new revision")

        first = self.registry.scan_sources(
            [old_source, new_source],
            lambda path: [{"id": f"{path.name}-chunk", "text": path.name}],
        )
        original_version_ids = set(self.registry.state["versions"])
        self.registry.mark_versions_indexed(first["version_ids"])

        second = self.registry.scan_sources(
            [old_source, new_source],
            lambda path: [{"id": f"{path.name}-chunk", "text": path.name}],
        )

        self.assertEqual(first["new_count"], 1)
        self.assertEqual(first["changed_count"], 1)
        self.assertEqual(second["unchanged_count"], 2)
        self.assertEqual(second["changed_count"], 0)
        self.assertEqual(set(self.registry.state["versions"]), original_version_ids)
        self.assertEqual(len(self.registry.state["versions"]), 2)

    def test_reset_clears_registry_state_and_persists_empty_state(self):
        self.registry.record_detection(
            "인사규정", "/closed/new.hwp", "hash", "2026-05-27", ["chunk"]
        )

        self.registry.reset()

        self.assertEqual(self.registry.state["regulations"], {})
        self.assertEqual(self.registry.state["versions"], {})
        self.assertEqual(self.registry.state["scan_runs"], [])
        self.assertEqual(self.registry.state["events"], [])
        reloaded = RegulationRegistry(self.registry.path)
        self.assertEqual(reloaded.state, self.registry.state)

    def test_loading_legacy_fragmented_revision_titles_merges_regulations(self):
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
                    "change_type": "new",
                    "status": "approved",
                },
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
                    "change_type": "new",
                    "status": "approved",
                },
            },
            "scan_runs": [],
            "events": [],
        }
        self.registry.path.write_text(json.dumps(legacy_state, ensure_ascii=False), encoding="utf-8")

        migrated = RegulationRegistry(self.registry.path)

        self.assertEqual(len(migrated.state["regulations"]), 1)
        self.assertEqual(
            {version["canonical_title"] for version in migrated.state["versions"].values()},
            {"18. 인사규정"},
        )
        self.assertEqual(
            len({version["regulation_id"] for version in migrated.state["versions"].values()}),
            1,
        )
        self.assertEqual(migrated.state["versions"]["old-version"]["status"], "superseded")
        self.assertEqual(migrated.state["versions"]["old-version"]["effective_to"], "2026-04-12")
        self.assertEqual(migrated.state["versions"]["new-version"]["status"], "approved")

    def test_startup_window_repair_records_and_persists_status_transition_events(self):
        old = self.registry.record_detection(
            "인사규정", "/closed/old.hwp", "old", "2025-01-01", ["old-chunk"]
        )
        self.registry.approve_version(
            old["version_id"], "감사팀장", "2025-01-01", today="2025-06-01"
        )
        scheduled = self.registry.record_detection(
            "인사규정", "/closed/new.hwp", "new", "2026-04-13", ["new-chunk"]
        )
        self.registry.approve_version(
            scheduled["version_id"], "감사팀장", "2026-04-13", today="2025-06-01"
        )
        original_event_count = len(self.registry.state["events"])

        with mock.patch("regulation_registry.business_today_iso", return_value="2026-07-14"):
            reloaded = RegulationRegistry(self.registry.path)

        transition_events = reloaded.state["events"][original_event_count:]
        self.assertEqual(
            [event["event_type"] for event in transition_events],
            ["RegulationVersionSuperseded", "ScheduledVersionActivated"],
        )
        self.assertEqual(reloaded.state["versions"][old["version_id"]]["status"], "superseded")
        self.assertEqual(
            reloaded.state["versions"][scheduled["version_id"]]["status"], "approved"
        )
        persisted = json.loads(self.registry.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["events"], reloaded.state["events"])

    def test_revision_normalization_preserves_meaningful_parentheses(self):
        version = self.registry.record_detection(
            "18. 임기제(직) 인사규정(전부개정 2026.4.13.).hwp",
            "/closed/term.hwp",
            "term-hash",
            "2026-04-13",
            ["term-chunk"],
        )

        self.assertEqual(version["canonical_title"], "18. 임기제(직) 인사규정")

    def test_scan_isolates_parser_failures(self):
        good = Path(self.tmp.name) / "복무규정_2026.05.27.hwp"
        bad = Path(self.tmp.name) / "회계규정_2026.05.27.hwp"
        good.write_bytes(b"good")
        bad.write_bytes(b"bad")

        def ingest(path):
            if path == bad:
                raise RuntimeError("broken parser")
            return ["good-chunk"]

        result = self.registry.scan_sources(
            [good, bad],
            ingest,
            effective_date=lambda path: "2026-05-27",
        )

        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["source_path"], str(bad.resolve()))
        self.assertEqual(result["errors"][0]["error"], "broken parser")
        self.assertEqual(
            [event["event_type"] for event in self.registry.events()],
            ["RegulationVersionDetected", "RegulationVersionScanFailed"],
        )
        error_versions = [
            version for version in self.registry.state["versions"].values() if version["status"] == "scan_error"
        ]
        self.assertEqual(len(error_versions), 1)

    def test_scan_retries_sources_after_parser_failure(self):
        source = Path(self.tmp.name) / "회계규정_2026.05.27.hwp"
        source.write_bytes(b"same-content")
        attempts = 0

        def ingest(path):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary parser failure")
            return ["chunk-after-retry"]

        first = self.registry.scan_sources(
            [source],
            ingest,
            effective_date=lambda path: "2026-05-27",
        )
        second = self.registry.scan_sources(
            [source],
            ingest,
            effective_date=lambda path: "2026-05-27",
        )

        self.assertEqual(first["error_count"], 1)
        self.assertEqual(second["new_count"], 1)
        self.assertEqual(attempts, 2)

    def test_scan_isolates_effective_date_failures_and_continues(self):
        bad = Path(self.tmp.name) / "회계규정_2026.05.27.hwp"
        good = Path(self.tmp.name) / "복무규정_2026.05.27.hwp"
        bad.write_bytes(b"bad")
        good.write_bytes(b"good")

        def effective_date(path):
            if path == bad:
                raise RuntimeError("broken date extractor")
            return "2026-05-27"

        result = self.registry.scan_sources(
            [bad, good],
            lambda path: [{"id": f"{path.stem}-chunk", "text": path.stem}],
            effective_date=effective_date,
        )

        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["errors"][0]["source_path"], str(bad.resolve()))
        self.assertEqual(result["errors"][0]["error"], "broken date extractor")
        scan_statuses = [source["status"] for source in self.registry.state["scan_runs"][-1]["sources"]]
        self.assertEqual(scan_statuses, ["error", "new"])
        self.assertEqual(
            [event["event_type"] for event in self.registry.events()],
            ["RegulationVersionScanFailed", "RegulationVersionDetected"],
        )

    def test_dictionary_chunks_retry_until_indexed_acknowledged(self):
        source = Path(self.tmp.name) / "감사규정_2026.05.27.pdf"
        source.write_bytes(b"same-content")

        def ingest(path):
            return [{"id": "chunk-1", "text": "감사 자료"}]

        first = self.registry.scan_sources([source], ingest, effective_date=lambda path: "2026-05-27")
        version_id = first["chunks"][0]["version_id"]
        second = self.registry.scan_sources([source], ingest, effective_date=lambda path: "2026-05-27")
        self.registry.mark_versions_indexed([version_id])
        third = self.registry.scan_sources([source], ingest, effective_date=lambda path: "2026-05-27")

        self.assertEqual(first["new_count"], 1)
        self.assertEqual(second["unchanged_count"], 0)
        self.assertEqual(len(second["chunks"]), 1)
        self.assertEqual(second["chunks"][0]["version_id"], version_id)
        self.assertEqual(third["unchanged_count"], 1)
        self.assertEqual(third["chunks"], [])

    def test_local_ingest_marks_versions_indexed_only_after_add_chunks_succeeds(self):
        import server

        source = Path(self.tmp.name) / "감사규정_2026.05.27.pdf"
        source.write_bytes(b"same-content")
        registry = RegulationRegistry(Path(self.tmp.name) / "server-registry.json")
        calls = []

        def ingest_file(path):
            return [{"id": "chunk-1", "text": "감사 자료"}]

        def failing_add_chunks(chunks):
            calls.append([chunk["version_id"] for chunk in chunks])
            raise RuntimeError("index write failed")

        with mock.patch.object(server, "REGISTRY", registry), mock.patch.object(
            server, "local_sources", return_value=[source]
        ), mock.patch.object(server, "ingest_file", side_effect=ingest_file), mock.patch.object(
            server, "add_chunks", side_effect=failing_add_chunks
        ), mock.patch.object(
            server, "document_summary", return_value=[]
        ):
            with self.assertRaises(RuntimeError):
                server.ingest_local_sources()

        failed_version_id = calls[0][0]
        self.assertFalse(registry.state["versions"][failed_version_id].get("indexed"))

        with mock.patch.object(server, "REGISTRY", registry), mock.patch.object(
            server, "local_sources", return_value=[source]
        ), mock.patch.object(server, "ingest_file", side_effect=ingest_file), mock.patch.object(
            server, "add_chunks", return_value=1
        ) as add_chunks, mock.patch.object(
            server, "document_summary", return_value=[]
        ):
            result = server.ingest_local_sources()

        indexed_version_id = add_chunks.call_args.args[0][0]["version_id"]
        self.assertEqual(indexed_version_id, failed_version_id)
        self.assertTrue(registry.state["versions"][failed_version_id]["indexed"])
        self.assertEqual(result["unchanged_count"], 0)


if __name__ == "__main__":
    unittest.main()
