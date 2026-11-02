"""Prepare video images for AstrBot's existing model request, without calling a model."""
import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Reply, Video

from .video_frames import VideoFrameError, extract_video_frames


class VideoInputs:
    def __init__(self, media_root: Path):
        self.media_root = media_root
        self._lock = asyncio.Lock()

    async def apply(self, event, req) -> None:
        videos = []
        for component in event.message_obj.message:
            if isinstance(component, Video):
                videos.append(component)
            elif isinstance(component, Reply):
                videos.extend(part for part in (component.chain or []) if isinstance(part, Video))
        if not videos or event.get_extra("wechat_video_frames_request") == id(req):
            return
        event.set_extra("wechat_video_frames_request", id(req))
        try:
            source = videos[0].path
            if not source:
                raise VideoFrameError("Video has no verified local path")
            async with self._lock:
                frames = await asyncio.to_thread(
                    extract_video_frames, source, self.media_root, self.media_root,
                )
            req.image_urls = list(req.image_urls or [])
            for _, path in frames:
                if path not in req.image_urls:
                    req.image_urls.append(path)
            times = ", ".join(str(second) for second, _ in frames)
            req.prompt = (req.prompt or "") + f"\n[视频抽帧依次约为 {times} 秒。只根据可见画面分析，不推断音轨或未见内容。]"
            logger.info(f"Hook video frames attached: event={event.message_obj.message_id}, frames={len(frames)}, seconds={times}; requires a vision model")
        except (OSError, ValueError, AttributeError) as exc:
            reason = str(exc) if isinstance(exc, VideoFrameError) else type(exc).__name__
            logger.warning(f"Hook video frames unavailable: event={event.message_obj.message_id}, reason={reason}")
            req.prompt = (req.prompt or "") + "\n[视频已接收，但抽帧未就绪。请说明暂时无法查看画面，不要仅凭文件路径猜测内容。]"
