"""Locate a bundled ffmpeg without requiring a system PATH change."""
from functools import lru_cache
import os
from pathlib import Path
import platform
import shutil
import sys


def platform_key() -> str:
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        return "windows-x86_64"
    if sys.platform.startswith("linux"):
        if machine in {"amd64", "x86_64"}:
            return "linux-x86_64"
        if machine in {"aarch64", "arm64"}:
            return "linux-aarch64"
    return "unsupported"


@lru_cache(maxsize=1)
def ffmpeg_executable() -> str:
    key = platform_key()
    suffix = ".exe" if sys.platform == "win32" else ""
    candidates = [Path(__file__).resolve().parent / "vendor" / "ffmpeg" / key / ("ffmpeg" + suffix)]
    try:
        import imageio_ffmpeg
        names = {"windows-x86_64": "ffmpeg-win-x86_64-v7.1.exe",
                 "linux-x86_64": "ffmpeg-linux-x86_64-v7.0.2",
                 "linux-aarch64": "ffmpeg-linux-aarch64-v7.0.2"}
        if key in names:
            candidates.append(Path(imageio_ffmpeg.__file__).resolve().parent / "binaries" / names[key])
    except ImportError:
        pass
    for candidate in candidates:
        if candidate.is_file():
            try:
                if os.name != "nt" and not os.access(candidate, os.X_OK):
                    candidate.chmod(candidate.stat().st_mode | 0o111)
                if os.name == "nt" or os.access(candidate, os.X_OK):
                    return str(candidate)
            except OSError:
                continue
    system = shutil.which("ffmpeg")
    if system:
        return system
    raise ValueError(f"Bundled ffmpeg unavailable for {key}; install the matching plugin package or imageio-ffmpeg wheel")
