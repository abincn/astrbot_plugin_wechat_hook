"""Build an install ZIP from an explicit source manifest; no plugin loading."""
from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from bundle_ffmpeg import bundled_files


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "astrbot_plugin_wechat_hook"
FILES = [
    "__init__.py",
    "adapter.py",
    "bridge.py",
    "components.py",
    "contacts.py",
    "delivery.py",
    "drafts.py",
    "events.py",
    "extra_media.py",
    "ffmpeg_runtime.py",
    "identity.py",
    "inbox.py",
    "main.py",
    "outbound.py",
    "payloads.py",
    "quotes.py",
    "media.py",
    "video_frames.py",
    "video_inputs.py",
    "metadata.yaml",
    "README.md",
    "docs/MESSAGE_CAPABILITIES.md",
    "docs/FFMPEG-NOTICE.md",
    "licenses/FFMPEG-GPLv3.txt",
    "licenses/FFMPEG-LGPLv2.1.txt",
    "requirements.txt"
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / (PLUGIN_NAME + ".zip"))
    parser.add_argument("--bundle-platform", choices=("windows-x86_64", "linux-x86_64", "linux-aarch64"))
    parser.add_argument("--wheel-cache", type=Path, default=ROOT.parent / "ffmpeg-wheels")
    args = parser.parse_args()
    sources = [(name, (ROOT / name).read_bytes()) for name in FILES]
    binaries = bundled_files(args.bundle_platform, args.wheel_cache) if args.bundle_platform else []
    if args.bundle_platform:
        # The platform ZIP already contains the executable; don't download a second copy at install time.
        sources = [(name, content.replace(b"\r\n", b"\n").replace(b"imageio-ffmpeg==0.6.0\n", b"") if name == "requirements.txt" else content)
                   for name, content in sources]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in sources:
            archive.writestr(f"{PLUGIN_NAME}/{name}", content)
        for name, content, permissions in binaries:
            info = ZipInfo(f"{PLUGIN_NAME}/{name}")
            info.create_system = 3
            info.external_attr = (0o100000 | permissions) << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
