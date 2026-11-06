from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
import unittest
from vault_mutations import VaultMutator, MutationConflict

class PreconditionsTests(unittest.TestCase):
    def test_changed_write_and_append_are_rejected_without_commit(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);(root/'wiki').mkdir();p=root/'wiki/log.md';p.write_text('history\n')
            m=VaultMutator(root);old=hashlib.sha256(p.read_bytes()).hexdigest();p.write_text('history\nhuman entry\n')
            for f in (m.write,m.append):
                with self.assertRaises(MutationConflict):f('wiki/log.md','replacement',expected_sha256=old)
                self.assertEqual(p.read_text(),'history\nhuman entry\n')
    def test_concurrent_append_precondition_checked_inside_lock(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);(root/'wiki').mkdir();p=root/'wiki/log.md';p.write_text('history\n')
            old=hashlib.sha256(p.read_bytes()).hexdigest()
            def append(n):
                try:VaultMutator(root).append('wiki/log.md',str(n)+'\n',expected_sha256=old);return True
                except MutationConflict:return False
            with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(append,range(6)))
            self.assertEqual(sum(results),1);self.assertEqual(len(p.read_text().splitlines()),2)
    def test_invalid_hash_cannot_mutate(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);(root/'wiki').mkdir();p=root/'wiki/log.md';p.write_text('history\n')
            for h in ('invalid',123):
                with self.assertRaises(ValueError):VaultMutator(root).append('wiki/log.md','new',expected_sha256=h)
            self.assertEqual(p.read_text(),'history\n')

if __name__=='__main__':unittest.main()
