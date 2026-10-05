"""Read canonical vault files, materializing macOS File Provider placeholders.

No mirror, content writes, or stale fallback. Only EDEADLK triggers a native
coordinated read; permission and missing-file errors keep their meaning.
"""
from __future__ import annotations

import errno
import subprocess
import sys
from pathlib import Path

COORDINATOR = Path(__file__).with_name('vault_coordinated_read')
HYDRATION_TIMEOUT = 15


def read_vault_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        if sys.platform != 'darwin' or exc.errno != errno.EDEADLK:
            raise
    try:
        result = subprocess.run(
            [str(COORDINATOR), str(path)],
            capture_output=True,
            timeout=HYDRATION_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('iCloud file hydration timed out; retry this read later') from exc
    except OSError as exc:
        raise RuntimeError('iCloud coordinated-read helper unavailable') from exc
    if result.returncode != 0:
        diagnostic = result.stderr.decode('utf-8', errors='replace').strip()[:500]
        raise RuntimeError('iCloud coordinated read failed: ' + diagnostic)
    return result.stdout


def read_vault_text(path: Path) -> str:
    return read_vault_bytes(path).decode('utf-8', errors='replace')
