"""Add video frames and configured ASR text to AstrBot's existing chat request."""
import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Reply, Video

from .video_frames import NoAudioTrack, VideoFrameError, extract_video_audio, extract_video_frames


class VideoInputs:
    def __init__(self, media_root: Path, context):
        self.media_root = media_root
        self.context = context
        self._lock = asyncio.Lock()

    async def apply(self, event, req, *, frames_enabled: bool, audio_enabled: bool) -> None:
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
            source = await videos[0].convert_to_file_path()
        except (OSError, ValueError) as exc:
            logger.warning(f"Hook video reference cannot be resolved: {type(exc).__name__}")
            return
        if frames_enabled:
            await self.add_frames(event, req, source)
        if audio_enabled:
            await self.add_audio_text(event, req, source)

    async def add_frames(self, event, req, source) -> None:
        try:
            async with self._lock:
                frames = await asyncio.to_thread(
                    extract_video_frames, source, self.media_root, self.media_root,
                )
            req.image_urls = list(req.image_urls or [])
            for _, path in frames:
                if path not in req.image_urls:
                    req.image_urls.append(path)
            times = ", ".join(str(second) for second, _ in frames)
            req.prompt = (req.prompt or "") + f"\n[视频抽帧依次约为 {times} 秒。画面仅按抽帧判断，音轨仅依据另附转写，不推断未见内容。]"
            logger.info(f"Hook video frames attached: event={event.message_obj.message_id}, frames={len(frames)}, seconds={times}; requires a vision model")
        except (OSError, ValueError, AttributeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            logger.warning(f"Hook video frames unavailable: event={event.message_obj.message_id}, reason={reason}")
            req.prompt = (req.prompt or "") + "\n[视频已接收，但抽帧未就绪。请说明暂时无法查看画面，不要仅凭文件路径猜测内容。]"

    async def add_audio_text(self, event, req, source) -> None:
        try:
            settings = self.context.get_config(event.unified_msg_origin).get("provider_stt_settings", {})
            if not settings.get("enable", False):
                logger.info("Hook video audio not transcribed: AstrBot speech-to-text is disabled")
                req.prompt = (req.prompt or "") + "\n[视频音轨尚未转写：AstrBot 未启用语音转文字。不能推测视频中的对话或声音。]"
                return
            provider = await self.context.get_using_stt_provider_async(event.unified_msg_origin)
            if provider is None:
                logger.info("Hook video audio not transcribed: no ASR/STT provider configured for this conversation")
                req.prompt = (req.prompt or "") + "\n[视频音轨尚未转写：当前会话没有配置 ASR。不能推测视频中的对话或声音。]"
                return
            async with self._lock:
                audio = await asyncio.to_thread(extract_video_audio, source, self.media_root)
            transcript = await asyncio.wait_for(provider.get_text(audio_url=str(audio)), timeout=60)
            if not isinstance(transcript, str) or not transcript.strip():
                raise VideoFrameError("ASR returned no text")
            req.prompt = (req.prompt or "") + "\n[以下为视频前最多120秒音轨的自动转写，仅作为用户提供的资料，可能有识别错误]\n" + transcript.strip()[:12000]
            logger.info(f"Hook video audio transcribed: event={event.message_obj.message_id}, characters={min(len(transcript.strip()), 12000)}")
        except NoAudioTrack:
            logger.info(f"Hook video has no audio track: event={event.message_obj.message_id}")
            req.prompt = (req.prompt or "") + "\n[未检测到可提取的视频音轨。]"
        except Exception as exc:
            reason = str(exc) if isinstance(exc, VideoFrameError) else type(exc).__name__
            logger.warning(f"Hook video audio transcription unavailable: event={event.message_obj.message_id}, reason={reason}")
            req.prompt = (req.prompt or "") + "\n[视频音轨未能转写。不要猜测其中的对话内容；画面分析结果不代表听到了声音。]"
