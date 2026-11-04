"""UI-47: keep the AI's answers as they arrive so a stopped pass can resume.

One small JSON file per book, next to the book's other per-book state (the
persistent-rules sidecar). Inside it, answers are grouped per input file
(content hash) and keyed per row by a hash of everything the AI was given for
that row, so a changed row is simply asked again.

Only successful, mapped answers are kept. No API key, no provider name and no
row description is ever written: a row key is a one-way hash.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, Optional

_VERSION = 1


def file_hash(path: str) -> str:
    """SHA-256 of the input file's bytes ('' when it cannot be read)."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def row_key(*parts: object) -> str:
    """Hash of everything the AI was given for one row."""
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", errors="replace"))
        h.update(b"\x1f")
    return h.hexdigest()


def store_path(book_dir: Path, book_stem: str) -> Path:
    return Path(book_dir) / f"{book_stem}_ai_answers.json"


class KeptAnswers:
    """The kept answers of ONE book for ONE input file."""

    def __init__(self, path: Path, book: str, input_hash: str):
        self.path = Path(path)
        self.book = book
        self.input_hash = input_hash
        self._rows: Dict[str, Dict] = {}
        self._load()

    # -- storage ---------------------------------------------------------
    def _read_all(self) -> Dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != _VERSION or data.get("book") != self.book:
            return {}
        files = data.get("files")
        return files if isinstance(files, dict) else {}

    def _load(self) -> None:
        rows = self._read_all().get(self.input_hash) if self.input_hash else None
        self._rows = dict(rows) if isinstance(rows, dict) else {}

    def _write(self) -> None:
        files = self._read_all()
        if self._rows:
            files[self.input_hash] = self._rows
        else:
            files.pop(self.input_hash, None)
        try:
            if not files:
                if self.path.exists():
                    self.path.unlink()
                return
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"version": _VERSION, "book": self.book, "files": files},
                                      indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass   # keeping answers is a convenience; never fail the run

    # -- use -------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.input_hash)

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, key: str) -> Optional[Dict]:
        v = self._rows.get(key)
        return v if isinstance(v, dict) and v.get("account") else None

    def put(self, key: str, account: str, reason: str) -> None:
        """Keep one successful mapped answer (never a skip, failure or Suspense)."""
        if not self.enabled or not account:
            return
        self._rows[key] = {"account": account, "reason": reason}
        self._write()

    def drop(self, key: str) -> None:
        if self._rows.pop(key, None) is not None:
            self._write()

    def clear(self) -> None:
        """A fully finished pass: nothing is kept for this book and file."""
        self._rows = {}
        if self.enabled:
            self._write()
