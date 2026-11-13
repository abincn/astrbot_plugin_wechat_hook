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
            req.prompt = (req.prompt or "") + f"\n[��Ƶ��֡����ԼΪ {times} �롣���������֡�жϣ��������������תд�����ƶ�δ�����ݡ�]"
            logger.info(f"Hook video frames attached: event={event.message_obj.message_id}, frames={len(frames)}, seconds={times}; requires a vision model")
        except (OSError, ValueError, AttributeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            logger.warning(f"Hook video frames unavailable: event={event.message_obj.message_id}, reason={reason}")
            req.prompt = (req.prompt or "") + "\n[��Ƶ�ѽ��գ�����֡δ��������˵����ʱ�޷��鿴���棬��Ҫ��ƾ�ļ�·���²����ݡ�]"

    async def add_audio_text(self, event, req, source) -> None:
        try:
            settings = self.context.get_config(event.unified_msg_origin).get("provider_stt_settings", {})
            if not settings.get("enable", False):
                logger.info("Hook video audio not transcribed: AstrBot speech-to-text is disabled")
                req.prompt = (req.prompt or "") + "\n[��Ƶ������δתд��AstrBot δ��������ת���֡������Ʋ���Ƶ�еĶԻ���������]"
                return
            provider = await self.context.get_using_stt_provider_async(event.unified_msg_origin)
            if provider is None:
                logger.info("Hook video audio not transcribed: no ASR/STT provider configured for this conversation")
                req.prompt = (req.prompt or "") + "\n[��Ƶ������δתд����ǰ�Ựû������ ASR�������Ʋ���Ƶ�еĶԻ���������]"
                return
            async with self._lock:
                audio = await asyncio.to_thread(extract_video_audio, source, self.media_root)
            transcript = await asyncio.wait_for(provider.get_text(audio_url=str(audio)), timeout=60)
            if not isinstance(transcript, str) or not transcript.strip():
                raise VideoFrameError("ASR returned no text")
            req.prompt = (req.prompt or "") + "\n[����Ϊ��Ƶǰ���120��������Զ�תд������Ϊ�û��ṩ�����ϣ�������ʶ�����]\n" + transcript.strip()[:12000]
            logger.info(f"Hook video audio transcribed: event={event.message_obj.message_id}, characters={min(len(transcript.strip()), 12000)}")
        except NoAudioTrack:
            logger.info(f"Hook video has no audio track: event={event.message_obj.message_id}")
            req.prompt = (req.prompt or "") + "\n[δ��⵽����ȡ����Ƶ���졣]"
        except Exception as exc:
            reason = str(exc) if isinstance(exc, VideoFrameError) else type(exc).__name__
            logger.warning(f"Hook video audio transcription unavailable: event={event.message_obj.message_id}, reason={reason}")
            req.prompt = (req.prompt or "") + "\n[��Ƶ����δ��תд����Ҫ�²����еĶԻ����ݣ�����������������������������]"
