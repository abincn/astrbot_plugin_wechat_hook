"""Register only the WeChat transport; Agent and reply policy live elsewhere."""
from astrbot.api.star import Context, Star


class Main(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        from .adapter import WeChatHookPlatform  # noqa: F401
