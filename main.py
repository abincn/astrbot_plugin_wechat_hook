"""WeChat transport with optional video media preparation; group policy lives elsewhere."""
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.api import logger as log
from .responses_files import FileProtocolError, probe_file_protocol, restore_file_protocol


class Main(Star):
    _stopped = False

    def __init__(self, context: Context):
        super().__init__(context)
        from .adapter import WeChatHookPlatform
        self._platform_type = WeChatHookPlatform

    async def initialize(self):
        """Restore configured robots on plugin reload; core owns initial startup."""
        manager = self.context.platform_manager
        if not manager.get_insts():
            return
        for config in list(manager.platforms_config):
            if config.get('type') != 'wechat_pc_hook' or not config.get('enable'):
                continue
            existing = self.context.get_platform_inst(config['id'])
            if existing is None:
                await manager.load_platform(config)
            elif not isinstance(existing, self._platform_type):
                # Older releases had no plugin terminate hook; retire their live instance.
                await manager.reload(config)

    async def terminate(self):
        if self._stopped:
            return
        self._stopped = True
        manager = self.context.platform_manager
        errors = []
        try:
            for platform in list(manager.get_insts()):
                if isinstance(platform, self._platform_type):
                    try:
                        await manager.terminate_platform(platform.meta().id)
                    except Exception as exc:
                        errors.append(exc)
        finally:
            restore_file_protocol()
        if errors:
            raise ExceptionGroup('Could not stop all WeChat Hook platforms', errors)

    @filter.on_llm_request()
    async def on_llm_request(self, event, req):
        if self._stopped:
            return
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
    async def hook_file_probe(self, event):
        """Dashboard or existing admin only; synthetic bytes, no attachment access."""
        if event.get_platform_name() != 'webchat' and getattr(event, 'role', '') != 'admin':
            yield event.plain_result('文件协议探针仅允许后台 WebChat 或现有管理员使用。')
            return
        try:
            result = await probe_file_protocol(self.context, event.unified_msg_origin)
            text = ('文件协议验证通过：当前模型从合成附件中读回了随机标记。' if result['content_verified']
                    else '接口已返回，但没有读回附件随机标记，尚不能证明文件内容可用。')
        except Exception as exc:
            status = getattr(exc, 'status_code', None)
            text = '文件协议验证失败：' + type(exc).__name__ + (f'，HTTP {status}' if status else '')
        yield event.plain_result(text + ' 本次未上传真实聊天附件。')
