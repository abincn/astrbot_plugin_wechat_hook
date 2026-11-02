"""Restricted, per-group data files (not a sandbox for arbitrary Python plugins)."""
from __future__ import annotations
import os
from pathlib import Path

ALLOWED = {"SOUL.md", "AGENTS.md", "MEMORY.md", "USER.md"}
LIMIT = 64 * 1024

class GroupWorkspace:
    def __init__(self, root: str | Path, namespace: str):
        if len(namespace) != 32 or any(ch not in "0123456789abcdef" for ch in namespace):
            raise ValueError("Invalid workspace namespace")
        self.root = Path(root).resolve()
        self.folder = self.root / namespace
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.folder.is_symlink() or not self.folder.resolve().is_relative_to(self.root):
            raise ValueError("Unsafe group folder")

    def _file(self, filename: str) -> Path:
        if filename not in ALLOWED:
            raise ValueError("Unknown or unsafe workspace document")
        dest = self.folder / filename
        if dest.is_symlink() or not dest.resolve().is_relative_to(self.folder.resolve()):
            raise ValueError("Unsafe document path")
        return dest

    def read(self, filename: str) -> str:
        dest = self._file(filename)
        if not dest.exists():
            return ""
        if dest.stat().st_size > LIMIT:
            raise ValueError("Workspace document too large")
        return dest.read_text(encoding="utf-8")

    def write(self, filename: str, content: str) -> None:
        dest = self._file(filename)
        if not isinstance(content, str) or len(content.encode("utf-8")) > LIMIT:
            raise ValueError("Workspace content exceeds limit")
        tmp = self.folder / ("." + filename + ".tmp")
        if tmp.is_symlink():
            raise ValueError("Unsafe temporary file")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, dest)

    def instructions(self) -> str:
        return "\n\n".join(
            f"[{name}]\n{value}" for name in ("SOUL.md", "AGENTS.md", "MEMORY.md")
            if (value := self.read(name))
        )
