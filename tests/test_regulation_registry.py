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


if __name__ == "__main__":
    unittest.main()
