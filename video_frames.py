"""Bounded local video frames shared by group/private and quoted messages."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


class VideoFrameError(ValueError):
    pass


def extract_video_frames(source: str, media_root: Path, workspace: Path) -> list[tuple[int, str]]:
    path = Path(source).resolve(strict=True)
    if (not path.is_relative_to(media_root.resolve())
            or not re.fullmatch(r"[0-9a-f]{64}", path.stem)
            or path.suffix not in {".mp4", ".webm"}
            or not 0 < path.stat().st_size <= 60 * 1024 * 1024):
        raise VideoFrameError("Video is outside the verified Hook media store")
    output_root = workspace / "video_frames" / path.stem
    if output_root.is_symlink() or not output_root.resolve().is_relative_to(workspace.resolve()):
        raise VideoFrameError("Unsafe frame directory")
    output_root.mkdir(parents=True, exist_ok=True)
    executable = shutil.which("ffmpeg")
    frames = []
    for second in (0, 3, 9):
        target = output_root / f"{second}.jpg"
        if target.is_symlink():
            raise VideoFrameError("Unsafe frame path")
        if target.is_file() and 0 < target.stat().st_size <= 1500000:
            with target.open("rb") as stream:
                if stream.read(3) == bytes.fromhex("ffd8ff"):
                    frames.append((second, str(target)))
                    continue
        if not executable:
            raise VideoFrameError("ffmpeg is required for video frame analysis")
        with tempfile.TemporaryDirectory(dir=output_root) as temporary:
            output = Path(temporary) / "frame.jpg"
            try:
                result = subprocess.run(
                    [executable, "-nostdin", "-hide_banner", "-loglevel", "error",
                     "-protocol_whitelist", "file,pipe", "-ss", str(second),
                     "-i", str(path), "-frames:v", "1", "-vf", "scale='min(960,iw)':-2",
                     "-y", str(output)],
                    timeout=12, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise VideoFrameError("ffmpeg unavailable or decoding timed out") from exc
            if result.returncode == 0 and output.is_file() and 0 < output.stat().st_size <= 1500000:
                output.replace(target)
                frames.append((second, str(target)))
    if not frames:
        raise VideoFrameError("No usable video frames were extracted")
    return frames
