"""WeChat transport with optional video media preparation; group policy lives elsewhere."""
import asyncio
import gc
import os
from pathlib import Path
import re
import tempfile

from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.api import logger as log
from .login_panel import LoginPanel, find_hook_platform, probe_hook_login_with_png
from .responses_files import FileProtocolError, probe_file_protocol, restore_file_protocol


_LEASE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.3, 0.5, 0.5, 1.0, 1.5, 2.0, 2.0)


def _is_inbox_lease_busy(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and "Inbox is already owned by another running connector" in str(exc)


class Main(Star):
    _stopped = False

    def __init__(self, context: Context):
        super().__init__(context)
        from .adapter import WeChatHookPlatform
        self._platform_type = WeChatHookPlatform
        self._login_panel = LoginPanel(context)

    async def _load_platform_with_lease_wait(self, manager, config: dict) -> None:
        # Yield once so a just-cancelled previous connector run() can unwind and close its lease.
        await asyncio.sleep(0)
        delays = iter(_LEASE_RETRY_DELAYS)
        warned = False
        while True:
            try:
                await manager.load_platform(config)
                return
            except RuntimeError as exc:
                if not _is_inbox_lease_busy(exc):
                    raise
                delay = next(delays, None)
                if delay is None:
                    raise
                if not warned:
                    warned = True
                    log.info("Waiting for previous WeChat Hook connector instance to release Inbox lock...")
                gc.collect()
                await asyncio.sleep(delay)

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
                await self._load_platform_with_lease_wait(manager, config)
            elif not isinstance(existing, self._platform_type):
                # Older releases had no plugin terminate hook; retire their live instance first.
                await manager.terminate_platform(config['id'])
                await self._load_platform_with_lease_wait(manager, config)

    async def terminate(self):
        if self._stopped:
            return
        self._stopped = True
        self._login_panel.active = False
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

    @filter.command('hook_qrcode')
    async def hook_qrcode(self, event, platform_id: str = ''):
        """Show current WeChat Hook login QR code on Dashboard WebChat or for admins."""
        if event.get_platform_name() != 'webchat' and getattr(event, 'role', '') != 'admin':
            yield event.plain_result('查看微信登录二维码仅允许后台 WebChat 或管理员使用。')
            return
        config, live_inst = find_hook_platform(self.context, platform_id)
        if config is None:
            yield event.plain_result('未找到已配置的微信 Hook 平台实例。')
            return
        data, raw_png = await probe_hook_login_with_png(config, live_inst)
        safe_id = re.sub(r'[^A-Za-z0-9._-]+', '_', str(data.get('platform_id') or 'wechat_pc_hook'))
        qr_path = Path(tempfile.gettempdir()) / f'astrbot_wxcx_qr_{safe_id}.png'
        if data.get('status') == 'qr_code' and raw_png:
            qr_path.write_bytes(raw_png)
            if os.name != 'nt':
                os.chmod(qr_path, 0o600)
            if hasattr(event, 'image_result'):
                yield event.image_result(str(qr_path))
            yield event.plain_result('请使用手机微信扫码登录（微信客户端窗口与此处二维码互通，任一端扫码即生效）。')
            return
        if data.get('status') == 'logged_in' and qr_path.exists():
            try:
                qr_path.unlink()
            except OSError:
                pass
        yield event.plain_result(str(data.get('detail') or '当前暂无可展示的登录二维码。'))
