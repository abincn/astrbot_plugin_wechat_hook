"""WeChat transport with optional video media preparation; group policy lives elsewhere."""
from astrbot.api.event import filter
from astrbot.api.star import Context, Star


class Main(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        from .adapter import WeChatHookPlatform
        self._platform_type = WeChatHookPlatform

    @filter.on_llm_request()
    async def on_llm_request(self, event, req):
        adapter = getattr(event, "adapter", None)
        if isinstance(adapter, self._platform_type):
            await adapter.prepare_video_input(event, req, self.context)
