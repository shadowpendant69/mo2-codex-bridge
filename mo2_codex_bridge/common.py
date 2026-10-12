"""Filesystem constraints shared by the bridge and its offline tests."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

VERSION = '0.1.0'
TEXT_EXTENSIONS = {'.ini', '.json', '.toml', '.xml', '.txt', '.yaml', '.yml', '.cfg'}
PROTECTED = {'meta.ini', 'modlist.txt', 'plugins.txt', 'loadorder.txt', 'archives.txt',
             'lockedorder.txt', 'settings.ini', 'modorganizer.ini'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.bridge-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False).encode('utf-8'))


def safe_name(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError('A nonempty name without surrounding whitespace is required')
    if len(value) > 150 or re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value.endswith(('.', ' ')):
        raise ValueError('Invalid Windows folder name')
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
        raise ValueError('Reserved Windows folder name')
    if value in {'.', '..'}:
        raise ValueError('Invalid folder name')
    return value


def contained_path(root, relative, *, text_only=False):
    """Reject traversal, alternate data streams, symlinks and Windows junctions."""
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative:
        raise ValueError('A relative file path is required')
    normalized = relative.replace('\\', '/')
    if normalized.startswith('/') or ':' in normalized or '\x00' in normalized:
        raise ValueError('Absolute paths and alternate data streams are not permitted')
    parts = normalized.split('/')
    if any(p in {'', '.', '..'} or p.endswith((' ', '.')) for p in parts):
        raise ValueError('Invalid relative path')
    for part in parts:
        safe_name(part)
    candidate = root.joinpath(*parts)
    check = root
    for part in parts:
        check = check / part
        if check.is_symlink() or (hasattr(check, 'is_junction') and check.is_junction()):
            raise ValueError('Linked files and junctions are not permitted')
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError('Path escapes the selected folder')
    if text_only and (resolved.suffix.lower() not in TEXT_EXTENSIONS or resolved.name.lower() in PROTECTED):
        raise ValueError('Only mod configuration text or game INIs may be edited; use MO2 APIs for ordering')
    return resolved
