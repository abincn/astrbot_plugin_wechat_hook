"""Transport verified bytes as native Responses input_file; never parse file contents."""
import asyncio
import base64
import copy
import inspect
import mimetypes
import secrets
import types

from .events import normalize_event
from .file_artifacts import MAX_FILE_BYTES, read_verified_file
from .payloads import file_name, quoted_file_card

MAX_REQUEST_FILE_BYTES = 48_000_000
_patched_providers = []
_registered_part = None
_protocol_stopped = False


class FileProtocolError(ValueError):
    pass


def register_file_content_part() -> None:
    """Use AstrBot's extension registry so typed agent messages retain file parts."""
    if _protocol_stopped:
        raise FileProtocolError('file_protocol_stopped')
    from astrbot.core.agent.message import ContentPart
    sample = file_part('probe.txt', b'')
    try:
        ContentPart.model_validate(sample)
        return  # Preserve a future core's existing file implementation.
    except (KeyError, ValueError, TypeError):
        pass
    global _registered_part
    class WeChatFilePart(ContentPart):
        type: str = 'file'
        file: dict[str, str]
    # Fail explicitly if this AstrBot release cannot register the extension.
    ContentPart.model_validate(sample)
    _registered_part = (ContentPart, WeChatFilePart)


def restore_file_protocol() -> None:
    """Undo only our own instance wrappers and our own content registration."""
    global _registered_part, _protocol_stopped
    _protocol_stopped = True
    for provider, wrapper, had_override, original_override in reversed(_patched_providers):
        if getattr(provider, '_convert_chat_messages_to_response_input', None) is wrapper:
            if had_override:
                provider._convert_chat_messages_to_response_input = original_override
            else:
                del provider._convert_chat_messages_to_response_input
        if getattr(provider, '_wechat_file_protocol', None) is wrapper:
            del provider._wechat_file_protocol
    _patched_providers.clear()
    if _registered_part is not None:
        base, owned = _registered_part
        registry = getattr(base, '_ContentPart__content_part_registry', None)
        if registry is not None and registry.get('file') is owned:
            del registry['file']
        _registered_part = None


def file_part(filename: str, data: bytes) -> dict:
    if (not isinstance(filename, str) or not filename or len(filename) > 200
            or any(ord(c) < 32 for c in filename) or '/' in filename or '\\' in filename):
        raise FileProtocolError('invalid_attachment_name')
    if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
        raise FileProtocolError('attachment_size_limit')
    mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    return {'type': 'file', 'file': {'filename': filename,
            'file_data': 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')}}


def _native_part(part: dict) -> tuple[dict, int]:
    value = part.get('file')
    if not isinstance(value, dict) or set(value) != {'filename', 'file_data'}:
        raise FileProtocolError('unsupported_file_reference')
    name, data = value['filename'], value['file_data']
    if (not isinstance(name, str) or not name or len(name) > 200 or '/' in name or '\\' in name
            or any(ord(c) < 32 for c in name)):
        raise FileProtocolError('invalid_attachment_name')
    if (not isinstance(data, str) or len(data) > ((MAX_FILE_BYTES + 2) // 3) * 4 + 200
            or not data.startswith('data:') or ';base64,' not in data):
        raise FileProtocolError('invalid_file_data')
    try:
        decoded = base64.b64decode(data.split(';base64,', 1)[1], validate=True)
    except (ValueError, TypeError):
        raise FileProtocolError('invalid_file_data') from None
    if len(decoded) > MAX_FILE_BYTES:
        raise FileProtocolError('attachment_size_limit')
    return {'type': 'input_file', 'filename': name, 'file_data': data}, len(decoded)


def convert_with_files(original, messages: list[dict]) -> list[dict]:
    """Delegate reasoning/tools/images to AstrBot; replace only private placeholders."""
    prepared = []; replacements = {}; total = 0
    for message in messages:
        content = message.get('content') if isinstance(message, dict) else None
        if (not isinstance(content, list)
                or not any(isinstance(p, dict) and p.get('type') == 'file' for p in content)):
            prepared.append(message); continue
        if message.get('role') != 'user':
            raise FileProtocolError('file_requires_user_role')
        parts = []
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'file':
                parts.append(part); continue
            native, size = _native_part(part); total += size
            if total > MAX_REQUEST_FILE_BYTES:
                raise FileProtocolError('request_file_size_limit')
            marker = 'wxcx-file-protocol-' + secrets.token_hex(24)
            replacements[marker] = native
            parts.append({'type': 'text', 'text': marker})
        prepared.append({**message, 'content': parts})
    if not replacements:
        return original(messages)
    output = copy.deepcopy(original(prepared)); replaced = set()
    for message in output:
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for index, part in enumerate(content):
            if (isinstance(part, dict) and part.get('type') == 'input_text'
                    and part.get('text') in replacements):
                marker = part['text']; content[index] = replacements[marker]; replaced.add(marker)
    if replaced != set(replacements):
        raise FileProtocolError('responses_converter_incompatible')
    return output


def enable_file_protocol(provider) -> bool:
    """Patch one existing Responses instance; file-free requests remain unchanged."""
    if _protocol_stopped:
        raise FileProtocolError('file_protocol_stopped')
    if provider is None or getattr(provider.meta(), 'type', None) != 'openai_responses':
        raise FileProtocolError('responses_provider_required')
    if any(p is provider for p, *_ in _patched_providers):
        return False
    original = getattr(provider, '_convert_chat_messages_to_response_input', None)
    if not callable(original) or list(inspect.signature(original).parameters) != ['messages']:
        raise FileProtocolError('responses_converter_incompatible')
    def convert(self, messages):
        return convert_with_files(original, messages)
    had_override = '_convert_chat_messages_to_response_input' in vars(provider)
    original_override = vars(provider).get('_convert_chat_messages_to_response_input')
    wrapper = types.MethodType(convert, provider)
    provider._convert_chat_messages_to_response_input = wrapper
    provider._wechat_file_protocol = wrapper
    _patched_providers.append((provider, wrapper, had_override, original_override))
    return True


async def prepare_file_upload(event, req, inbox, owner, root, context) -> bool:
    if event.get_extra('wechat_file_upload_request') == id(req):
        return False
    source = normalize_event(event.message_obj.raw_message, owner)
    if source is None or source.media_kind != 'file':
        return False
    record = inbox.ready_file(owner, source.room, event_id=event.message_obj.message_id)
    if record is None:
        raise FileProtocolError('attachment_not_ready')
    provider = await context.get_using_provider_async(umo=event.unified_msg_origin)
    enable_file_protocol(provider)
    data = await asyncio.to_thread(read_verified_file, root, record)
    card = quoted_file_card(source.quote) if source.quote else {}
    part = file_part(card.get('title') or file_name(source.raw), data)
    register_file_content_part()
    part['_no_save'] = True  # Provider-facing only; don't persist Base64 in history.
    req.extra_user_content_parts = list(req.extra_user_content_parts or []) + [part]
    # A fallback provider without this compatibility shim may discard file parts.
    # Keep an explicit per-turn warning instead of implying that it read the bytes.
    req.extra_user_content_parts.append({
        'type': 'text', '_no_save': True,
        'text': '[附件读取约定：只有实际收到文件内容时才能分析附件；若当前接口未提供文件内容，请明确说明无法读取，不要根据文件名或路径猜测。]',
    })
    event.set_extra('wechat_file_upload_request', id(req))
    event.set_extra('wechat_file_upload', {'state': 'prepared', 'bytes': len(data),
                                         'protocol': 'responses_input_file', 'accepted_by_api': False})
    return True


async def probe_file_protocol(context, umo: str) -> dict:
    """Admin-triggered synthetic request using the existing client; no private files."""
    provider = await context.get_using_provider_async(umo=umo)
    if provider is None or getattr(provider.meta(), 'type', None) != 'openai_responses':
        raise FileProtocolError('responses_provider_required')
    marker = 'FILEPROBE_' + secrets.token_hex(8)
    part = file_part('wxcx-probe.S', ('.section .rodata\nmarker: .asciz "' + marker + '"\n').encode())
    converted = convert_with_files(provider._convert_chat_messages_to_response_input, [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Return only the ASCII string defined by marker in the attached file.'}, part]}])
    client = getattr(provider, 'client', None)
    model = getattr(provider.meta(), 'model', None)
    if not client or not model:
        raise FileProtocolError('provider_client_not_ready')
    response = await asyncio.wait_for(client.responses.create(model=model, input=converted,
                                                             max_output_tokens=1024, store=False), timeout=45)
    text = getattr(response, 'output_text', '') or ''
    if not text:
        for item in getattr(response, 'output', []) or []:
            for block in getattr(item, 'content', []) or []:
                if getattr(block, 'type', '') == 'output_text': text += getattr(block, 'text', '')
    return {'protocol': 'responses_input_file', 'api_returned': True,
            'content_verified': marker in text, 'private_attachment_sent': False}
