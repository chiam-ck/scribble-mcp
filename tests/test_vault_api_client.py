from __future__ import annotations

import hashlib
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import vault_api_client as client


class ClientGuardTests(unittest.TestCase):
    source = "wiki/source.md"
    destination = "wiki/destination.md"
    content = "original café\r\n"

    def setUp(self):
        self.digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        self.receipt = {"ok": True, "from": self.source, "to": self.destination,
                        "sha256": self.digest, "bytes": len(self.content.encode("utf-8")),
                        "logged": True}

    def test_move_sends_digest_of_exact_read_and_preserves_receipt(self):
        with patch.object(client, "read", side_effect=[self.content, None, None, self.content]), \
                patch.object(client, "request", return_value=(200, json.dumps(self.receipt).encode())) as request:
            result = client.move(self.source, self.destination)
        self.assertEqual(request.call_args.kwargs["payload"]["expected_sha256"], self.digest)
        self.assertTrue(result["read_back_verified"])
        self.assertEqual(result["bytes"], self.receipt["bytes"])
        self.assertTrue(result["logged"])

    def test_move_preserves_explicit_stale_digest_without_retry(self):
        stale = "0" * 64
        error = HTTPError("unused", 409, "Conflict", {}, io.BytesIO(b"sha256 mismatch"))
        with patch.object(client, "read", side_effect=[self.content, None]), \
                patch.object(client, "request", side_effect=error) as request:
            with self.assertRaisesRegex(RuntimeError, "409"):
                client.move(self.source, self.destination, stale)
        self.assertEqual(request.call_args.kwargs["payload"]["expected_sha256"], stale)
        request.assert_called_once()

    def test_invalid_explicit_digest_never_falls_back(self):
        for operation in (client.delete, lambda path, sha256: client.move(path, self.destination, sha256)):
            for invalid in ("", "bad", "A" * 64, 0):
                with self.subTest(operation=operation, invalid=invalid), \
                        patch.object(client, "read", return_value=self.content), \
                        patch.object(client, "request") as request:
                    with self.assertRaises(ValueError):
                        operation(self.source, invalid)
                    request.assert_not_called()

    def test_move_refuses_missing_source_before_post(self):
        with patch.object(client, "read", return_value=None), patch.object(client, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "missing"):
                client.move(self.source, self.destination)
            request.assert_not_called()

    def test_move_refuses_existing_destination_before_post(self):
        with patch.object(client, "read", side_effect=[self.content, "another note"]), \
                patch.object(client, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                client.move(self.source, self.destination)
            request.assert_not_called()

    def test_move_checks_destination_bytes_not_just_existence(self):
        with patch.object(client, "read", side_effect=[self.content, None, None, "wrong note"]), \
                patch.object(client, "request", return_value=(200, json.dumps(self.receipt).encode())):
            with self.assertRaisesRegex(RuntimeError, "read-back mismatch"):
                client.move(self.source, self.destination)

    def test_move_checks_source_is_absent(self):
        with patch.object(client, "read", side_effect=[self.content, None, self.content, self.content]), \
                patch.object(client, "request", return_value=(200, json.dumps(self.receipt).encode())):
            with self.assertRaisesRegex(RuntimeError, "read-back mismatch"):
                client.move(self.source, self.destination)

    def test_move_checks_receipt(self):
        for change in ({"sha256": "0" * 64}, {"ok": False}, {"to": "wiki/wrong.md"}):
            with self.subTest(change=change), \
                    patch.object(client, "read", side_effect=[self.content, None]), \
                    patch.object(client, "request", return_value=(200, json.dumps({**self.receipt, **change}).encode())):
                with self.assertRaisesRegex(RuntimeError, "unexpected move receipt"):
                    client.move(self.source, self.destination)

    def test_batch_move_forwards_explicit_digest(self):
        with patch.object(client, "move", return_value={"ok": True}) as move:
            client.run_action({"op": "move", "path": self.source, "to": self.destination, "sha256": self.digest})
        move.assert_called_once_with(self.source, self.destination, self.digest)

    def test_move_cli_accepts_sha256(self):
        with patch("sys.argv", ["vault_api_client.py", "move", self.source, self.destination, "--sha256", self.digest]), \
                patch.object(client, "healthy"), patch.object(client, "move", return_value={"ok": True}) as move, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(client.main(), 0)
        move.assert_called_once_with(self.source, self.destination, self.digest)

    def test_delete_preserves_explicit_digest(self):
        stale = "0" * 64
        error = HTTPError("unused", 409, "Conflict", {}, io.BytesIO(b"sha256 mismatch"))
        with patch.object(client, "read", return_value=self.content), \
                patch.object(client, "request", side_effect=error) as request:
            with self.assertRaisesRegex(RuntimeError, "refused"):
                client.delete(self.source, stale)
        self.assertEqual(request.call_args.kwargs["payload"]["expected_sha256"], stale)
        request.assert_called_once()

    def test_read_refuses_invalid_utf8_instead_of_changing_digest_input(self):
        with patch.object(client, "request", return_value=(200, b"\xff")):
            with self.assertRaises(UnicodeDecodeError):
                client.read(self.source)


if __name__ == "__main__":
    unittest.main()
