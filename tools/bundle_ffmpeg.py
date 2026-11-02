"""Package pinned upstream ffmpeg bytes; never execute the downloaded binary."""
import hashlib
import io
import json
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile


def bundled_files(platform: str, cache: Path):
    manifest = json.loads(Path(__file__).with_name("ffmpeg_bundles.json").read_text(encoding="utf-8"))
    entry = manifest[platform]
    cache.mkdir(parents=True, exist_ok=True)
    wheel = cache / entry["url"].rsplit("/", 1)[1]
    if not wheel.exists():
        temporary = wheel.with_suffix(".download")
        size = 0
        with urlopen(entry["url"], timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 40 * 1024 * 1024:
                    raise ValueError("ffmpeg distribution exceeds package size limit")
                output.write(chunk)
        temporary.replace(wheel)
    data = wheel.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError("ffmpeg wheel SHA256 differs from pinned upstream release")
    with ZipFile(io.BytesIO(data)) as source:
        binary = source.read(entry["member"])
        license_text = source.read("imageio_ffmpeg-0.6.0.dist-info/LICENSE")
    prefix = "vendor/ffmpeg/" + platform + "/"
    provenance = {"distribution": "imageio-ffmpeg==0.6.0", "wheel_url": entry["url"],
                  "wheel_sha256": entry["sha256"], "upstream_member": entry["member"],
                  "binary_sha256": hashlib.sha256(binary).hexdigest()}
    return [(prefix + entry["target"], binary, 0o755),
            (prefix + "IMAGEIO-LICENSE.txt", license_text, 0o644),
            (prefix + "PROVENANCE.json", json.dumps(provenance, indent=2).encode(), 0o644)]
