"""WeChat transport with optional video media preparation; group policy lives elsewhere."""
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.api import logger as log
from .responses_files import FileProtocolError, probe_file_protocol


class Main(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        from .adapter import WeChatHookPlatform
        self._platform_type = WeChatHookPlatform

    @filter.on_llm_request()
    async def on_llm_request(self, event, req):
        adapter = getattr(event, "adapter", None)
        if isinstance(adapter, self._platform_type):
            try:
                await adapter.prepare_file_upload(event, req, self.context)
            except (FileProtocolError, OSError, ValueError, TypeError) as exc:
                code = str(exc) if isinstance(exc, FileProtocolError) else type(exc).__name__
                log.warning(f"Hook attachment protocol preparation failed: {code}")
                # A file error must be visible; never silently claim to have uploaded it.
                req.extra_user_content_parts = list(req.extra_user_content_parts or []) + [
                    {'type': 'text', 'text': '[附件未作为文件成功提交：' + code + '。请明确说明无法读取附件，不要根据路径或名称猜测内容。]'}]
            await adapter.prepare_video_input(event, req, self.context)

    @filter.command('hook_file_probe')
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def hook_file_probe(self, event):
        """Verify current Responses gateway with a synthetic assembly file only."""
        try:
            result = await probe_file_protocol(self.context, event.unified_msg_origin)
            text = ('文件协议验证通过：当前模型从合成附件中读回了随机标记。' if result['content_verified']
                    else '接口已返回，但没有读回附件随机标记，尚不能证明文件内容可用。')
        except Exception as exc:
            status = getattr(exc, 'status_code', None)
            text = '文件协议验证失败：' + type(exc).__name__ + (f'，HTTP {status}' if status else '')
        yield event.plain_result(text + ' 本次未上传真实聊天附件。')
