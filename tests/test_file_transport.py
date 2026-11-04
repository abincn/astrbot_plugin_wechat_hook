import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# Framework/network stand-ins only. Service normalization, SQLite, file hashing,
# media pipeline, quote mapping and model-request preparation are production code.
httpx = types.ModuleType('httpx')
httpx.HTTPError = type('HTTPError', (Exception,), {})
httpx.HTTPStatusError = type('HTTPStatusError', (httpx.HTTPError,), {})
sys.modules['httpx'] = httpx
api = types.ModuleType('astrbot.api')
api.logger = types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)
sys.modules['astrbot'] = types.ModuleType('astrbot')
sys.modules['astrbot.api'] = api
components = types.ModuleType('astrbot.api.message_components')
class Component:
    def __init__(self, **kw): self.__dict__.update(kw)
for name in ('Plain', 'Reply', 'At', 'AtAll', 'File', 'Image', 'Json', 'Location', 'Node', 'Nodes', 'Record', 'Share', 'Video'):
    setattr(components, name, type(name, (Component,), {}))
sys.modules[components.__name__] = components
event_api = types.ModuleType('astrbot.api.event')
event_api.filter = types.SimpleNamespace(on_llm_request=lambda: lambda f: f,
    command=lambda *a, **k: lambda f: f, permission_type=lambda *a, **k: lambda f: f,
    PermissionType=types.SimpleNamespace(ADMIN='admin'))
sys.modules[event_api.__name__] = event_api
star = types.ModuleType('astrbot.api.star'); star.Context = object; star.Star = object
sys.modules[star.__name__] = star

P = 'astrbot_plugin_wechat_hook'
events = importlib.import_module(P + '.events')
inbox_module = importlib.import_module(P + '.inbox')
inputs = importlib.import_module(P + '.file_artifacts')
media = importlib.import_module(P + '.media')
mapping = importlib.import_module(P + '.components')
main = importlib.import_module(P + '.main')

OWNER = 'wxid_self'
ROOM = '123456@chatroom'
SOURCE_ID = '9007199254740997'

def raw(seq=1, *, room=ROOM, owner=OWNER, name='run.py', quote=False, subtype=6):
    result = dict(event_id=f'event-{owner}-{room}-{seq}', sequence=seq, self_id=owner,
                  sender_id='wxid_peer', direction='incoming', is_group=room.endswith('@chatroom'),
                  conversation_id=room, room_id=room if room.endswith('@chatroom') else '',
                  msgsvrid=SOURCE_ID if not quote else str(int(SOURCE_ID) + seq),
                  type=49, message_kind='quote' if quote else 'file_card',
                  card={'title': name}, content='文件是什么？', message_time=1000 + seq)
    if quote:
        result['quote'] = dict(message_type=49, msgsvrid=SOURCE_ID, conversation_id=room,
                               reply_text='文件是什么？', display_name='Peer',
                               content=f'<msg><appmsg><type>{subtype}</type><title>{name}</title></appmsg></msg>')
    return result

class Event:
    def __init__(self, message):
        self.message_obj = types.SimpleNamespace(raw_message=message, message_id=message['event_id'])
        self.extra = {}
    def get_extra(self, k): return self.extra.get(k)
    def set_extra(self, k, v): self.extra[k] = v


class FileTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.media_root = self.root / 'media'; self.media_root.mkdir()
        self.inbox = inbox_module.DurableInbox(self.root / 'inbox.db')
        self.body = b'\x00\xffopaque attachment\x80\x01'

    def tearDown(self): self.inbox.conn.close(); self.temp.cleanup()

    def ready(self, message=None, data=None):
        message = message or raw(); data = self.body if data is None else data
        self.inbox.ingest_batch([message], message['self_id'])
        artifact = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), extension=Path(message['card']['title']).suffix.lstrip('.'))
        path = media.save_verified_media(self.media_root, data, artifact, 'file')
        self.inbox.media_ready(message['event_id'], str(path), artifact)
        return path

    def pending_source(self, message):
        return next(m for m in self.inbox.pending() if m.event_id == message['event_id'])

    def test_all_file_types_are_opaque_File_components(self):
        for seq, name in enumerate(('run.py', 'initialize.S', 'data.bin', 'report.pdf', 'report.docx', 'archive.zip', 'program.exe', 'unknown.xyz', 'README'), 1):
            with self.subTest(name=name):
                message = raw(seq, name=name); path = self.ready(message)
                _, chain = mapping.inbound_content(self.pending_source(message))
                self.assertIsInstance(chain[0], components.File)
                self.assertEqual(chain[0].name, name)
                self.assertEqual(Path(chain[0].file).read_bytes(), self.body)
                self.assertEqual(path.suffix, '.bin')

    async def test_quoted_binary_file_reuses_original_without_HTTP(self):
        self.ready(raw(name='archive.zip')); quote = raw(2, name='archive.zip', quote=True)
        self.inbox.ingest_batch([quote], OWNER)
        async def no_http(*a, **kw): self.fail('cached quote requested HTTP')
        await media.process_pending_media(self.inbox, no_http, None, self.media_root)
        _, chain = mapping.inbound_content(self.pending_source(quote))
        self.assertEqual(chain[0].message_str, '[引用文件] archive.zip')
        self.assertIsInstance(chain[0].chain[-1], components.File)
        self.assertEqual(chain[0].chain[-1].name, 'archive.zip')
        self.assertEqual(Path(chain[0].chain[-1].file).read_bytes(), self.body)

    async def test_capacity_rejection_waits_without_unknown_or_immediate_resubmit(self):
        message = raw(); self.inbox.ingest_batch([message], OWNER)
        calls = []
        class Capacity(Exception):
            status_code = 429
            error_code = 'media_capacity'
        async def request(*args, **kwargs):
            calls.append(args); raise Capacity()
        await media.process_pending_media(self.inbox, request, None, self.media_root)
        self.assertEqual(self.inbox.pending_media(), [])
        row = self.inbox.conn.execute('SELECT * FROM media_jobs').fetchone()
        self.assertEqual(row['status'], 'new')
        self.assertEqual(row['capacity_retries'], 1)
        await media.process_pending_media(self.inbox, request, None, self.media_root)
        self.assertEqual(len(calls), 1)
        self.inbox.conn.execute('UPDATE media_jobs SET retry_after=0,capacity_retries=30')
        await media.process_pending_media(self.inbox, request, None, self.media_root)
        row = self.inbox.conn.execute('SELECT * FROM media_jobs').fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error'], 'media_capacity_wait_exhausted')

    async def test_uncertain_create_is_not_retried(self):
        message = raw(); self.inbox.ingest_batch([message], OWNER)
        calls = []
        async def request(*args, **kwargs):
            calls.append(args); raise TimeoutError()
        await media.process_pending_media(self.inbox, request, None, self.media_root)
        await media.process_pending_media(self.inbox, request, None, self.media_root)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.inbox.conn.execute('SELECT status FROM media_jobs').fetchone()[0], 'unknown')

    async def test_historical_ready_release_is_recoverable_and_keeps_local_copy(self):
        path = self.ready()
        self.inbox.conn.execute("UPDATE media_jobs SET task_id='old-task'")
        calls = []
        async def broken(method, url, **kwargs):
            calls.append((method, url)); raise TimeoutError()
        await media.process_pending_media(self.inbox, broken, None, self.media_root)
        self.assertEqual(len(self.inbox.releasable_media()), 1)
        async def released(method, url, **kwargs):
            calls.append((method, url)); return 200, {'released': True}
        await media.process_pending_media(self.inbox, released, None, self.media_root)
        self.assertEqual(self.inbox.releasable_media(), [])
        self.assertEqual(path.read_bytes(), self.body)
        self.assertTrue(all(method == 'DELETE' for method, _ in calls))

    async def test_uncached_binary_task_content_handoff(self):
        quote = raw(2, name='report.docx', quote=True); self.inbox.ingest_batch([quote], OWNER)
        data = self.body; calls = []
        artifact = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), extension='docx')
        async def request(method, path, **kwargs):
            calls.append((method, kwargs.get('data')))
            if method == 'POST': return 202, {'task_id': 'file-task'}
            return 200, dict(state='ready', kind='file', msgsvrid=SOURCE_ID, source_talker=ROOM, artifact=artifact)
        class Content:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            def raise_for_status(self): pass
            async def aiter_bytes(self): yield data
        class Client:
            def stream(self, *a): return Content()
        await media.process_pending_media(self.inbox, request, Client(), self.media_root)
        await media.process_pending_media(self.inbox, request, Client(), self.media_root)
        _, chain = mapping.inbound_content(self.pending_source(quote))
        self.assertEqual(calls[0][1]['msgsvrid'], SOURCE_ID)
        self.assertEqual(calls[0][1]['kind'], 'file')
        self.assertEqual(Path(chain[0].chain[-1].file).read_bytes(), self.body)
        self.assertEqual(calls[-1][0], 'DELETE')

    def test_downstream_descriptor_has_reference_and_integrity_without_body_or_MIME_guess(self):
        message = raw(name='data.weird'); self.ready(message); event = Event(message)
        inputs.attach_file_metadata(event, self.inbox, OWNER)
        descriptor = event.extra['wechat_attachments'][0]
        self.assertEqual(descriptor['name'], 'data.weird')
        self.assertEqual(descriptor['source_msgsvrid'], SOURCE_ID)
        self.assertEqual(descriptor['source_talker'], ROOM)
        self.assertEqual(descriptor['bytes'], len(self.body))
        self.assertEqual(descriptor['sha256'], hashlib.sha256(self.body).hexdigest())
        self.assertNotIn('content', descriptor); self.assertNotIn('mime_type', descriptor)

    async def test_model_hook_does_not_inject_file_text_into_prompt(self):
        calls = []
        class Adapter:
            async def prepare_file_upload(self, event, req, context): calls.append('file')
            async def prepare_video_input(self, event, req, context): calls.append('video')
        handler = main.Main.__new__(main.Main); handler._platform_type = Adapter; handler.context = None
        message = raw(); self.ready(message)
        event = Event(message); event.adapter = Adapter(); req = types.SimpleNamespace(prompt='原始问题')
        await handler.on_llm_request(event, req)
        self.assertEqual(req.prompt, '原始问题'); self.assertEqual(calls, ['file', 'video'])

    def test_cache_isolated_by_account_room_and_message_ID(self):
        self.ready()
        for owner, room, msgid in [(OWNER, 'other@chatroom', SOURCE_ID), ('wxid_other', ROOM, SOURCE_ID), (OWNER, ROOM, '42')]:
            self.assertIsNone(self.inbox.ready_file(owner, room, source_id=msgid))

    async def test_same_name_does_not_match_another_message(self):
        self.ready(); quote = raw(2, quote=True); quote['quote']['msgsvrid'] = '42'
        self.inbox.ingest_batch([quote], OWNER); calls = []
        async def request(method, path, **kwargs):
            calls.append(kwargs['data']); return 202, {'task_id': 'fresh-task'}
        await media.process_pending_media(self.inbox, request, None, self.media_root)
        self.assertEqual(calls[0]['msgsvrid'], '42')

    async def test_bad_cached_file_does_not_get_handed_off_or_retried(self):
        path = self.ready(); path.write_bytes(b'changed')
        quote = raw(2, quote=True); self.inbox.ingest_batch([quote], OWNER)
        async def no_http(*a, **kw): self.fail('corrupt cached file retried')
        await media.process_pending_media(self.inbox, no_http, None, self.media_root)
        event = Event(quote); inputs.attach_file_metadata(event, self.inbox, OWNER)
        self.assertNotIn('wechat_attachments', event.extra)

    async def test_private_quoted_binary(self):
        self.ready(raw(room='wxid_peer', name='program.exe'))
        quote = raw(2, room='wxid_peer', name='program.exe', quote=True); self.inbox.ingest_batch([quote], OWNER)
        async def no_http(*a, **kw): self.fail('private cached file missed')
        await media.process_pending_media(self.inbox, no_http, None, self.media_root)
        self.assertTrue(self.pending_source(quote).media_path)

    async def test_actual_chatuser_field_stays_in_current_group(self):
        self.ready(); quote = raw(2, quote=True); quote['quote']['conversation_id'] = 'Abin_CN'
        self.inbox.ingest_batch([quote], OWNER)
        async def no_http(*a, **kw): self.fail('chatuser replaced group source')
        await media.process_pending_media(self.inbox, no_http, None, self.media_root)
        self.assertEqual(self.pending_source(quote).media_source_talker, ROOM)

    def test_invalid_references_and_link_card_not_files(self):
        self.assertEqual(events.normalize_event(raw(2, quote=True, subtype=5), OWNER).media_kind, '')
        for value in ('0', str(2**64), '９', '1x'):
            quote = raw(2, quote=True); quote['quote']['msgsvrid'] = value
            self.assertEqual(events.normalize_event(quote, OWNER).media_kind, '')

    def test_DTD_not_expanded(self):
        quote = raw(2, quote=True); quote['quote']['content'] = '<!DOCTYPE x [<!ENTITY a "secret">]><appmsg><type>6</type><title>&a;</title></appmsg>'
        self.assertEqual(events.normalize_event(quote, OWNER).media_kind, '')

    def test_empty_file(self):
        message = raw(); path = self.ready(message, b'')
        self.assertEqual(path.read_bytes(), b'')
        self.assertEqual(self.inbox.ready_file(OWNER, ROOM, event_id=message['event_id'])['artifact']['bytes'], 0)

    def test_integrity_path_and_size_limits_apply_without_format_detection(self):
        path = self.ready(); record = self.inbox.ready_file(OWNER, ROOM, event_id=raw()['event_id'])
        outside = self.root / 'outside'; outside.write_bytes(self.body)
        with self.assertRaises(ValueError): inputs.read_verified_file(self.media_root, {**record, 'path': str(outside)})
        with self.assertRaises(ValueError): inputs.read_verified_file(self.media_root, {**record, 'artifact': {**record['artifact'], 'bytes': 2**32}})
        path.write_bytes(b'x' * len(self.body))
        with self.assertRaises(ValueError): inputs.read_verified_file(self.media_root, record)

    def test_source_code_never_runs(self):
        sentinel = self.root / 'executed'; body = f'open({str(sentinel)!r}, "w").write("bad")'.encode()
        message = raw(); self.ready(message, body)
        inputs.attach_file_metadata(Event(message), self.inbox, OWNER)
        self.assertFalse(sentinel.exists())

if __name__ == '__main__': unittest.main(verbosity=2)
