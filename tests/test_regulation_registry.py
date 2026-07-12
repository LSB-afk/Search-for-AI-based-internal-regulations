import tempfile
import unittest
from pathlib import Path

from regulation_registry import RegulationRegistry


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
            ["detected", "RegulationVersionScanFailed"],
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


if __name__ == "__main__":
    unittest.main()
