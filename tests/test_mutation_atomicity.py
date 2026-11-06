"""Temporary-vault regression tests; never touch a configured live vault."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import vault_api_macos
from vault_mutations import MutationConflict, VaultMutator


def digest(data):
    return hashlib.sha256(data).hexdigest()


class AtomicityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "wiki").mkdir()
        self.note = self.root / "wiki/note.md"
        self.note.write_bytes(b"original\n")
        self.mutator = VaultMutator(self.root, lock_dir=self.root / "locks")

    def test_destructive_operations_require_current_digest(self):
        for operation in (self.mutator.delete, lambda p, **kw: self.mutator.move(p, "wiki/new.md", **kw)):
            for value in (None, "bad", 123):
                with self.assertRaises(ValueError):
                    operation("wiki/note.md", expected_sha256=value)
            with self.assertRaises(MutationConflict):
                operation("wiki/note.md", expected_sha256=digest(b"stale"))
            self.assertEqual(self.note.read_bytes(), b"original\n")

    def test_delete_and_move_wait_for_append_then_reject_stale_digest(self):
        for operation in ("delete", "move"):
            with self.subTest(operation=operation):
                self.note.write_bytes(b"original\n")
                old = digest(self.note.read_bytes())
                entered = threading.Event()
                release = threading.Event()
                started = threading.Event()
                commit = self.mutator._commit

                def paused_commit(path, data):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("test commit not released")
                    commit(path, data)

                def destructive():
                    started.set()
                    if operation == "delete":
                        return self.mutator.delete("wiki/note.md", expected_sha256=old)
                    return self.mutator.move("wiki/note.md", "wiki/new.md", expected_sha256=old)

                with patch.object(self.mutator, "_commit", paused_commit), ThreadPoolExecutor(2) as pool:
                    append = pool.submit(self.mutator.append, "wiki/note.md", "appended\n")
                    self.assertTrue(entered.wait(5))
                    pending = pool.submit(destructive)
                    try:
                        self.assertTrue(started.wait(5))
                        self.assertFalse(pending.done())
                    finally:
                        release.set()
                    append.result(timeout=5)
                    with self.assertRaises(MutationConflict):
                        pending.result(timeout=5)
                self.assertEqual(self.note.read_bytes(), b"original\nappended\n")
                self.assertFalse((self.root / "wiki/new.md").exists())

    def test_move_waits_for_destination_write_and_preserves_it(self):
        entered = threading.Event()
        release = threading.Event()
        commit = self.mutator._commit

        def paused_commit(path, data):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test commit not released")
            commit(path, data)

        with patch.object(self.mutator, "_commit", paused_commit), ThreadPoolExecutor(2) as pool:
            write = pool.submit(self.mutator.write, "wiki/new.txt", "destination writer")
            self.assertTrue(entered.wait(5))
            pending = pool.submit(self.mutator.move, "wiki/note.md", "wiki/new.txt", expected_sha256=digest(b"original\n"))
            try:
                self.assertFalse(pending.done())
            finally:
                release.set()
            write.result(timeout=5)
            with self.assertRaises(FileExistsError):
                pending.result(timeout=5)
        self.assertEqual(self.note.read_bytes(), b"original\n")
        self.assertEqual((self.root / "wiki/new.txt").read_text(), "destination writer")

    def test_move_does_not_overwrite_late_destination(self):
        destination = self.root / "wiki/new.md"
        move = self.mutator._move_no_replace

        def competing_create(source, target):
            target.write_bytes(b"competitor")
            move(source, target)

        with patch.object(self.mutator, "_move_no_replace", competing_create):
            with self.assertRaises(FileExistsError):
                self.mutator.move("wiki/note.md", "wiki/new.md", expected_sha256=digest(self.note.read_bytes()))
        self.assertEqual(self.note.read_bytes(), b"original\n")
        self.assertEqual(destination.read_bytes(), b"competitor")

    def test_concurrent_moves_to_same_destination_preserve_loser(self):
        other = self.root / "wiki/other.md"
        other.write_bytes(b"other\n")

        def move(path):
            try:
                self.mutator.move(path, "wiki/new.md", expected_sha256=digest((self.root / path).read_bytes()))
                return True
            except FileExistsError:
                return False

        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(move, ["wiki/note.md", "wiki/other.md"]))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(sum(p.exists() for p in [self.note, other]), 1)
        self.assertTrue((self.root / "wiki/new.md").is_file())

    def test_digest_uses_configured_coordinated_reader(self):
        with patch.object(self.mutator, "read_bytes", return_value=b"hydrated") as read:
            raw, sha = self.mutator.delete("wiki/note.md", expected_sha256=digest(b"hydrated"))
        read.assert_called_once_with(self.note.resolve())
        self.assertEqual((raw, sha), (b"hydrated", digest(b"hydrated")))

    def test_concurrent_audit_lines_are_complete_and_separated(self):
        (self.root / "wiki/log.md").write_text("existing without newline")
        with patch.object(vault_api_macos, "MUTATOR", self.mutator), ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda n: vault_api_macos.audit_mutation("deleted", f"wiki/{n}.md", "a" * 64, n), range(40)))
        self.assertTrue(all(results))
        lines = (self.root / "wiki/log.md").read_text().splitlines()
        self.assertEqual(lines[0], "existing without newline")
        audit = [line for line in lines if "[vault-api]" in line]
        self.assertEqual(len(audit), 40)
        for n in range(40):
            self.assertEqual(sum(f"deleted wiki/{n}.md " in line for line in audit), 1)


class HttpMutationTests(unittest.TestCase):
    def test_macos_server_enforces_preconditions_and_preserves_conflicts(self):
        for module in (vault_api_macos,):
            with self.subTest(server=module.__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "wiki").mkdir()
                note = root / "wiki/note.md"
                note.write_bytes(b"original\n")
                mutator = VaultMutator(root, lock_dir=root / "locks", atomic=module is vault_api_macos)
                server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                with patch.object(module, "VAULT", root), patch.object(module, "MUTATOR", mutator), patch.object(module, "service_ready", return_value=(True, "ok")), patch.object(module.Handler, "log_message"):
                    thread.start()
                    try:
                        def post(endpoint, data):
                            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                            try:
                                connection.request("POST", endpoint, json.dumps(data), {"Content-Type": "application/json"})
                                response = connection.getresponse()
                                return response.status, response.read()
                            finally:
                                connection.close()

                        sha = digest(note.read_bytes())
                        for endpoint, data in (("delete", {"path": "wiki/note.md"}), ("move", {"from": "wiki/note.md", "to": "wiki/new.md"})):
                            self.assertEqual(post("/vault/" + endpoint, data)[0], 400)
                            self.assertEqual(post("/vault/" + endpoint, dict(data, expected_sha256=digest(b"stale")))[0], 409)
                            self.assertEqual(note.read_bytes(), b"original\n")
                        for endpoint in ("write", "append"):
                            self.assertEqual(post("/vault/" + endpoint, {"path": "wiki/note.md", "content": "wrong", "expected_sha256": digest(b"stale")})[0], 409)
                            self.assertEqual(note.read_bytes(), b"original\n")
                        (root / "wiki/new.md").write_bytes(b"existing")
                        move = {"from": "wiki/note.md", "to": "wiki/new.md", "expected_sha256": sha}
                        self.assertEqual(post("/vault/move", move)[0], 409)
                        self.assertEqual((root / "wiki/new.md").read_bytes(), b"existing")
                        move["to"] = "wiki/moved.md"
                        self.assertEqual(post("/vault/move", move)[0], 200)
                        self.assertFalse(note.exists())
                        self.assertEqual((root / "wiki/moved.md").read_bytes(), b"original\n")
                        self.assertEqual(post("/vault/delete", {"path": "wiki/moved.md", "expected_sha256": sha})[0], 200)
                        self.assertFalse((root / "wiki/moved.md").exists())
                    finally:
                        server.shutdown()
                        server.server_close()
                        thread.join(5)


if __name__ == "__main__":
    unittest.main()
