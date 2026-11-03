"""Opaque file verification and attachment metadata; no content interpretation."""
import hashlib
import os
from pathlib import Path
import stat

from .events import normalize_event
from .payloads import file_name, quoted_file_card

MAX_FILE_BYTES = 24 * 1024 * 1024


def read_verified_file(root: Path, record: dict) -> bytes:
    path = Path(record['path'])
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_relative_to(root.resolve()):
        raise ValueError('file_path_outside_media')
    artifact = record['artifact']
    size = artifact.get('bytes')
    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
        raise ValueError('file_size_invalid')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError('file_changed')
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != str(artifact.get('sha256') or '').lower():
        raise ValueError('file_integrity_mismatch')
    return data


def attach_file_metadata(event, inbox, owner: str) -> None:
    """Expose the already verified attachment for downstream handlers, as data."""
    source = normalize_event(event.message_obj.raw_message, owner)
    if source is None or source.media_kind != 'file':
        return
    record = inbox.ready_file(owner, source.room, event_id=event.message_obj.message_id)
    if record is None:
        return
    card = quoted_file_card(source.quote) if source.quote else {}
    artifact = record['artifact']
    event.set_extra('wechat_attachments', [{
        'kind': 'file', 'name': card.get('title') or file_name(source.raw),
        'path': record['path'], 'bytes': artifact['bytes'], 'sha256': artifact['sha256'],
        'source_msgsvrid': source.media_msgsvrid,
        'source_talker': source.media_source_talker,
        'quoted': source.quote is not None,
    }])
