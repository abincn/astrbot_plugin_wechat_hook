"""Native-file transport tests. No credentials, sockets, live model or file execution."""
import asyncio
import base64
import copy
import importlib
import json
import types
import sys
import unittest
from unittest.mock import patch

import test_file_transport as fixtures

class RegistryPart:
    registry = {}
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        RegistryPart.registry[cls.type] = cls
    @classmethod
    def model_validate(cls, value):
        return cls.registry[value['type']]

message_module = types.ModuleType('astrbot.core.agent.message')
message_module.ContentPart = RegistryPart
sys.modules.setdefault(message_module.__name__, message_module)

protocol = importlib.import_module(fixtures.P + '.responses_files')


class Provider:
    _REASONING_STATE_TYPE = 'openai_responses_reasoning'
    def __init__(self, *, kind='openai_responses', verify=True):
        self.kind = kind; self.calls = []; self.api_calls = []; self.verify = verify
        self.client = types.SimpleNamespace(base_url=types.SimpleNamespace(host='gateway.invalid'),
                                            responses=types.SimpleNamespace(create=self.create))
        self.provider_config = {}
    def meta(self): return types.SimpleNamespace(type=self.kind, model='configured-model')
    async def create(self, **kwargs):
        self.api_calls.append(kwargs)
        content = kwargs['input'][0]['content']
        part = next(p for p in content if p['type'] == 'input_file')
        body = base64.b64decode(part['file_data'].split(',', 1)[1]).decode()
        marker = body.split('"')[1]
        return types.SimpleNamespace(output_text=marker if self.verify else 'I cannot read it')
    def _convert_chat_messages_to_response_input(self, messages):
        # A converter that, like the affected core, has no file branch.
        self.calls.append(copy.deepcopy(messages)); result = []
        for message in messages:
            if message.get('role') == 'tool':
                result.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': message['content']})
                continue
            content = message.get('content')
            if isinstance(content, list):
                parts = []
                for part in content:
                    if part.get('type') == 'text': parts.append({'type': 'input_text', 'text': part['text']})
                    elif part.get('type') == 'image_url': parts.append({'type': 'input_image', **part['image_url']})
                content = parts
            if content: result.append({'role': message['role'], 'content': content})
        return result


def context(provider, *fallbacks):
    async def current(**kwargs): return provider
    return types.SimpleNamespace(get_using_provider_async=current,
                                 get_all_providers=lambda: [provider, *fallbacks])


class ProtocolTests(unittest.TestCase):
    def test_agent_content_part_registration_is_idempotent(self):
        from astrbot.core.agent.message import ContentPart
        protocol.register_file_content_part()
        first = ContentPart.model_validate(protocol.file_part('a.S', b'bytes'))
        protocol.register_file_content_part()
        second = ContentPart.model_validate(protocol.file_part('a.S', b'bytes'))
        self.assertIs(type(first), type(second))

    def test_binary_bytes_and_original_name_are_native_file_not_text(self):
        p = Provider(); protocol.enable_file_protocol(p)
        part = protocol.file_part('initialize(1).S', b'\x00\xffexact bytes')
        output = p._convert_chat_messages_to_response_input([{'role': 'user', 'content': [part]}])
        native = output[0]['content'][0]
        self.assertEqual(native['type'], 'input_file')
        self.assertEqual(native['filename'], 'initialize(1).S')
        self.assertEqual(base64.b64decode(native['file_data'].split(',', 1)[1]), b'\x00\xffexact bytes')
        self.assertNotIn('text', native)

    def test_any_extension_or_no_extension_and_empty_file(self):
        for name in ('file.pdf', 'file.docx', 'file.zip', 'file.exe', 'file.weird', 'README'):
            p = Provider(); protocol.enable_file_protocol(p)
            result = p._convert_chat_messages_to_response_input([{'role': 'user', 'content': [protocol.file_part(name, b'')]}])
            self.assertEqual(result[0]['content'][0]['filename'], name)

    def test_no_file_delegates_without_modification(self):
        p = Provider(); messages = [{'role': 'user', 'content': 'hello'}]
        expected = p._convert_chat_messages_to_response_input(messages)
        protocol.enable_file_protocol(p)
        self.assertEqual(p._convert_chat_messages_to_response_input(messages), expected)
        self.assertEqual(messages, [{'role': 'user', 'content': 'hello'}])

    def test_text_image_tools_and_source_object_preserved(self):
        p = Provider(); protocol.enable_file_protocol(p)
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'question'}, protocol.file_part('a.txt', b'one'),
                     {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]},
                    {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'result'}]
        original = copy.deepcopy(messages)
        result = p._convert_chat_messages_to_response_input(messages)
        self.assertEqual([x['type'] for x in result[0]['content']], ['input_text', 'input_file', 'input_image'])
        self.assertEqual(result[1]['call_id'], 'call-1')
        self.assertEqual(messages, original)

    def test_multiple_files_keep_order(self):
        p = Provider(); protocol.enable_file_protocol(p)
        parts = [protocol.file_part('first.txt', b'1'), protocol.file_part('second.txt', b'2')]
        result = p._convert_chat_messages_to_response_input([{'role': 'user', 'content': parts}])
        self.assertEqual([p['filename'] for p in result[0]['content']], ['first.txt', 'second.txt'])

    def test_incompatible_converter_does_not_silently_drop_file(self):
        with self.assertRaisesRegex(protocol.FileProtocolError, 'incompatible'):
            protocol.convert_with_files(lambda messages: [], [{'role': 'user', 'content': [protocol.file_part('a.txt', b'a')]}])

    def test_invalid_name_rejected(self):
        for name in ('../secret', 'a\\secret', '', 'a\n.txt', 'x' * 201):
            with self.assertRaises(protocol.FileProtocolError): protocol.file_part(name, b'')

    def test_invalid_base64_and_URL_reference_rejected(self):
        for value in ({'filename': 'a.txt', 'file_data': 'data:text/plain;base64,??'},
                      {'filename': 'a.txt', 'file_url': 'http://untrusted.invalid/'}):
            with self.assertRaises(protocol.FileProtocolError):
                protocol.convert_with_files(lambda x: x, [{'role': 'user', 'content': [{'type': 'file', 'file': value}]}])

    def test_assistant_file_rejected(self):
        with self.assertRaises(protocol.FileProtocolError):
            protocol.convert_with_files(lambda x: x, [{'role': 'assistant', 'content': [protocol.file_part('a.txt', b'a')]}])

    def test_total_request_limit(self):
        with patch.object(protocol, 'MAX_REQUEST_FILE_BYTES', 5):
            with self.assertRaisesRegex(protocol.FileProtocolError, 'request_file_size_limit'):
                protocol.convert_with_files(lambda x: x, [{'role': 'user', 'content': [protocol.file_part('a.txt', b'123'), protocol.file_part('b.txt', b'456')]}])

    def test_instance_install_is_idempotent_and_others_unchanged(self):
        p = Provider(); other = Provider()
        self.assertTrue(protocol.enable_file_protocol(p)); self.assertFalse(protocol.enable_file_protocol(p))
        self.assertFalse(hasattr(other, '_wechat_file_protocol'))

    def test_non_responses_provider_rejected(self):
        with self.assertRaisesRegex(protocol.FileProtocolError, 'responses_provider_required'):
            protocol.enable_file_protocol(Provider(kind='other'))


class UploadTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.FileTransportTests.setUp
    tearDown = fixtures.FileTransportTests.tearDown
    ready = fixtures.FileTransportTests.ready

    async def test_verified_direct_file_added_without_prompt_change_or_network_call(self):
        raw = fixtures.raw(); self.ready(raw); event = fixtures.Event(raw); event.unified_msg_origin = 'test-session'
        req = types.SimpleNamespace(prompt='original question', extra_user_content_parts=[])
        p = Provider(); fallback = Provider()
        self.assertTrue(await protocol.prepare_file_upload(event, req, self.inbox, fixtures.OWNER, self.media_root, context(p, fallback)))
        self.assertEqual(req.prompt, 'original question'); self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertEqual(base64.b64decode(req.extra_user_content_parts[0]['file']['file_data'].split(',', 1)[1]), self.body)
        self.assertTrue(hasattr(fallback, '_wechat_file_protocol')); self.assertEqual(p.api_calls, [])
        self.assertFalse(event.extra['wechat_file_upload']['accepted_by_api'])
        self.assertTrue(req.extra_user_content_parts[0]['_no_save'])
        self.assertFalse(await protocol.prepare_file_upload(event, req, self.inbox, fixtures.OWNER, self.media_root, context(p)))
        self.assertEqual(len(req.extra_user_content_parts), 1)

    async def test_quoted_original_name_and_bytes_reach_native_conversion(self):
        self.ready(fixtures.raw(name='archive.zip')); raw = fixtures.raw(2, name='archive.zip', quote=True)
        self.inbox.ingest_batch([raw], fixtures.OWNER)
        async def no_http(*a, **kw): self.fail('should reuse verified bytes')
        await fixtures.media.process_pending_media(self.inbox, no_http, None, self.media_root)
        event = fixtures.Event(raw); event.unified_msg_origin = 'session'
        req = types.SimpleNamespace(prompt='question', extra_user_content_parts=[]); p = Provider()
        await protocol.prepare_file_upload(event, req, self.inbox, fixtures.OWNER, self.media_root, context(p))
        result = p._convert_chat_messages_to_response_input([{'role': 'user', 'content': req.extra_user_content_parts}])
        self.assertEqual(result[0]['content'][0]['filename'], 'archive.zip')
        self.assertEqual(base64.b64decode(result[0]['content'][0]['file_data'].split(',', 1)[1]), self.body)

    async def test_corrupt_file_rejected_before_content_added(self):
        raw = fixtures.raw(); path = self.ready(raw); path.write_bytes(b'changed')
        event = fixtures.Event(raw); event.unified_msg_origin = 'session'
        req = types.SimpleNamespace(extra_user_content_parts=[])
        with self.assertRaises(ValueError):
            await protocol.prepare_file_upload(event, req, self.inbox, fixtures.OWNER, self.media_root, context(Provider()))
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_wrong_account_rejected(self):
        raw = fixtures.raw(); self.ready(raw); event = fixtures.Event(raw)
        with self.assertRaises(ValueError):
            await protocol.prepare_file_upload(event, types.SimpleNamespace(), self.inbox, 'other', self.media_root, context(Provider()))

    async def test_missing_attachment_not_claimed_uploaded(self):
        raw = fixtures.raw(); event = fixtures.Event(raw)
        with self.assertRaisesRegex(protocol.FileProtocolError, 'attachment_not_ready'):
            await protocol.prepare_file_upload(event, types.SimpleNamespace(), self.inbox, fixtures.OWNER, self.media_root, context(Provider()))

    async def test_non_file_ignored(self):
        raw = fixtures.raw(); raw.update(type=1, message_kind='text')
        self.assertFalse(await protocol.prepare_file_upload(fixtures.Event(raw), types.SimpleNamespace(), self.inbox, fixtures.OWNER, self.media_root, context(Provider())))

    async def test_probe_reads_random_marker_from_synthetic_file(self):
        p = Provider(); result = await protocol.probe_file_protocol(context(p), 'session')
        self.assertTrue(result['content_verified']); self.assertFalse(result['private_attachment_sent'])
        request = p.api_calls[0]; content = request['input'][0]['content']
        marker = base64.b64decode(content[1]['file_data'].split(',', 1)[1]).decode().split('"')[1]
        self.assertNotIn(marker, content[0]['text']); self.assertEqual(content[1]['filename'], 'wxcx-probe.S')

    async def test_probe_http_response_alone_not_content_success(self):
        result = await protocol.probe_file_protocol(context(Provider(verify=False)), 'session')
        self.assertTrue(result['api_returned']); self.assertFalse(result['content_verified'])


if __name__ == '__main__': unittest.main(verbosity=2)
