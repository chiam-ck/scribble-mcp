from __future__ import annotations

import concurrent.futures
import tempfile
import unittest
from datetime import date
from pathlib import Path

from vault_mutations import MetadataError, VaultMutator


TODAY = date(2026, 9, 16)


def note(*, created: str = "2026-08-31", updated: str = "2026-09-15", body: str = "# Note\n\nOriginal body.\n") -> str:
    return (
        "---\n"
        "title: Test Note\n"
        f"created: {created}\n"
        f"updated: {updated}\n"
        "type: concept\n"
        "tags: [test, concept]\n"
        "sources:\n"
        "  - \"fixture\"\n"
        "related: [test-parent]\n"
        "---\n"
        "\n"
        + body
    )


class FailingReplaceMutator(VaultMutator):
    def _replace(self, temporary: Path, target: Path) -> None:
        raise OSError("injected replace failure")


class VaultMutatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.mutator = VaultMutator(self.root, today=lambda: TODAY)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_fixture(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def test_append_existing_concept_bumps_updated_and_preserves_other_content(self) -> None:
        original = note(body="# Note\n\nOriginal body.\n")
        path = self.write_fixture("wiki/concepts/2026/example.md", original)

        result = self.mutator.append("wiki/concepts/2026/example.md", "\nAppended.\n")

        self.assertTrue(result.metadata_updated)
        self.assertEqual(result.updated, "2026-09-16")
        actual = path.read_text(encoding="utf-8")
        self.assertEqual(actual.count("updated:"), 1)
        self.assertIn("updated: 2026-09-16\n", actual)
        self.assertIn("title: Test Note\ncreated: 2026-08-31\n", actual)
        self.assertIn("Original body.\n\nAppended.\n", actual)

    def test_append_note_already_dated_today_does_not_rewrite_metadata(self) -> None:
        original = note(updated="2026-09-16")
        path = self.write_fixture("wiki/concepts/2026/example.md", original)

        result = self.mutator.append("wiki/concepts/2026/example.md", "\nAgain.\n")

        self.assertFalse(result.metadata_updated)
        self.assertEqual(path.read_text(encoding="utf-8"), original + "\nAgain.\n")

    def test_overwrite_existing_note_bumps_updated_and_preserves_created(self) -> None:
        self.write_fixture("wiki/concepts/2026/example.md", note(created="2026-01-02"))
        replacement = note(created="2026-09-16", updated="2026-09-01", body="# Replaced\n\nNew body.\n")

        result = self.mutator.write("wiki/concepts/2026/example.md", replacement)

        actual = (self.root / "wiki/concepts/2026/example.md").read_text(encoding="utf-8")
        self.assertTrue(result.metadata_updated)
        self.assertIn("created: 2026-01-02\n", actual)
        self.assertIn("updated: 2026-09-16\n", actual)
        self.assertIn("New body.\n", actual)

    def test_new_valid_note_is_validated_and_written_without_unrelated_metadata(self) -> None:
        content = note(created="2026-09-16", updated="2026-09-16", body="# New\n")

        result = self.mutator.write("wiki/concepts/2026/new-note.md", content)

        self.assertFalse(result.metadata_updated)
        self.assertEqual(result.updated, "2026-09-16")
        self.assertEqual((self.root / "wiki/concepts/2026/new-note.md").read_text(encoding="utf-8"), content)

    def test_new_markdown_without_frontmatter_is_rejected(self) -> None:
        with self.assertRaises(MetadataError):
            self.mutator.write("wiki/concepts/2026/new-note.md", "# Missing frontmatter\n")
        self.assertFalse((self.root / "wiki/concepts/2026/new-note.md").exists())

    def test_log_append_is_exempt_from_metadata_mutation(self) -> None:
        original = note(updated="2026-01-01", body="- historical entry\n")
        path = self.write_fixture("wiki/log.md", original)

        result = self.mutator.append("wiki/log.md", "- new entry\n")

        self.assertFalse(result.metadata_updated)
        self.assertEqual(path.read_text(encoding="utf-8"), original + "- new entry\n")

    def test_raw_file_append_is_exempt_from_metadata_mutation(self) -> None:
        original = note(updated="2026-01-01", body="raw source\n")
        path = self.write_fixture("wiki/raw/articles/2026/source.md", original)

        result = self.mutator.append("wiki/raw/articles/2026/source.md", "\nmore raw source\n")

        self.assertFalse(result.metadata_updated)
        self.assertEqual(path.read_text(encoding="utf-8"), original + "\nmore raw source\n")

    def test_binary_asset_append_is_byte_preserving_and_metadata_exempt(self) -> None:
        original = b"\x89PNG\r\n\x1a\n\x00binary"
        path = self.root / "wiki/raw/assets/image.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)

        result = self.mutator.append("wiki/raw/assets/image.png", "\nnot-metadata")

        self.assertFalse(result.metadata_updated)
        self.assertEqual(path.read_bytes(), original + b"\nnot-metadata")

    def test_fuse_safe_mode_enforces_metadata_without_atomic_rename(self) -> None:
        original = note(updated="2026-09-15")
        path = self.write_fixture("wiki/concepts/2026/fuse-safe.md", original)
        fuse_safe = VaultMutator(self.root, today=lambda: TODAY, atomic=False)

        result = fuse_safe.append("wiki/concepts/2026/fuse-safe.md", "\nFUSE-safe append.\n")

        self.assertTrue(result.metadata_updated)
        actual = path.read_text(encoding="utf-8")
        self.assertIn("updated: 2026-09-16\n", actual)
        self.assertIn("FUSE-safe append.\n", actual)

    def test_markdown_without_frontmatter_is_left_unchanged_except_for_append(self) -> None:
        original = "# Plain file\n\nNo YAML here.\n"
        path = self.write_fixture("wiki/queries/2026/plain.md", original)

        result = self.mutator.append("wiki/queries/2026/plain.md", "\nAdded text.\n")

        self.assertFalse(result.metadata_updated)
        self.assertEqual(path.read_text(encoding="utf-8"), original + "\nAdded text.\n")

    def test_malformed_frontmatter_fails_without_touching_original(self) -> None:
        original = "---\ntitle: [broken\nupdated: 2026-09-01\n---\n\nBody\n"
        path = self.write_fixture("wiki/concepts/2026/broken.md", original)

        with self.assertRaises(MetadataError):
            self.mutator.append("wiki/concepts/2026/broken.md", "\nShould not land.\n")

        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_missing_updated_fails_without_touching_original(self) -> None:
        original = "---\ntitle: Missing Updated\ncreated: 2026-09-01\ntype: concept\ntags: [test]\nsources: []\nrelated: [parent]\n---\n\nBody\n"
        path = self.write_fixture("wiki/concepts/2026/missing-updated.md", original)

        with self.assertRaises(MetadataError):
            self.mutator.append("wiki/concepts/2026/missing-updated.md", "\nShould not land.\n")

        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_concurrent_appends_to_same_note_do_not_lose_updates(self) -> None:
        self.write_fixture("wiki/concepts/2026/concurrent.md", note())

        def append_one(index: int):
            return self.mutator.append("wiki/concepts/2026/concurrent.md", f"\nmarker-{index}\n")

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(append_one, range(24)))

        actual = (self.root / "wiki/concepts/2026/concurrent.md").read_text(encoding="utf-8")
        for index in range(24):
            self.assertEqual(actual.count(f"marker-{index}\n"), 1)
        self.assertEqual(actual.count("updated:"), 1)
        self.assertIn("updated: 2026-09-16\n", actual)

    def test_failure_during_atomic_replace_leaves_original_intact(self) -> None:
        original = note()
        path = self.write_fixture("wiki/concepts/2026/failure.md", original)
        failing = FailingReplaceMutator(self.root, today=lambda: TODAY)

        with self.assertRaises(OSError):
            failing.append("wiki/concepts/2026/failure.md", "\nShould not land.\n")

        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(list(path.parent.glob(".failure.md.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
