"""AstrBot platform registration and conservative per-group model policy."""
from pathlib import Path
from astrbot.api.star import Context, Star
from astrbot.api.event import filter
from astrbot.core.agent.tool import ToolSet
from .workspace import GroupWorkspace


class Main(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        from .adapter import WeChatHookPlatform  # noqa: F401

    @filter.on_llm_request()
    async def on_llm_request(self, event, req):
        if event.get_platform_name() != "wechat_pc_hook":
            return
        namespace = event.get_extra("wechat_workspace_id")
        if not namespace:
            raise RuntimeError("Missing per-group namespace")
        ws = GroupWorkspace(Path("data/wechat_hook_workspaces"), namespace)
        instructions = ws.instructions()
        if instructions:
            req.system_prompt += "\n\n[Current WeChat group instructions]\n" + instructions
        # Pilot: no MCP/LLM tools, even if AstrBot enables them globally.
        # This filters the request, NOT execution authority for arbitrary plugins.
        req.func_tool = ToolSet()
