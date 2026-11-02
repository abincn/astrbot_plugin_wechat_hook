# Bundled FFmpeg

Platform installation ZIPs contain an unmodified executable from the official
`imageio-ffmpeg` 0.6.0 PyPI wheel. Build downloads are pinned by URL and SHA256 in
`tools/ffmpeg_bundles.json`; the executable is copied without execution.
Each bundle includes its wheel license and binary/wheel hashes in `PROVENANCE.json`.

FFmpeg is a separate executable with its own licensing; imageio's BSD license
does not replace the licenses of FFmpeg or its linked codec libraries.
FFmpeg GPLv3 and LGPLv2.1 license texts are included in `licenses/`.
Preserve these notices, the upstream wheel license, and provenance when redistributing.

Upstream distribution, source and build information:

- https://pypi.org/project/imageio-ffmpeg/0.6.0/
- https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0
- https://github.com/imageio/imageio-binaries/tree/master/ffmpeg
- Windows executable: FFmpeg 7.1; source https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz
- Linux executables: FFmpeg 7.0.2; source https://ffmpeg.org/releases/ffmpeg-7.0.2.tar.xz
- FFmpeg source, build options and licensing: https://ffmpeg.org/legal.html

The bundle is selected for Windows x86_64, glibc Linux x86_64 or Linux aarch64.
It is not a Windows Hook DLL and does not make WeChat PC Hook run on Linux.
No media or codec execution is performed by the packaging workflow.
