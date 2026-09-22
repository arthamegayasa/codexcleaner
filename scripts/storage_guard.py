#!/usr/bin/env python3
"""Explicit, reviewable storage maintenance. No scan result authorizes deletion.

Audit lives in storage_audit.py. This module accepts a reviewed policy, seals a
24-hour plan, and moves only unchanged disposable files to verified quarantine.
Windows exclusive handles keep a checked source from being replaced mid-copy.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any
import uuid

VERSION = 1
DAY = 86400
PROTECTED_PARTS = {
    '.git', '.hg', '.svn', '.ssh', '.aws', '.azure', '.gnupg',
    '.opencodex', '.claude', '.gemini', '.cursor', 'responses-state-spill',
    'sessions', 'archived_sessions', 'generated_images', 'visualizations',
    'attachments', 'outputs', 'worktrees', 'codex-router', 'skills', 'plugins',
    'memories', 'mcp', 'rules', 'automations', 'node_modules', '.venv',
    'local storage', 'indexeddb', 'session storage', 'webstorage', 'network',
}
PROJECT_MARKERS = {'.git', '.hg', '.svn', 'pyproject.toml', 'package.json',
                   'cargo.toml', 'go.mod', 'pom.xml', 'build.gradle', 'agents.md'}
PROTECTED_NAMES = {'auth.json', 'config.toml', 'agents.md', 'claude.md',
                   'session_index.jsonl', 'history.jsonl', 'cookies', 'preferences',
                   'local state', '.env', 'credentials', 'credentials.json'}
SOURCE_EXTENSIONS = {'.py', '.rs', '.go', '.c', '.cpp', '.h', '.ts', '.tsx',
                     '.jsx', '.java', '.cs', '.sln', '.csproj', '.toml'}
BLOCKED_PROCESSES = {'codex', 'chatgpt', 'claude', 'node', 'node_repl',
                     'python', 'pythonw', 'uv', 'npm', 'npx', 'neuroguide',
                     'neuroguide revamp', 'codex-router', 'opencodex'}


class SafetyError(RuntimeError):
    pass


class JournalError(RuntimeError):
    """Fatal durability failure; ordinary per-file handlers must not continue."""


def utc(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode('utf-8')).hexdigest()


def absolute(value: str | Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute() or '..' in p.parts:
        raise SafetyError('Use an absolute path without parent traversal: ' + str(p))
    # ADS/device/UNC paths are unnecessary for local storage maintenance.
    if os.name == 'nt' and (str(p).startswith('\\\\') or ':' in str(p)[2:]):
        raise SafetyError('Network, device and alternate-stream paths are not supported.')
    return Path(os.path.abspath(p))


def inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(root))]) == os.path.normcase(str(root))
    except ValueError:
        return False


def overlaps(a: Path, b: Path) -> bool:
    return inside(a, b) or inside(b, a)


def no_links(path: Path) -> None:
    """Check lexical ancestors before resolving, including Windows junctions."""
    for p in [*reversed(path.parents), path]:
        try:
            info = p.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise SafetyError('Link/reparse point is protected: ' + str(p))


def protected(path: Path) -> str | None:
    lowered = [part.casefold() for part in path.parts]
    if any(part in PROTECTED_PARTS for part in lowered):
        return 'state, output, environment or project directory'
    name = path.name.casefold()
    if name in PROTECTED_NAMES or name.startswith('.env.'):
        return 'configuration or credentials'
    if re.search(r'\.(?:sqlite\d*|db)(?:-(?:wal|shm|journal))?$', name) or name.endswith(('-wal', '-shm', '-journal', '.lock', '.pid')):
        return 'database, journal or active lock'
    if path.suffix.casefold() in SOURCE_EXTENSIONS:
        return 'source or project configuration'
    home = Path.home().absolute()
    for part in ('Documents', 'Desktop', 'Downloads', 'Pictures', 'Videos', 'Music'):
        if inside(path, home / part):
            return 'user documents or projects'
    codex = absolute(os.environ.get('CODEX_HOME', str(home / '.codex')))
    if inside(path, codex) and not any(inside(path, codex / part) for part in ('cache', 'tmp', '.tmp')):
        return 'Codex persistent data'
    for base in (os.environ.get('SystemRoot', 'C:\\Windows'),
                 os.environ.get('ProgramFiles', 'C:\\Program Files'),
                 os.environ.get('ProgramFiles(x86)', 'C:\\Program Files (x86)'),
                 os.environ.get('ProgramData', 'C:\\ProgramData')):
        if Path(base).is_absolute() and inside(path, Path(base)):
            return 'system or installed application data'
    return None


def project_ancestor(path: Path) -> bool:
    for parent in (path, *path.parents):
        if parent.is_dir():
            for marker in PROJECT_MARKERS:
                if (parent / marker).exists():
                    return True
    return False


def read_json(path: Path) -> dict:
    path = absolute(path)
    no_links(path)
    if path.stat().st_size > 100 * 1024 * 1024:
        raise SafetyError('Report exceeds 100 MiB; split the reviewed policy.')
    obj = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(obj, dict):
        raise SafetyError('Expected a JSON object.')
    return obj


def write_json(path: Path, data: dict, *, exclusive: bool = False) -> None:
    path = absolute(path)
    no_links(path)
    if exclusive and path.exists():
        raise SafetyError('Refusing to overwrite existing report: ' + str(path))
    if 'run_id' in data:
        data['manifest_hash'] = digest({k: v for k, v in data.items() if k != 'manifest_hash'})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.writing-' + uuid.uuid4().hex)
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    if exclusive:
        # Windows rename refuses an existing destination; hard link provides the
        # same no-overwrite guarantee on POSIX for report-only operations.
        if os.name == 'nt':
            temporary.rename(path)
        else:
            os.link(temporary, path)
            temporary.unlink()
    else:
        os.replace(temporary, path)


def load_policy(path: Path) -> dict:
    raw = read_json(path)
    if set(raw) - {'version', 'protected_roots', 'rules'} or raw.get('version') != VERSION:
        raise SafetyError('Unsupported policy version or keys.')
    protections = [str(absolute(p)) for p in raw.get('protected_roots', [])]
    for p in protections:
        no_links(Path(p))
    rules = []
    for rule in raw.get('rules', []):
        if not isinstance(rule, dict) or set(rule) - {'root', 'kind', 'reason', 'min_age_days'}:
            raise SafetyError('Unknown rule fields.')
        root = absolute(rule['root'])
        no_links(root)
        if not root.is_dir():
            raise SafetyError('Rule must name an existing exact directory: ' + str(root))
        if root == Path(root.anchor) or root in {Path.home().absolute(), Path(os.environ.get('TEMP', '/tmp')).absolute()}:
            raise SafetyError('Whole drives, profiles and Temp roots cannot be cleaned.')
        if rule.get('kind') not in {'cache', 'reviewed_temp'} or not str(rule.get('reason', '')).strip():
            raise SafetyError('Every rule needs a kind and the human review reason.')
        age = rule.get('min_age_days', 7)
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 7:
            raise SafetyError('The minimum age is seven days.')
        cause = protected(root)
        if cause or project_ancestor(root) or any(overlaps(root, Path(p)) for p in protections):
            raise SafetyError('Protected rule root: ' + str(root) + ' (' + str(cause or 'project/explicit protection') + ')')
        if any(overlaps(root, Path(r['root'])) for r in rules):
            raise SafetyError('Rule roots must not overlap.')
        rules.append({'root': str(root), 'kind': rule['kind'], 'reason': rule['reason'].strip(), 'min_age_days': float(age)})
    return {'version': VERSION, 'protected_roots': protections, 'rules': rules}


def fingerprint(info: os.stat_result) -> dict:
    # Windows path-stat and handle-stat expose different ctime semantics on
    # some Python builds. File identity, mtime and the plan's SHA-256 are used
    # consistently instead of mistaking that difference for a changed file.
    keys = ('st_size', 'st_mtime_ns', 'st_dev', 'st_ino', 'st_nlink')
    if os.name != 'nt':
        keys += ('st_ctime_ns',)
    return {k: getattr(info, k) for k in keys}


def file_check(path: Path, rule: dict, policy: dict, now: float, *, check_project: bool = True) -> dict:
    root = Path(rule['root'])
    if path == root or not inside(path, root):
        raise SafetyError('File escapes reviewed root.')
    no_links(path)
    if protected(path) or (check_project and project_ancestor(path.parent)) or any(inside(path, Path(p)) for p in policy['protected_roots']):
        raise SafetyError('Protected file or project.')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not info.st_ino or info.st_nlink != 1:
        raise SafetyError('Not a regular single-link file with known identity.')
    if getattr(info, 'st_file_attributes', 0) & (0x1 | 0x200 | 0x800 | 0x1000 | 0x4000):
        raise SafetyError('Read-only, sparse, compressed, offline or encrypted file.')
    if info.st_mtime > now - rule['min_age_days'] * DAY:
        raise SafetyError('Recently modified file.')
    with path.open('rb') as stream:
        if stream.read(16) == b'SQLite format 3\x00':
            raise SafetyError('Database header found regardless of extension.')
    return fingerprint(info)


def scan_rule(rule: dict, policy: dict, now: float) -> tuple[list, list]:
    root = Path(rule['root'])
    entries, skipped = [], []
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            no_links(folder)
            if protected(folder) or project_ancestor(folder):
                raise SafetyError('Protected directory or project marker.')
            with os.scandir(folder) as iterator:
                children = sorted(iterator, key=lambda e: e.name)
            for child in children:
                path = Path(child.path)
                try:
                    no_links(path)
                    if child.is_dir(follow_symlinks=False):
                        stack.append(path)
                    elif child.is_file(follow_symlinks=False):
                        fp = file_check(path, rule, policy, now, check_project=False)
                        content_hash = sha256_file(path)
                        if fingerprint(path.lstat()) != fp:
                            raise SafetyError('File changed while preparing plan.')
                        entries.append({'source': str(path), 'root': str(root),
                                        'relative': str(path.relative_to(root)), 'fingerprint': fp,
                                        'content_hash': content_hash})
                except (OSError, SafetyError) as error:
                    skipped.append({'path': str(path), 'reason': str(error)})
        except (OSError, SafetyError) as error:
            skipped.append({'path': str(folder), 'reason': str(error)})
    # Temp bundles are treated as units: one new/protected/unreadable member
    # prevents a partial removal that could break an ongoing workflow.
    if rule['kind'] == 'reviewed_temp' and skipped:
        return [], skipped + [{'path': str(root), 'reason': 'Entire temp bundle retained because some members are ineligible.'}]
    return entries, skipped


def make_plan(policy_path: Path, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    policy = load_policy(policy_path)
    entries, skipped = [], []
    for rule in policy['rules']:
        found, ignored = scan_rule(rule, policy, now)
        entries.extend(found)
        skipped.extend(ignored)
    entries.sort(key=lambda e: e['source'])
    plan = {'version': VERSION, 'created_at': now, 'expires_at': now + DAY,
            'policy': policy, 'policy_hash': digest(policy), 'entries': entries,
            'skipped': skipped, 'logical_bytes': sum(e['fingerprint']['st_size'] for e in entries),
            'notice': 'Logical eligible bytes, not guaranteed reclaimed disk space. Nothing has been removed.'}
    plan['plan_id'] = digest(plan)
    return plan


def active_processes() -> list[dict]:
    if os.name != 'nt':
        raise SafetyError('Mutations require Windows process and exclusive-file checks.')
    command = 'Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object ProcessId,Name | ConvertTo-Json -Compress'
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, timeout=30, check=True,
                                creationflags=0x08000000)
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not data:
            raise ValueError('Empty or malformed process inventory.')
        blocked = []
        for row in data:
            pid = int(row['ProcessId'])
            name = str(row['Name'])
            if pid != os.getpid() and Path(name).stem.casefold() in BLOCKED_PROCESSES:
                blocked.append({'pid': pid, 'name': name})
        return blocked
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise SafetyError('Cannot verify that AI applications are stopped; no changes allowed.') from error


def require_idle(probe=None) -> None:
    running = (probe or active_processes)()
    if running:
        raise SafetyError('Finish/close AI applications and tool processes first; nothing was stopped: ' +
                          ', '.join(str(p.get('name', 'unknown')) for p in running))


def sha256_file(path: Path) -> str:
    with path.open('rb') as stream:
        return hash_stream(stream)


def hash_stream(stream) -> str:
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        value.update(block)
    return value.hexdigest()


def _win_api():
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


@contextlib.contextmanager
def exclusive_file(path: Path):
    """Open the exact Windows file with no sharing; delete by held handle only."""
    if os.name != 'nt':
        raise SafetyError('Exclusive mutation handles are implemented only on Windows.')
    import msvcrt
    api = _win_api()
    handle = api.CreateFileW(str(path), 0x80000000 | 0x10000, 0, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), 'File is locked or cannot be opened exclusively', str(path))
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'rb') as stream:
        def delete(remove=True):
            flag = ctypes.c_ubyte(bool(remove))
            if not api.SetFileInformationByHandle(handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
                raise OSError(ctypes.get_last_error(), 'Could not mark verified handle for deletion')
        yield stream, delete


def reject_streams(path: Path) -> None:
    """Do not silently discard NTFS alternate streams during cross-volume copy."""
    if os.name != 'nt':
        return
    from ctypes import wintypes
    class StreamData(ctypes.Structure):
        _fields_ = [('size', ctypes.c_longlong), ('name', wintypes.WCHAR * 296)]
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.FindFirstStreamW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(StreamData), wintypes.DWORD]
    api.FindFirstStreamW.restype = wintypes.HANDLE
    api.FindNextStreamW.argtypes = [wintypes.HANDLE, ctypes.POINTER(StreamData)]
    api.FindNextStreamW.restype = wintypes.BOOL
    api.FindClose.argtypes = [wintypes.HANDLE]
    data = StreamData()
    handle = api.FindFirstStreamW(str(path), 0, ctypes.byref(data), 0)
    if handle == ctypes.c_void_p(-1).value:
        if ctypes.get_last_error() == 38:  # empty file on a supported filesystem
            return
        raise SafetyError('Cannot verify alternate streams; file retained.')
    try:
        while True:
            if data.name != '::$DATA':
                raise SafetyError('Alternate data stream present; file retained.')
            if not api.FindNextStreamW(handle, ctypes.byref(data)):
                if ctypes.get_last_error() != 38:
                    raise SafetyError('Cannot finish alternate-stream enumeration.')
                break
    finally:
        api.FindClose(handle)


def _copy_verified(stream, destination: Path) -> str:
    header = stream.read(16)
    if header == b'SQLite format 3\x00':
        raise SafetyError('SQLite database detected regardless of extension.')
    stream.seek(0)
    no_links(destination)
    with destination.open('xb') as target:
        shutil.copyfileobj(stream, target, 1024 * 1024)
        target.flush()
        os.fsync(target.fileno())
    stream.seek(0)
    expected = hash_stream(stream)
    if sha256_file(destination) != expected:
        raise SafetyError('Copy verification failed; source retained.')
    return expected


def _load_plan(path: Path, policy_path: Path, confirm: str, now: float) -> dict:
    plan = read_json(path)
    claimed = plan.get('plan_id')
    payload = {k: v for k, v in plan.items() if k != 'plan_id'}
    if plan.get('version') != VERSION or digest(payload) != claimed or confirm != claimed:
        raise SafetyError('Plan is modified or confirmation does not match its exact ID.')
    if not plan['created_at'] <= now <= plan['expires_at'] or plan['expires_at'] - plan['created_at'] > DAY:
        raise SafetyError('Plan is expired or dated in the future; make a fresh plan.')
    policy = load_policy(policy_path)
    if digest(policy) != plan['policy_hash'] or policy != plan['policy']:
        raise SafetyError('Policy changed; make a fresh plan.')
    rules = {r['root']: r for r in policy['rules']}
    seen = set()
    for entry in plan['entries']:
        source = absolute(entry['source'])
        rule = rules.get(entry['root'])
        if not rule or str(source) in seen or str(source.relative_to(Path(entry['root']))) != entry['relative']:
            raise SafetyError('Invalid, duplicate or out-of-scope plan entry.')
        seen.add(str(source))
    return plan


class Journal:
    """Linear write-ahead logging; avoid rewriting an N-file manifest N times."""
    def __init__(self, path: Path, manifest: dict):
        self.path = path.with_name('journal.jsonl')
        self.manifest_path = path
        self.manifest = manifest
        self.sequence = manifest.get('journal_sequence', 0)
        self.previous = manifest.get('journal_hash', manifest['run_id'])
        self.failed = False
        tail = manifest.pop('_journal_tail', None)
        if tail:
            # Only restore/purge reach this with a fully validated checkpoint.
            # Preserve the incomplete append before removing it from the log.
            try:
                no_links(self.path)
                with self.path.open('r+b') as stream:
                    if hash_stream(stream) != tail['sha256']:
                        raise SafetyError('Recovery journal changed during recovery.')
                    stream.seek(tail['offset'])
                    incomplete = stream.read()
                    saved = self.path.with_name('journal-incomplete-' + uuid.uuid4().hex + '.bin')
                    with saved.open('xb') as target:
                        target.write(incomplete)
                        target.flush()
                        os.fsync(target.fileno())
                    stream.truncate(tail['offset'])
                    stream.flush()
                    os.fsync(stream.fileno())
                    manifest['recovered_journal_tail'] = str(saved)
            except (OSError, SafetyError) as error:
                self.failed = True
                raise JournalError('Journal recovery stopped; retain the complete quarantine: ' + str(error)) from error

    def record(self, index: int) -> None:
        if self.failed:
            raise JournalError('Journal writer failed; restart recovery before further changes.')
        event = {'sequence': self.sequence + 1, 'previous': self.previous,
                 'index': index, 'entry': dict(self.manifest['entries'][index])}
        event['hash'] = digest(event)
        try:
            no_links(self.path)
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, SafetyError) as error:
            self.failed = True
            raise JournalError('Journal durability failed; all further changes stopped. Retain quarantine for recovery: ' + str(error)) from error
        self.sequence += 1
        self.previous = event['hash']
        self.manifest['journal_sequence'] = self.sequence
        self.manifest['journal_hash'] = self.previous

    def checkpoint(self) -> None:
        if self.failed:
            raise JournalError('Cannot checkpoint a failed journal writer.')
        write_json(self.manifest_path, self.manifest)


def replay_journal(path: Path, manifest: dict) -> None:
    journal = path.with_name('journal.jsonl')
    no_links(journal)
    if not journal.exists():
        if manifest.get('journal_sequence', 0):
            raise SafetyError('Recovery journal is missing.')
        return
    sequence, previous = 0, manifest['run_id']
    checkpoint = manifest.get('journal_sequence', 0)
    checkpoint_hash = manifest.get('journal_hash', previous)
    if checkpoint == 0 and checkpoint_hash != previous:
        raise SafetyError('Invalid initial journal checkpoint.')
    valid_bytes = 0
    journal_hash = hashlib.sha256()
    with journal.open('rb') as stream:
        for line in stream:
            journal_hash.update(line)
            if not line.endswith(b'\n'):
                if sequence < checkpoint:
                    raise SafetyError('Recovery journal is truncated before its checkpoint.')
                manifest['_journal_tail'] = {'offset': valid_bytes, 'sha256': journal_hash.hexdigest()}
                break
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError) as error:
                raise SafetyError('Recovery journal contains a corrupt committed record.') from error
            expected_hash = digest({k: v for k, v in event.items() if k != 'hash'})
            if event['hash'] != expected_hash or event['previous'] != previous or event['sequence'] != sequence + 1:
                raise SafetyError('Recovery journal is inconsistent; keep quarantine for manual recovery.')
            sequence += 1
            previous = event['hash']
            if sequence == checkpoint and previous != checkpoint_hash:
                raise SafetyError('Manifest and recovery journal disagree.')
            if sequence > checkpoint:
                i = event['index']
                if type(i) is not int or i < 0 or i >= len(manifest['entries']):
                    raise SafetyError('Invalid recovery journal index.')
                manifest['entries'][i] = event['entry']
            valid_bytes += len(line)
    if sequence < checkpoint:
        raise SafetyError('Recovery journal is truncated.')
    manifest['journal_sequence'] = sequence
    manifest['journal_hash'] = previous


def quarantine_temp_bundle(indices, manifest, journal, rules, now, process_probe, opener):
    """Lock and copy the whole reviewed bundle before marking any member deleted.

    A crash can interrupt the final delete phase; durable copied records retain
    every byte needed for recovery. This is not an atomic filesystem transaction.
    """
    records = [manifest['entries'][i] for i in indices]
    marked = []
    failure = None
    try:
        with contextlib.ExitStack() as handles:
            sources, backups = [], []
            # Every lock/ADS check happens before the first source removal.
            for record in records:
                source = Path(record['source'])
                rule = rules[record['root']]
                if file_check(source, rule, manifest['policy'], now) != record['fingerprint']:
                    raise SafetyError('Temp bundle changed after preflight.')
                reject_streams(source)
                stream, delete = handles.enter_context(opener(source))
                if fingerprint(os.fstat(stream.fileno())) != record['fingerprint']:
                    raise SafetyError('Temp bundle identity changed while locking.')
                sources.append((stream, delete))
            for index, record, (stream, _) in zip(indices, records, sources):
                target = Path(record['backup'])
                record['sha256'] = _copy_verified(stream, target)
                if record['sha256'] != record['content_hash']:
                    raise SafetyError('Temp bundle content changed after plan.')
                record['state'] = 'copied'
                journal.record(index)
                verified, _ = handles.enter_context(opener(target))
                if hash_stream(verified) != record['sha256']:
                    raise SafetyError('Temp quarantine verification failed.')
                backups.append(verified)
            require_idle(process_probe)
            # Confirm no newly added path appeared while the existing members
            # were locked. Metadata traversal does not reopen locked contents.
            root = Path(records[0]['root'])
            names, pending = set(), [root]
            while pending:
                folder = pending.pop()
                no_links(folder)
                with os.scandir(folder) as items:
                    for item in items:
                        p = Path(item.path)
                        no_links(p)
                        if item.is_dir(follow_symlinks=False):
                            pending.append(p)
                        elif item.is_file(follow_symlinks=False):
                            names.add(str(p))
            if names != {r['source'] for r in records}:
                raise SafetyError('Temp bundle gained or lost members; sources retained.')
            try:
                for _, delete in sources:
                    delete()
                    marked.append(delete)
            except BaseException:
                # Cancel pending disposition while handles remain open where
                # possible. The verified copies remain even if undo fails.
                for delete in marked:
                    try:
                        delete(False)
                    except OSError:
                        pass
                raise
        for record in records:
            record['state'] = 'quarantined'
    except (OSError, SafetyError) as error:
        failure = str(error)
        for record in records:
            record['state'] = 'retained_with_copy' if Path(record['backup']).exists() else 'skipped'
            record['reason'] = failure
    for index in indices:
        journal.record(index)


def apply_plan(plan_path: Path, policy_path: Path, quarantine_root: Path, confirm: str,
               *, now: float | None = None, process_probe=None, opener=exclusive_file) -> dict:
    now = time.time() if now is None else now
    plan = _load_plan(plan_path, policy_path, confirm, now)
    require_idle(process_probe)
    quarantine_root = absolute(quarantine_root)
    no_links(quarantine_root)
    if quarantine_root == Path(quarantine_root.anchor) or protected(quarantine_root) or project_ancestor(quarantine_root):
        raise SafetyError('Quarantine must be a separate directory outside protected data/projects.')
    if any(overlaps(quarantine_root, Path(r['root'])) for r in plan['policy']['rules']) or any(overlaps(quarantine_root, Path(p)) for p in plan['policy']['protected_roots']):
        raise SafetyError('Quarantine overlaps source or protected roots.')
    if not plan['entries']:
        return {'status': 'no-op', 'quarantined_bytes': 0, 'entries': []}
    quarantine_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(quarantine_root).free < plan['logical_bytes'] + 16 * 1024 * 1024:
        raise SafetyError('Insufficient quarantine space; source data retained.')
    run_id = uuid.uuid4().hex
    run = quarantine_root / run_id
    run.mkdir()
    (run / 'files').mkdir()
    manifest_path = run / 'manifest.json'
    rules = {r['root']: r for r in plan['policy']['rules']}
    source_volumes = {Path(e['source']).anchor for e in plan['entries']}
    before = {v: shutil.disk_usage(v).free for v in source_volumes}
    manifest = {'version': VERSION, 'run_id': run_id, 'created_at': now, 'retention_days': 7,
                'plan_id': plan['plan_id'], 'policy': plan['policy'], 'status': 'running',
                'manifest_path': str(manifest_path), 'entries': [
                    {**e, 'backup': str(run / 'files' / ('%08d.bin' % i)), 'state': 'pending'}
                    for i, e in enumerate(plan['entries'])], 'source_free_before': before,
                'notice': 'Same-volume quarantine does not free source-disk space. Copy is content-verified; original ACLs are not backed up.'}
    write_json(manifest_path, manifest, exclusive=True)
    journal = Journal(manifest_path, manifest)
    # Revalidate temp bundles as a whole before selecting their old members.
    invalid_temp = set()
    for rule in rules.values():
        if rule['kind'] == 'reviewed_temp':
            current, ignored = scan_rule(rule, plan['policy'], now)
            expected = [e for e in plan['entries'] if e['root'] == rule['root']]
            if ignored or sorted(current, key=lambda e: e['source']) != expected:
                invalid_temp.add(rule['root'])
    processed_temp = set()
    for i, entry in enumerate(plan['entries']):
        source = absolute(entry['source'])
        target = run / 'files' / ('%08d.bin' % i)
        record = manifest['entries'][i]
        if rules[entry['root']]['kind'] == 'reviewed_temp' and entry['root'] not in invalid_temp:
            if entry['root'] not in processed_temp:
                indices = [j for j, e in enumerate(plan['entries']) if e['root'] == entry['root']]
                quarantine_temp_bundle(indices, manifest, journal, rules, now, process_probe, opener)
                processed_temp.add(entry['root'])
            continue
        try:
            require_idle(process_probe)
            if entry['root'] in invalid_temp:
                raise SafetyError('Temp bundle changed since plan; entire bundle retained.')
            current = file_check(source, rules[entry['root']], plan['policy'], now)
            if current != entry['fingerprint']:
                raise SafetyError('Identity, size, links or timestamp changed since plan.')
            reject_streams(source)
            no_links(target)
            with opener(source) as (stream, delete):
                if fingerprint(os.fstat(stream.fileno())) != entry['fingerprint']:
                    changed = [k for k, v in fingerprint(os.fstat(stream.fileno())).items() if entry['fingerprint'][k] != v]
                    raise SafetyError('Opened file does not match reviewed metadata: ' + ', '.join(changed))
                record['sha256'] = _copy_verified(stream, target)
                if record['sha256'] != entry['content_hash']:
                    raise SafetyError('Contents changed since plan; source retained.')
                record['state'] = 'copied'
                journal.record(i)  # durable recovery record BEFORE removal
                no_links(source)
                no_links(target)
                with opener(target) as (verified_copy, _):
                    if hash_stream(verified_copy) != record['sha256']:
                        raise SafetyError('Quarantine changed before removal; source retained.')
                    if fingerprint(os.fstat(stream.fileno())) != entry['fingerprint']:
                        raise SafetyError('Source changed during copy.')
                    delete()
            record['state'] = 'quarantined'
        except (OSError, SafetyError) as error:
            record['state'] = 'retained_with_copy' if target.exists() else 'skipped'
            record['reason'] = str(error)
        journal.record(i)
    manifest['status'] = 'completed' if all(e['state'] == 'quarantined' for e in manifest['entries']) else 'partial'
    manifest['quarantined_bytes'] = sum(e['fingerprint']['st_size'] for e in manifest['entries'] if e['state'] == 'quarantined')
    manifest['source_free_after'] = {v: shutil.disk_usage(v).free for v in source_volumes}
    manifest['observed_free_delta'] = {v: manifest['source_free_after'][v] - before[v] for v in before}
    manifest['finished_at'] = time.time()
    journal.checkpoint()
    return manifest


def load_manifest(path: Path, confirm: str) -> dict:
    path = absolute(path)
    manifest = read_json(path)
    if manifest.get('manifest_hash') != digest({k: v for k, v in manifest.items() if k != 'manifest_hash'}):
        raise SafetyError('Manifest checksum changed; retain files for manual recovery.')
    if manifest.get('version') != VERSION or manifest.get('run_id') != confirm or path.parent.name != confirm or not re.fullmatch('[a-f0-9]{32}', confirm):
        raise SafetyError('Manifest/run confirmation mismatch.')
    if manifest.get('manifest_path') != str(path):
        raise SafetyError('Manifest was relocated; retain it for manual recovery.')
    replay_journal(path, manifest)
    for i, entry in enumerate(manifest['entries']):
        backup = absolute(entry['backup'])
        if backup != path.parent / 'files' / ('%08d.bin' % i):
            raise SafetyError('Backup escapes its exact quarantine slot.')
        no_links(backup)
        source = absolute(entry['source'])
        allowed = any(r['root'] == entry['root'] for r in manifest['policy']['rules'])
        explicitly_protected = any(inside(source, absolute(p)) for p in manifest['policy']['protected_roots'])
        if not allowed or explicitly_protected or str(source.relative_to(Path(entry['root']))) != entry['relative'] or protected(source) or project_ancestor(source.parent):
            raise SafetyError('Manifest source is no longer eligible; manual recovery required.')
    return manifest


def restore_manifest(path: Path, confirm: str, *, process_probe=None) -> dict:
    require_idle(process_probe)
    manifest = load_manifest(path, confirm)
    journal = Journal(path, manifest)
    for index, entry in enumerate(manifest['entries']):
        if entry['state'] not in {'quarantined', 'copied', 'retained_with_copy'} or 'sha256' not in entry:
            continue
        source, backup = Path(entry['source']), Path(entry['backup'])
        try:
            require_idle(process_probe)
            no_links(source)
            if source.exists():
                raise SafetyError('Original path exists; restore never overwrites it.')
            no_links(backup)
            if sha256_file(backup) != entry['sha256']:
                raise SafetyError('Quarantine content changed.')
            source.parent.mkdir(parents=True, exist_ok=True)
            partial = source.with_name('.codexcleaner-restore-' + uuid.uuid4().hex + '.partial')
            entry['restore_partial'] = str(partial)
            with backup.open('rb') as stream:
                if _copy_verified(stream, partial) != entry['sha256']:
                    raise SafetyError('Backup changed while restoring; original path retained.')
            os.utime(partial, ns=(entry['fingerprint']['st_mtime_ns'], entry['fingerprint']['st_mtime_ns']))
            no_links(source)
            if os.name == 'nt':
                partial.rename(source)  # Windows refuses an existing destination
            else:  # portable fixtures, not reachable from production mutation CLI
                os.link(partial, source)
                partial.unlink()
            entry['state'] = 'restored'
            entry.pop('restore_partial', None)
            entry.pop('restore_error', None)
        except (OSError, SafetyError) as error:
            entry['restore_error'] = str(error)
        journal.record(index)
    manifest['restore_status'] = 'partial' if any('restore_error' in e for e in manifest['entries']) else 'completed'
    journal.checkpoint()
    return manifest


def purge_manifest(path: Path, confirm: str, *, now: float | None = None,
                   process_probe=None, opener=exclusive_file) -> dict:
    require_idle(process_probe)
    now = time.time() if now is None else now
    manifest = load_manifest(path, confirm)
    journal = Journal(path, manifest)
    if now - max(manifest['created_at'], manifest.get('finished_at', manifest['created_at'])) < 7 * DAY:
        raise SafetyError('Quarantine must be retained for at least seven days.')
    for index, entry in enumerate(manifest['entries']):
        if entry['state'] not in {'quarantined', 'restored'}:
            continue  # interrupted/uncertain copies always require manual review
        backup = Path(entry['backup'])
        try:
            require_idle(process_probe)
            no_links(backup)
            reject_streams(backup)
            info = backup.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SafetyError('Quarantine file identity is not safe.')
            with opener(backup) as (stream, delete):
                if fingerprint(os.fstat(stream.fileno())) != fingerprint(info):
                    changed = [k for k, v in fingerprint(os.fstat(stream.fileno())).items() if fingerprint(info)[k] != v]
                    raise SafetyError('Quarantine metadata changed: ' + ', '.join(changed))
                if hash_stream(stream) != entry['sha256']:
                    raise SafetyError('Quarantine content changed; retained.')
                entry['purge_pending'] = True
                journal.record(index)
                delete()
            entry['state'] = 'purged'
            entry.pop('purge_pending', None)
            entry.pop('purge_error', None)
        except (OSError, SafetyError) as error:
            entry['purge_error'] = str(error)
        journal.record(index)
    manifest['purge_outstanding'] = [e['backup'] for e in manifest['entries'] if e['state'] != 'purged' and Path(e['backup']).exists()]
    manifest['purge_status'] = 'partial' if manifest['purge_outstanding'] or any('purge_error' in e for e in manifest['entries']) else 'completed'
    journal.checkpoint()
    return manifest


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('policy-template', help='Print a policy with no cleanup targets.')
    plan = sub.add_parser('plan', help='Create a reviewable manifest; no source changes.')
    plan.add_argument('--policy', type=Path, required=True)
    plan.add_argument('--output', type=Path, required=True)
    apply = sub.add_parser('apply', help='Quarantine exact approved, unchanged plan entries.')
    apply.add_argument('--plan', type=Path, required=True)
    apply.add_argument('--policy', type=Path, required=True)
    apply.add_argument('--quarantine-root', type=Path, required=True)
    apply.add_argument('--confirm', required=True, help='Exact full plan ID displayed by plan.')
    for name in ('restore', 'purge'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--manifest', type=Path, required=True)
        cmd.add_argument('--confirm', required=True, help='Exact quarantine run ID.')
    return p


def main(argv=None) -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    args = parser().parse_args(argv)
    try:
        if args.command == 'policy-template':
            report = {'version': VERSION, 'protected_roots': [], 'rules': []}
        elif args.command == 'plan':
            report = make_plan(args.policy.absolute())
            output = absolute(args.output.absolute())
            if any(inside(output, Path(r['root'])) for r in report['policy']['rules']):
                raise SafetyError('Store the plan outside its cleanup roots.')
            write_json(output, report, exclusive=True)
        elif args.command == 'apply':
            report = apply_plan(args.plan.absolute(), args.policy.absolute(), args.quarantine_root, args.confirm)
        elif args.command == 'restore':
            report = restore_manifest(args.manifest.absolute(), args.confirm)
        else:
            report = purge_manifest(args.manifest.absolute(), args.confirm)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 4 if any(report.get(k) == 'partial' for k in ('status', 'restore_status', 'purge_status')) else 0
    except (SafetyError, JournalError, OSError, ValueError, KeyError, TypeError) as error:
        print('Storage guard refused: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
