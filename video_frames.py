"""Bounded local video frames shared by group/private and quoted messages."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
import wave

from .ffmpeg_runtime import ffmpeg_executable


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
    executable = ffmpeg_executable()
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


class NoAudioTrack(VideoFrameError):
    pass


def extract_video_audio(source: str, media_root: Path) -> Path:
    path = Path(source).resolve(strict=True)
    if (not path.is_relative_to(media_root.resolve())
            or not re.fullmatch(r"[0-9a-f]{64}", path.stem)
            or path.suffix not in {".mp4", ".webm"}
            or not 0 < path.stat().st_size <= 60 * 1024 * 1024):
        raise VideoFrameError("Video is outside the verified Hook media store")
    folder = media_root / "video_frames" / path.stem
    if folder.is_symlink() or not folder.resolve().is_relative_to(media_root.resolve()):
        raise VideoFrameError("Unsafe audio directory")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "audio.wav"
    if target.is_symlink():
        raise VideoFrameError("Unsafe audio path")
    with tempfile.TemporaryDirectory(dir=folder) as temporary:
        output = Path(temporary) / "audio.wav"
        errors = Path(temporary) / "decode.log"
        try:
            with errors.open("wb") as stderr:
                result = subprocess.run(
                    [ffmpeg_executable(), "-nostdin", "-hide_banner", "-loglevel", "error",
                     "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:a:0",
                     "-vn", "-t", "120", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(output)],
                    timeout=30, stdout=subprocess.DEVNULL, stderr=stderr, check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VideoFrameError("Video audio decode unavailable or timed out") from exc
        if result.returncode != 0:
            with errors.open("rb") as stream:
                detail = stream.read(1024).lower()
            if b"matches no streams" in detail or b"does not contain any stream" in detail:
                raise NoAudioTrack("Video has no available audio track")
            raise VideoFrameError("Video audio extraction failed")
        if not output.is_file() or not 44 < output.stat().st_size <= 4 * 1024 * 1024:
            raise VideoFrameError("Video audio size invalid")
        with wave.open(str(output), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000) or not 0 < audio.getnframes() <= 120 * 16000:
                raise VideoFrameError("Video audio format invalid")
        output.replace(target)
    return target
