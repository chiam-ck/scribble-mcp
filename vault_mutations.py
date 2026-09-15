"""Safe, centrally enforced Markdown mutations for the vault APIs.

The API deliberately edits only the updated/created scalar lines in YAML
frontmatter. It never serializes the YAML document back out, so ordering,
comments, list formatting, and unrelated whitespace remain untouched.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - the deployed targets are POSIX
    fcntl = None  # type: ignore[assignment]

try:
    import yaml
except ImportError:  # pragma: no cover - the Hermes runtime provides PyYAML
    yaml = None  # type: ignore[assignment]


class MetadataError(ValueError):
    """The requested mutation would violate the vault metadata contract."""


@dataclass(frozen=True)
class MutationResult:
    path: str
    operation: str
    metadata_updated: bool
    updated: str | None = None


@dataclass(frozen=True)
class _Field:
    key: str
    start: int
    end: int
    line: str


@dataclass(frozen=True)
class _Frontmatter:
    start: int
    end: int
    fields: dict[str, _Field]
    values: dict[str, object]


_FIELD_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)(?P<sep>[ \t]*:[ \t]*)(?P<rest>.*)$")
_DATE_RE = re.compile(r"^(?P<quote>['\"]?)(?P<date>\d{4}-\d{2}-\d{2})(?P=quote)$")
_REQUIRED_FIELDS = {"title", "created", "updated", "type", "tags", "sources", "related"}
_ALLOWED_TYPES = {"entity", "concept", "comparison", "query"}


class VaultMutator:
    """Mutate vault files with metadata enforcement and serialized commits.

    ``atomic=True`` is the normal local-filesystem mode used by macOS. Linux
    runs against an iCloud FUSE mount where rename-based replacement is not a
    safe assumption, so its API uses ``atomic=False`` for an in-place,
    fully-staged write under the same per-file lock.
    """

    _registry_guard = threading.Lock()

    def __init__(
        self,
        vault_root: Path,
        *,
        today: Callable[[], date] = date.today,
        lock_dir: Path | None = None,
        atomic: bool = True,
    ) -> None:
        self.vault_root = Path(vault_root).resolve()
        self.today = today
        self.lock_dir = Path(lock_dir or os.environ.get("VAULT_LOCK_DIR", os.path.join(tempfile.gettempdir(), "scribble-mcp-vault-locks"))).resolve()
        self.atomic = atomic
        self._thread_locks: dict[str, threading.Lock] = {}

    def append(self, relative_path: str, content: str) -> MutationResult:
        path = self._path(relative_path)
        with self._locked(path):
            existing = path.read_bytes() if path.exists() else b""
            exempt = self._is_exempt(relative_path)
            old_text = self._decode(existing, path) if existing and not exempt else ""
            if not path.exists():
                new_bytes = content.encode("utf-8")
                metadata_updated = False
                updated = None
            elif exempt:
                new_bytes = existing + content.encode("utf-8")
                metadata_updated = False
                updated = None
            else:
                frontmatter = self._frontmatter(old_text, path)
                if frontmatter is None:
                    new_bytes = existing + content.encode("utf-8")
                    metadata_updated = False
                    updated = None
                else:
                    old_updated = self._date_field(frontmatter, "updated", required=True)
                    today = self._today_text()
                    new_text = old_text + content
                    metadata_updated = old_updated != today
                    if metadata_updated:
                        new_frontmatter = self._frontmatter(new_text, path)
                        assert new_frontmatter is not None
                        new_text = self._replace_field(new_text, new_frontmatter, "updated", today)
                    new_bytes = new_text.encode("utf-8")
                    updated = today
            self._commit(path, new_bytes)
        return MutationResult(self._relative(relative_path), "append", metadata_updated, updated)

    def write(self, relative_path: str, content: str) -> MutationResult:
        path = self._path(relative_path)
        with self._locked(path):
            exists = path.exists()
            existing = path.read_bytes() if exists else b""
            exempt = self._is_exempt(relative_path)
            old_text = self._decode(existing, path) if existing and not exempt else ""

            if exempt:
                self._commit(path, content.encode("utf-8"))
                return MutationResult(self._relative(relative_path), "write", False, None)

            if path.suffix.lower() != ".md":
                self._commit(path, content.encode("utf-8"))
                return MutationResult(self._relative(relative_path), "write", False, None)

            old_frontmatter = self._frontmatter(old_text, path) if exists else None
            new_frontmatter = self._frontmatter(content, path)

            if not exists:
                if new_frontmatter is None:
                    raise MetadataError("new Markdown notes require YAML frontmatter")
                self._validate_new_frontmatter(new_frontmatter)
                updated = self._date_field(new_frontmatter, "updated", required=True)
                self._commit(path, content.encode("utf-8"))
                return MutationResult(self._relative(relative_path), "write", False, updated)

            if old_frontmatter is None:
                self._commit(path, content.encode("utf-8"))
                return MutationResult(self._relative(relative_path), "write", False, None)

            if new_frontmatter is None:
                raise MetadataError("overwriting a frontmatter note requires frontmatter in the replacement")

            # Validate the replacement before touching the old file. Existing
            # notes may contain additional legacy fields, so only the required
            # date fields are enforced on overwrite.
            old_created = self._date_field(old_frontmatter, "created", required=False)
            new_created = self._date_field(new_frontmatter, "created", required=False)
            if old_created is not None:
                if new_created is None:
                    raise MetadataError("frontmatter missing created field")
            old_updated = self._date_field(old_frontmatter, "updated", required=True)
            self._date_field(new_frontmatter, "updated", required=True)

            new_text = content
            if old_created is not None:
                new_frontmatter = self._frontmatter(new_text, path)
                assert new_frontmatter is not None
                new_text = self._replace_field(new_text, new_frontmatter, "created", old_created)
            new_frontmatter = self._frontmatter(new_text, path)
            assert new_frontmatter is not None
            today = self._today_text()
            new_text = self._replace_field(new_text, new_frontmatter, "updated", today)
            self._commit(path, new_text.encode("utf-8"))
            return MutationResult(self._relative(relative_path), "write", old_updated != today, today)

    def _path(self, relative_path: str) -> Path:
        relative = relative_path.replace("\\", "/")
        if not relative or relative.startswith("/") or "\x00" in relative:
            raise ValueError("relative vault path required")
        path = (self.vault_root / relative).resolve()
        try:
            path.relative_to(self.vault_root)
        except ValueError as exc:
            raise ValueError("path traversal rejected") from exc
        return path

    def _relative(self, relative_path: str) -> str:
        return relative_path.replace("\\", "/")

    @staticmethod
    def _decode(data: bytes, path: Path) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if path.suffix.lower() == ".md":
                raise MetadataError("Markdown file is not valid UTF-8") from exc
            raise

    @staticmethod
    def _is_exempt(relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/").lstrip("./")
        if normalized in {"wiki/log.md", "wiki/index.md", "wiki/SCHEMA.md"}:
            return True
        if normalized.startswith("wiki/raw/"):
            return True
        return not normalized.lower().endswith(".md")

    def _today_text(self) -> str:
        return self.today().isoformat()

    def _frontmatter(self, text: str, path: Path) -> _Frontmatter | None:
        prefix = "\ufeff" if text.startswith("\ufeff") else ""
        offset = len(prefix)
        if not text[offset:].startswith("---"):
            return None
        first_end = text.find("\n", offset)
        if first_end < 0:
            raise MetadataError("malformed YAML frontmatter: missing opening newline")
        opening = text[offset:first_end].rstrip("\r")
        if opening != "---":
            return None

        body_start = first_end + 1
        cursor = body_start
        closing_start: int | None = None
        closing_end: int | None = None
        while cursor <= len(text):
            line_end = text.find("\n", cursor)
            if line_end < 0:
                line_end = len(text)
                next_cursor = len(text)
            else:
                next_cursor = line_end + 1
            line = text[cursor:line_end].rstrip("\r")
            if line == "---":
                closing_start = cursor
                closing_end = next_cursor
                break
            if next_cursor == len(text) and line_end == len(text):
                break
            cursor = next_cursor
        if closing_start is None or closing_end is None:
            raise MetadataError("malformed YAML frontmatter: missing closing delimiter")

        block = text[body_start:closing_start]
        values = self._safe_yaml(block, path)
        fields: dict[str, _Field] = {}
        cursor = body_start
        for raw_line in block.splitlines(keepends=True):
            line = raw_line.rstrip("\r\n")
            match = _FIELD_RE.match(line)
            if match and not match.group("indent"):
                key = match.group("key")
                if key in fields:
                    raise MetadataError(f"malformed YAML frontmatter: duplicate {key} field")
                fields[key] = _Field(key, cursor, cursor + len(line), line)
            cursor += len(raw_line)
        return _Frontmatter(body_start, closing_start, fields, values)

    @staticmethod
    def _safe_yaml(block: str, path: Path) -> dict[str, object]:
        if yaml is None:
            # The production runtime includes PyYAML. This fallback still
            # rejects non-mapping frontmatter and malformed root lines rather
            # than silently mutating an unknown document.
            values: dict[str, object] = {}
            for line in block.splitlines():
                if not line.strip() or line.lstrip().startswith("#") or line[:1].isspace():
                    continue
                match = _FIELD_RE.match(line)
                if not match:
                    raise MetadataError("malformed YAML frontmatter")
                values[match.group("key")] = match.group("rest").strip()
            return values
        try:
            parsed = yaml.safe_load(block) if block.strip() else {}
        except yaml.YAMLError as exc:
            raise MetadataError("malformed YAML frontmatter") from exc
        if not isinstance(parsed, dict):
            raise MetadataError("YAML frontmatter must be a mapping")
        return parsed

    @staticmethod
    def _raw_value(field: _Field) -> str:
        match = _FIELD_RE.match(field.line)
        assert match is not None
        rest = match.group("rest")
        comment = VaultMutator._comment_start(rest)
        return rest[:comment].strip() if comment is not None else rest.strip()

    @staticmethod
    def _comment_start(value: str) -> int | None:
        quote: str | None = None
        for index, character in enumerate(value):
            if character in {"'", '"'}:
                if quote == character:
                    quote = None
                elif quote is None:
                    quote = character
            elif character == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
                return index
        return None

    def _date_field(self, frontmatter: _Frontmatter, key: str, *, required: bool) -> str | None:
        field = frontmatter.fields.get(key)
        if field is None:
            if required:
                raise MetadataError(f"frontmatter missing {key} field")
            return None
        raw = self._raw_value(field)
        match = _DATE_RE.fullmatch(raw)
        if not match:
            raise MetadataError(f"frontmatter {key} must be YYYY-MM-DD")
        try:
            date.fromisoformat(match.group("date"))
        except ValueError as exc:
            raise MetadataError(f"frontmatter {key} must be a real date") from exc
        return match.group("date")

    def _validate_new_frontmatter(self, frontmatter: _Frontmatter) -> None:
        missing = sorted(_REQUIRED_FIELDS - frontmatter.fields.keys())
        if missing:
            raise MetadataError("new Markdown frontmatter missing required fields: " + ", ".join(missing))
        title = frontmatter.values.get("title")
        if not isinstance(title, str) or not title.strip():
            raise MetadataError("frontmatter title must be a non-empty string")
        note_type = frontmatter.values.get("type")
        if note_type not in _ALLOWED_TYPES:
            raise MetadataError("frontmatter type is not allowed by wiki/SCHEMA.md")
        for key in ("tags", "sources", "related"):
            if not isinstance(frontmatter.values.get(key), list):
                raise MetadataError(f"frontmatter {key} must be a YAML list")
        self._date_field(frontmatter, "created", required=True)
        self._date_field(frontmatter, "updated", required=True)

    @staticmethod
    def _replace_field(text: str, frontmatter: _Frontmatter, key: str, value: str) -> str:
        field = frontmatter.fields.get(key)
        if field is None:
            raise MetadataError(f"frontmatter missing {key} field")
        match = _FIELD_RE.match(field.line)
        assert match is not None
        rest = match.group("rest")
        comment_index = VaultMutator._comment_start(rest)
        scalar = rest if comment_index is None else rest[:comment_index]
        comment = "" if comment_index is None else rest[comment_index:]
        leading = scalar[: len(scalar) - len(scalar.lstrip())]
        trailing = scalar[len(scalar.rstrip()):]
        current = scalar.strip()
        quote = current[0] if len(current) >= 2 and current[0] in {"'", '"'} and current[-1] == current[0] else ""
        replacement = leading + (quote + value + quote if quote else value) + trailing + comment
        line = match.group("indent") + match.group("key") + match.group("sep") + replacement
        return text[:field.start] + line + text[field.end:]

    @contextmanager
    def _locked(self, path: Path) -> Iterator[None]:
        key = str(path)
        with self._registry_guard:
            thread_lock = self._thread_locks.setdefault(key, threading.Lock())
        thread_lock.acquire()
        lock_handle = None
        try:
            self.lock_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            lock_path = self.lock_dir / f"{digest}.lock"
            lock_handle = open(lock_path, "a+b")
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if lock_handle is not None:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
            thread_lock.release()

    def _replace(self, temporary: Path, target: Path) -> None:
        os.replace(temporary, target)

    def _commit(self, path: Path, data: bytes) -> None:
        if self.atomic:
            self._atomic_write(path, data)
        else:
            self._direct_write(path, data)

    @staticmethod
    def _direct_write(path: Path, data: bytes) -> None:
        """Write complete staged bytes without rename, for FUSE mounts."""
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+b" if path.exists() else "wb"
        with path.open(mode) as handle:
            handle.seek(0)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            handle.truncate()

    def _atomic_write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temporary = Path(temporary_name)
        try:
            if path.exists():
                os.fchmod(fd, stat.S_IMODE(path.stat().st_mode))
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace(temporary, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some filesystems do not allow fsync on directories. The
                # file replacement itself remains atomic and already durable.
                pass
        finally:
            if fd != -1:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
