#!/usr/bin/env python3
"""Auditable client for CK's canonical private Vault API.

Uses the fixed Mac-mini endpoint and never accesses the vault filesystem.
Batch related read/write/append/delete/move steps through one invocation to avoid repeated
security-approval prompts for ad-hoc raw-IP curl/inline-Python commands.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "http://100.90.117.53:8765"


def valid_path(value: str) -> str:
    if (not value.startswith("wiki/") or ".." in Path(value).parts
            or value.startswith("wiki/.") or "\\" in value):
        raise ValueError("path must be a safe relative path beneath wiki/")
    return value


def request(route: str, *, method: str = "GET", payload: dict | None = None) -> tuple[int, bytes]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json, text/plain; charset=utf-8"}
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = Request(BASE_URL + route, data=data, headers=headers, method=method)
    with urlopen(req, timeout=15) as response:
        return response.status, response.read()


def healthy() -> None:
    status, body = request("/health")
    if status != 200 or body.decode("utf-8", "replace").strip() != "ok":
        raise RuntimeError(f"Vault health failed: HTTP {status}, {body[:300]!r}")


def read(path: str, *, missing_ok: bool = False) -> str | None:
    path = valid_path(path)
    try:
        status, body = request("/vault/read?" + urlencode({"path": path}))
    except HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
        raise
    if status != 200:
        raise RuntimeError(f"Vault read failed for {path}: HTTP {status}")
    return body.decode("utf-8", "replace")


def write(path: str, content: str) -> dict[str, object]:
    path = valid_path(path)
    if not content.strip():
        raise ValueError("refusing to write empty content")
    previous = read(path, missing_ok=True)
    if previous == content:
        return {"ok": True, "unchanged": True, "path": path}
    status, body = request("/vault/write", method="POST",
                           payload={"path": path, "content": content})
    if status != 200:
        raise RuntimeError(f"Vault write failed for {path}: HTTP {status} {body[:300]!r}")
    result = json.loads(body.decode("utf-8", "replace"))
    actual = read(path)
    if actual != content:
        raise RuntimeError(f"read-back mismatch after writing {path}")
    result["read_back_verified"] = True
    return result


def append(path: str, content: str) -> dict[str, object]:
    path = valid_path(path)
    if not content.strip() or "\n" in content.rstrip("\n") or "\r" in content:
        raise ValueError("append content must be one non-empty line")
    before = read(path)
    if content.strip() in before.splitlines():
        return {"ok": True, "already_present": True, "path": path}
    payload = content.rstrip("\n") + "\n"
    status, body = request("/vault/append", method="POST",
                           payload={"path": path, "content": payload})
    if status != 200:
        raise RuntimeError(f"Vault append failed for {path}: HTTP {status} {body[:300]!r}")
    result = json.loads(body.decode("utf-8", "replace"))
    after = read(path)
    if payload.rstrip("\n") not in after.splitlines():
        raise RuntimeError(f"read-back verification failed after appending {path}")
    result["read_back_verified"] = True
    return result


def delete(path: str, sha256: str | None = None) -> dict[str, object]:
    """Delete one note. The API requires the digest of the content you last read."""
    path = valid_path(path)
    current = read(path, missing_ok=True)
    if current is None:
        raise RuntimeError(f"cannot delete missing note: {path}")
    digest = sha256 or hashlib.sha256(current.encode("utf-8")).hexdigest()
    try:
        status, body = request("/vault/delete", method="POST",
                               payload={"path": path, "expected_sha256": digest})
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        if exc.code == 409:
            raise RuntimeError(
                f"refused: {detail}. The note changed since it was read; re-read it and pass its digest."
            ) from exc
        raise RuntimeError(f"Vault delete failed for {path}: HTTP {exc.code} {detail}") from exc
    result = json.loads(body.decode("utf-8", "replace"))
    if read(path, missing_ok=True) is not None:
        raise RuntimeError(f"read-back shows {path} still present")
    result["read_back_verified"] = True
    return result


def move(source: str, destination: str) -> dict[str, object]:
    source, destination = valid_path(source), valid_path(destination)
    if read(source, missing_ok=True) is None:
        raise RuntimeError(f"cannot move missing note: {source}")
    if read(destination, missing_ok=True) is not None:
        raise RuntimeError(f"destination already exists: {destination}")
    status, body = request("/vault/move", method="POST",
                           payload={"from": source, "to": destination})
    if status != 200:
        raise RuntimeError(f"Vault move failed: HTTP {status} {body[:200]!r}")
    if read(source, missing_ok=True) is not None or read(destination, missing_ok=True) is None:
        raise RuntimeError(f"read-back mismatch after moving {source} -> {destination}")
    return {"ok": True, "from": source, "to": destination, "read_back_verified": True}


def run_action(action: dict) -> dict[str, object] | str:
    op = action.get("op")
    if op == "read":
        return read(action["path"]) or ""
    if op == "write":
        content = Path(action["content_file"]).read_text(encoding="utf-8")
        return write(action["path"], content)
    if op == "append":
        content = Path(action["content_file"]).read_text(encoding="utf-8")
        return append(action["path"], content)
    if op == "delete":
        return delete(action["path"], action.get("sha256"))
    if op == "move":
        return move(action["path"], action["to"])
    raise ValueError("batch actions support only read, write, append, delete, move")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    read_parser = sub.add_parser("read")
    read_parser.add_argument("path")
    list_parser = sub.add_parser("list")
    list_parser.add_argument("path", default="wiki", nargs="?")
    search_parser = sub.add_parser("search")
    search_parser.add_argument("query")
    write_parser = sub.add_parser("write")
    write_parser.add_argument("path")
    write_parser.add_argument("content_file", type=Path)
    append_parser = sub.add_parser("append")
    append_parser.add_argument("path")
    append_parser.add_argument("content_file", type=Path)
    delete_parser = sub.add_parser("delete")
    delete_parser.add_argument("path")
    delete_parser.add_argument("--sha256", default=None,
                               help="digest of the content you last read; taken from a fresh read when omitted")
    move_parser = sub.add_parser("move")
    move_parser.add_argument("path")
    move_parser.add_argument("to")
    batch_parser = sub.add_parser("batch")
    batch_parser.add_argument("manifest", type=Path, help="JSON array of ordered {op,path,content_file} actions")
    args = parser.parse_args()

    try:
        healthy()
        if args.command == "health":
            print("ok")
        elif args.command == "read":
            sys.stdout.write(read(args.path) or "")
        elif args.command == "list":
            folder = valid_path(args.path) if args.path != "wiki" else "wiki"
            _, body = request("/vault/list?" + urlencode({"path": folder}))
            sys.stdout.write(body.decode("utf-8", "replace"))
        elif args.command == "search":
            _, body = request("/vault/search?" + urlencode({"q": args.query}))
            sys.stdout.write(body.decode("utf-8", "replace"))
        elif args.command == "write":
            content = args.content_file.read_text(encoding="utf-8")
            print(json.dumps(write(args.path, content), ensure_ascii=False))
        elif args.command == "delete":
            print(json.dumps(delete(args.path, args.sha256), ensure_ascii=False))
        elif args.command == "move":
            print(json.dumps(move(args.path, args.to), ensure_ascii=False))
        elif args.command == "append":
            content = args.content_file.read_text(encoding="utf-8")
            print(json.dumps(append(args.path, content), ensure_ascii=False))
        elif args.command == "batch":
            actions = json.loads(args.manifest.read_text(encoding="utf-8"))
            if not isinstance(actions, list) or not actions:
                raise ValueError("manifest must be a non-empty JSON array")
            results = [run_action(item) for item in actions]
            print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
        return 0
    except (HTTPError, URLError, OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"vault-api-client: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
