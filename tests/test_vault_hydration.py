"""Regression tests for File Provider failures at the HTTP boundary."""
from __future__ import annotations

import errno
import threading
import tempfile
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import vault_api_macos as api
from vault_mutations import VaultMutator


@patch('vault_reads.sys.platform', 'darwin')
class HydrationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'wiki' / 'fixture.txt'
        self.path.parent.mkdir()
        self.content = b'Exact original content.\n'
        self.path.write_bytes(self.content)
        self.root_patch = patch.object(api, 'VAULT', self.root)
        self.root_patch.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), api.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/vault/read?path=wiki/fixture.txt'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.root_patch.stop()
        self.temp.cleanup()

    def test_read_recovers_cloud_only_file_without_changing_content(self):
        original_open = Path.open
        def cloud_open(path, *args, **kwargs):
            if path == self.path:
                raise OSError(errno.EDEADLK, 'Resource deadlock avoided')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', cloud_open), patch('subprocess.run', return_value=CompletedProcess([], 0, self.content, b'')) as coordinator:
            try:
                response = urllib.request.urlopen(self.url, timeout=5)
                status, body = response.status, response.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 200, body)
        self.assertEqual(body, self.content)
        coordinator.assert_called_once()
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_hydration_timeout_is_retryable_503(self):
        from subprocess import TimeoutExpired
        original_open = Path.open
        def cloud_open(path, *args, **kwargs):
            if path == self.path:
                raise OSError(errno.EDEADLK, 'Resource deadlock avoided')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', cloud_open), patch('subprocess.run', side_effect=TimeoutExpired('coordinator', 15)):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.url, timeout=5)
        self.assertEqual(error.exception.code, 503)
        self.assertEqual(error.exception.headers['Retry-After'], '3')
        self.assertIn(b'hydration timed out', error.exception.read())
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_permission_failure_is_not_retried_as_hydration(self):
        original_open = Path.open
        def denied_open(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError(errno.EACCES, 'denied')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', denied_open), patch('subprocess.run') as coordinator:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.url, timeout=5)
        self.assertEqual(error.exception.code, 403)
        coordinator.assert_not_called()

    def test_existing_note_write_hydrates_before_preserving_created(self):
        from vault_reads import read_vault_bytes
        original = b'---\ntitle: Fixture\ncreated: 2026-01-01\nupdated: 2026-01-02\n---\nOld body.\n'
        note_path = self.root / 'wiki' / 'note.md'
        note_path.write_bytes(original)
        mutator = VaultMutator(self.root, read_bytes=read_vault_bytes)
        original_open = Path.open
        def cloud_open(path, *args, **kwargs):
            if path == note_path and args and args[0] == 'rb':
                raise OSError(errno.EDEADLK, 'Resource deadlock avoided')
            return original_open(path, *args, **kwargs)
        replacement = original.decode().replace('2026-01-01', '2026-09-01').replace('Old body.', 'New body.')
        with patch.object(Path, 'open', cloud_open), patch('subprocess.run', return_value=CompletedProcess([], 0, original, b'')):
            mutator.write('wiki/note.md', replacement)
        actual = note_path.read_text()
        self.assertIn('created: 2026-01-01', actual)
        self.assertIn('New body.', actual)

    def test_existing_note_write_timeout_leaves_original_untouched(self):
        from vault_reads import read_vault_bytes
        from subprocess import TimeoutExpired
        mutator = VaultMutator(self.root, read_bytes=read_vault_bytes)
        original_open = Path.open
        def cloud_open(path, *args, **kwargs):
            if path == self.path:
                raise OSError(errno.EDEADLK, 'Resource deadlock avoided')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', cloud_open), patch('subprocess.run', side_effect=TimeoutExpired('coordinator', 15)):
            with self.assertRaisesRegex(RuntimeError, 'hydration timed out'):
                mutator.write('wiki/fixture.txt', 'Do not commit this')
        self.assertEqual(self.path.read_bytes(), self.content)


if __name__ == '__main__':
    unittest.main()
