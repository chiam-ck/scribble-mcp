from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from vault_api_macos import require_digest
from vault_mutations import MetadataError, MutationConflict, VaultMutator


class DigestPreconditionTests(unittest.TestCase):
    """The delete/move guard: no digest, no mutation."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "wiki").mkdir()
        self.note = self.root / "wiki" / "note.md"
        self.note.write_text("---\ntitle: x\n---\n\nbody\n", encoding="utf-8")
        self.digest = hashlib.sha256(self.note.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_missing_digest_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            require_digest({}, self.note, "delete", "wiki/note.md")

    def test_malformed_digest_is_refused(self) -> None:
        for bad in ("abc", 123, "z" * 64):
            with self.assertRaises(ValueError):
                require_digest({"expected_sha256": bad}, self.note, "move", "wiki/note.md")

    def test_stale_digest_is_refused_and_names_the_current_one(self) -> None:
        self.note.write_text("changed underneath you\n", encoding="utf-8")
        with self.assertRaises(MutationConflict) as ctx:
            require_digest({"expected_sha256": self.digest}, self.note, "delete", "wiki/note.md")
        self.assertIn("sha256 mismatch", str(ctx.exception))

    def test_matching_digest_returns_the_bytes(self) -> None:
        raw, digest = require_digest({"expected_sha256": self.digest}, self.note, "delete", "wiki/note.md")
        self.assertEqual(digest, self.digest)
        self.assertEqual(raw, self.note.read_bytes())


class FrontmatterFallbackTests(unittest.TestCase):
    """Without PyYAML the parser must fail closed, never guess."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "wiki").mkdir()
        self.mutator = VaultMutator(self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_flow_style_frontmatter_is_refused_without_pyyaml(self) -> None:
        import vault_mutations

        original = vault_mutations.yaml
        vault_mutations.yaml = None
        try:
            content = (
                "---\ntitle: x\ncreated: 2026-11-06\nupdated: 2026-11-06\n"
                "type: concept\ntags: [a, b]\nsources:\n  - s\nrelated: [r]\n---\n\nbody\n"
            )
            with self.assertRaises(MetadataError):
                self.mutator.write("wiki/new.md", content)
        finally:
            vault_mutations.yaml = original


if __name__ == "__main__":
    unittest.main()
