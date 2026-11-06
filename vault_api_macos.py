#!/usr/bin/env python3
"""Dependency-free HTTP API for the native macOS iCloud Obsidian vault."""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from vault_mutations import MutationResult, MetadataError, MutationConflict, VaultMutator
from vault_reads import read_vault_bytes, read_vault_text

VAULT = Path(
    os.environ.get(
        "VAULT_ROOT",
        "/home/ck/icloud-linux-mount/Obsidian/Ck's Vault",
    )
).expanduser()
MOUNT = Path(
    os.environ.get("VAULT_MOUNT", "/home/ck/icloud-linux-mount")
).expanduser()
SERVICE = os.environ.get("VAULT_SYNC_SERVICE", "icloud.service")
MAX_BODY = 20 * 1024 * 1024
MUTATOR = VaultMutator(VAULT, read_bytes=read_vault_bytes)
# Deletion is opt-in and narrow: only notes beneath wiki/, and never the files
# that hold the vault's own structure.
DELETE_ROOT = "wiki/"
DELETE_PROTECTED = {
    "wiki/index.md",
    "wiki/SCHEMA.md",
    "wiki/log.md",
    "wiki/log-2026-04_09.md",
}


def audit_delete(relative_path: str, digest: str, size: int) -> bool:
    """Best-effort audit line; a failed log append never resurrects the file."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"{stamp} \u2014 [vault-api] deleted {relative_path} sha256={digest} bytes={size}"
    try:
        MUTATOR.append("wiki/log.md", line)
        return True
    except Exception:
        return False



def service_ready() -> tuple[bool, str]:
    """Check the vault backend without assuming Linux systemd.

    Ubuntu production uses the iCloud FUSE mount and user systemd service.
    macOS uses the native iCloud File Provider path, so the directory and its
    wiki root are the readiness boundary instead.
    """
    if not VAULT.is_dir() or not (VAULT / "wiki").is_dir():
        return False, "vault root or wiki directory is unavailable"

    if sys.platform == "darwin" or os.environ.get("VAULT_SKIP_MOUNT_CHECK") == "1":
        return True, "ok"

    if not MOUNT.is_mount():
        return False, "iCloud mount is unavailable"
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", SERVICE],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return False, f"{SERVICE} is not active"
    return True, "ok"


def safe_path(value: str, *, allow_empty: bool = False) -> Path:
    value = unquote(value or "")
    if not value and allow_empty:
        return VAULT
    if not value or value.startswith("/") or "\x00" in value:
        raise ValueError("relative vault path required")
    candidate = (VAULT / value).resolve()
    try:
        candidate.relative_to(VAULT.resolve())
    except ValueError as exc:
        raise ValueError("path traversal rejected") from exc
    return candidate


def relative(path: Path) -> str:
    return path.relative_to(VAULT.resolve()).as_posix()


def json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length > MAX_BODY:
        raise ValueError("request body too large")
    raw = handler.rfile.read(length) if length else b"{}"
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "VaultAPI/1.0"

    def reply(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if code == 503:
            self.send_header("Retry-After", "3")
        self.end_headers()
        self.wfile.write(payload)

    def error(self, code: int, message: str) -> None:
        self.reply(code, message + "\n", "text/plain; charset=utf-8")

    def json_reply(self, code: int, payload: dict[str, object]) -> None:
        self.reply(code, json.dumps(payload, ensure_ascii=False) + "\n", "application/json; charset=utf-8")

    def require_ready(self) -> None:
        ready, reason = service_ready()
        if not ready:
            raise RuntimeError(reason)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if parsed.path == "/health":
                ready, reason = service_ready()
                self.reply(200 if ready else 503, reason + "\n")
                return
            self.require_ready()
            if parsed.path == "/vault/read":
                path = safe_path(query.get("path", [""])[0])
                self.reply(200, read_vault_text(path))
            elif parsed.path == "/vault/list":
                path = safe_path(query.get("path", [""])[0], allow_empty=True)
                if not path.is_dir():
                    raise ValueError("directory required")
                entries = []
                for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                    suffix = "/" if child.is_dir() else ""
                    entries.append(relative(child) + suffix)
                self.reply(200, "\n".join(entries) + ("\n" if entries else ""))
            elif parsed.path == "/vault/search":
                needle = query.get("q", [""])[0]
                if not needle:
                    raise ValueError("search query required")
                base = safe_path(query.get("path", [""])[0], allow_empty=True)
                pattern = re.compile(needle)
                roots = [base] if base.is_file() else base.rglob("*")
                matches = []
                for candidate in roots:
                    if not candidate.is_file() or candidate.name.startswith("."):
                        continue
                    try:
                        text = read_vault_text(candidate)
                    except OSError:
                        continue
                    if pattern.search(text):
                        matches.append(relative(candidate))
                self.reply(200, "\n".join(sorted(matches)) + ("\n" if matches else ""))
            else:
                self.error(404, "not found")
        except (ValueError, json.JSONDecodeError) as exc:
            self.error(400, str(exc))
        except (FileNotFoundError, NotADirectoryError) as exc:
            self.error(404, str(exc))
        except PermissionError as exc:
            self.error(403, str(exc))
        except RuntimeError as exc:
            self.error(503, str(exc))
        except Exception as exc:
            self.error(500, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            self.require_ready()
            data = json_body(self)
            if parsed.path == "/vault/write":
                path = safe_path(str(data["path"]))
                result = MUTATOR.write(relative(path), str(data["content"]), expected_sha256=data.get("expected_sha256"))
                self.json_reply(200, mutation_payload(result))
            elif parsed.path == "/vault/append":
                path = safe_path(str(data["path"]))
                result = MUTATOR.append(relative(path), str(data["content"]), expected_sha256=data.get("expected_sha256"))
                self.json_reply(200, mutation_payload(result))
            elif parsed.path == "/vault/delete":
                path = safe_path(str(data["path"]))
                target = relative(path)
                if not target.startswith(DELETE_ROOT):
                    raise ValueError("delete is limited to paths beneath wiki/")
                if target in DELETE_PROTECTED:
                    raise ValueError(f"protected path: {target}")
                if path.is_dir():
                    raise ValueError("delete refuses directories")
                if path.suffix != ".md":
                    raise ValueError("delete refuses non-Markdown files")
                if not path.is_file():
                    raise FileNotFoundError(target)
                expected = data.get("expected_sha256")
                if expected is None:
                    raise ValueError("expected_sha256 is required for delete")
                if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise ValueError("expected_sha256 must be a SHA256 hex digest")
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if digest != expected:
                    raise MutationConflict(
                        f"sha256 mismatch for {target}: current {digest}"
                    )
                path.unlink()
                logged = audit_delete(target, digest, len(raw))
                self.json_reply(200, {
                    "ok": True,
                    "deleted": target,
                    "sha256": digest,
                    "bytes": len(raw),
                    "logged": logged,
                })
            elif parsed.path == "/vault/move":
                source = safe_path(str(data["from"]))
                destination = safe_path(str(data["to"]))
                if destination.exists():
                    raise FileExistsError("destination already exists")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.rename(destination)
                self.reply(200, "ok\n")
            else:
                self.error(404, "not found")
        except MutationConflict as exc:
            self.error(409, str(exc))
        except (KeyError, ValueError, json.JSONDecodeError, MetadataError) as exc:
            self.error(400, str(exc))
        except (FileNotFoundError, NotADirectoryError) as exc:
            self.error(404, str(exc))
        except FileExistsError as exc:
            self.error(409, str(exc))
        except PermissionError as exc:
            self.error(403, str(exc))
        except RuntimeError as exc:
            self.error(503, str(exc))
        except Exception as exc:
            self.error(500, str(exc))

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("vault-api: " + (fmt % args) + "\n")


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VAULT_API_HOST", "127.0.0.1")
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("VAULT_API_PORT", "8765"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def mutation_payload(result: MutationResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "path": result.path,
        "operation": result.operation,
        "metadata_updated": result.metadata_updated,
    }
    if result.updated is not None:
        payload["updated"] = result.updated
    return payload


if __name__ == "__main__":
    main()
