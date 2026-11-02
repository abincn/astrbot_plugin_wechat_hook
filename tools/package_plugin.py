"""Build an install ZIP from an explicit source manifest; no plugin loading."""
from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "astrbot_plugin_wechat_hook"
FILES = [
    "__init__.py",
    "adapter.py",
    "bridge.py",
    "delivery.py",
    "drafts.py",
    "events.py",
    "identity.py",
    "inbox.py",
    "main.py",
    "media.py",
    "metadata.yaml",
    "README.md",
    "requirements.txt"
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / (PLUGIN_NAME + ".zip"))
    args = parser.parse_args()
    sources = [(name, (ROOT / name).read_bytes()) for name in FILES]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in sources:
            archive.writestr(f"{PLUGIN_NAME}/{name}", content)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
