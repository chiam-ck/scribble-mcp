from __future__ import annotations

import inspect
import unittest

import vault_api
from vault_mutations import VaultMutator


class LinuxApiIntegrationTests(unittest.TestCase):
    def test_linux_api_uses_fuse_safe_shared_mutator(self) -> None:
        self.assertIsInstance(vault_api.MUTATOR, VaultMutator)
        self.assertFalse(vault_api.MUTATOR.atomic)
        self.assertNotIn("write_text", inspect.getsource(vault_api.Handler.do_POST))


if __name__ == "__main__":
    unittest.main()
