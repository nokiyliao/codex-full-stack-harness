# SPDX-License-Identifier: MIT
"""Task-scoped DCF navigation. Cache syntax only; recompute corpus resolution.

The two dcf_navigation_* modules are frozen copies of canonical DCF sources,
not independently maintained parsers. Their provenance lives in protocol/.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace

from . import embedded_nokiy as caller
from . import dcf_navigation_extract as extractor
from . import dcf_navigation_resolve as resolver
from .source_excerpt import MAX_OUTLINE_SOURCE_BYTES, _read_exact

CACHE_ROOT = Path('/Volumes/NOKIY-TB5/UTM/rebuildable_caches/nokiy-dcf-navigation')
SKIP = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'target', 'dist', 'build', 'output'}
MAX_FILES = 2048
MAX_BYTES = 64 * 1024 * 1024
# Navigation parses an outline, not an excerpt: allow large modules; MAX_BYTES still bounds the scope.
NAVIGATION_FILE_BYTES = MAX_OUTLINE_SOURCE_BYTES
PROJECTION_REVISION = 3


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def invalid(message):
    return caller.EmbeddedNokiyError('NOKIY_NAVIGATION_INVALID', message)


def inventory(workspace, paths):
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 32
            or any(not isinstance(p, str) for p in paths) or len(set(paths)) != len(paths)):
        raise invalid('read_paths requires distinct exact files or directories')
    pending = []
    for name in paths:
        if (not name or len(name) > 512 or any(c in name for c in '*?[]{}\\\x00\r\n')
                or (name != '.' and (Path(name).as_posix() != name or Path(name).is_absolute()
                    or any(p.startswith('.') or p in SKIP for p in name.split('/'))))):
            raise invalid('read_paths must stay within explicit non-hidden source scopes')
        node = workspace
        for part in (() if name == '.' else Path(name).parts):
            node /= part
            if node.is_symlink():
                raise invalid('read_paths cannot traverse links')
        if not node.exists():
            raise invalid('read_paths must exist')
        pending.append(node)
    found, excluded, unsupported, deferred = set(), 0, {}, []
    visited = set()
    while pending:
        node = pending.pop()
        if node in visited:
            continue
        visited.add(node)
        info = node.lstat()
        if stat.S_ISLNK(info.st_mode):
            excluded += 1
        elif stat.S_ISDIR(info.st_mode):
            # Never follow a newly swapped directory symlink during discovery.
            if node.resolve() != node:
                raise invalid('directory identity changed during navigation')
            children = sorted(node.iterdir(), reverse=True)
            pending.extend(p for p in children if not p.name.startswith('.') and p.name not in SKIP)
            excluded += sum(p.name.startswith('.') or p.name in SKIP for p in children)
        elif stat.S_ISREG(info.st_mode):
            if node.suffix == '.py':
                found.add(node.relative_to(workspace).as_posix())
            else:
                suffix = node.suffix or '<none>'
                unsupported[suffix] = unsupported.get(suffix, 0) + 1
        else:
            excluded += 1
        if len(found) >= MAX_FILES or len(visited) >= 20000:
            deferred = [p.relative_to(workspace).as_posix() for p in pending[:16]]
            break
    return sorted(found), {'excluded_entries': excluded, 'unsupported_extensions': unsupported,
                          'inventory_complete': not pending, 'deferred_scopes': deferred}


@contextmanager
def cache_directory(root, workspace):
    """A busy/unavailable rebuildable cache cannot block an authorized source read."""
    fd = None
    directory = None
    try:
        # The admitted TB5 parent must already exist; never recreate a missing volume.
        if not root.parent.is_dir() or root.parent.resolve() != root.parent:
            raise OSError('cache parent unavailable')
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise OSError('unsafe cache path')
        directory = root / digest(str(workspace).encode())
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise OSError('unsafe cache directory')
        fd = os.open(directory / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        directory = None
    try:
        yield directory
    finally:
        if fd is not None:
            os.close(fd)


def cached_rows(directory, name, data, revision):
    key = {'path': name, 'source_sha256': digest(data), 'parser_revision': revision}
    target = directory / (digest(name.encode()) + '.json') if directory else None
    if target:
        try:
            if target.is_symlink() or target.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('invalid cache')
            record = json.loads(target.read_bytes())
            rows = record['rows']
            if (record['identity'] == key and record['rows_sha256'] == digest(encoded(rows))
                    and isinstance(rows, list) and all(isinstance(row, dict) and row.get('path') == name
                        and {'entity_id', 'fqn', 'name', 'kind', 'path', 'line', 'summary', 'references', 'calls', 'navigation'} <= row.keys()
                        and isinstance(row['navigation'], dict) and 'end_line' in row['navigation']
                        and isinstance(row['calls'], list) and isinstance(row['references'], list) for row in rows)):
                return rows, True
        except (OSError, ValueError, KeyError, TypeError):
            pass
    rows = extractor.extract_python(data.decode('utf-8'), name)
    if target:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded({'identity': key, 'rows': rows, 'rows_sha256': digest(encoded(rows))}))
            os.replace(temporary, target)
        except OSError:
            pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return rows, False


def call_relations(calls):
    """Keep distinct edges and multiplicity, without repeating identical rows."""
    relations = {}
    for call in calls:
        if not call['candidate_ids']:
            continue
        key = (call['text'], call['confidence'], tuple(call['candidate_ids']))
        if key in relations:
            relation = relations[key]
            relation['occurrences'] = relation.get('occurrences', 1) + 1
        else:
            relations[key] = {'name': call['text'], 'confidence': call['confidence'],
                              'targets': call['candidate_ids']}
    return list(relations.values())


def navigate(workspace, read_paths, *, query='', queries=None, projection='full',
             offset=0, expected_map_sha256=None, expected_source_snapshot_sha256=None,
             cache_root=None):
    started = time.perf_counter()
    if not isinstance(query, str) or len(query) > 512 or type(offset) is not int or offset < 0:
        raise invalid('invalid query or offset')
    if not isinstance(projection, str) or projection not in ('full', 'locations'):
        raise invalid('projection must be full or locations')
    if queries is not None:
        if query or not isinstance(queries, list) or not 1 <= len(queries) <= 32:
            raise invalid('queries requires 1..32 strings and an empty scalar query')
        for term in queries:
            if not isinstance(term, str) or not term:
                raise invalid('queries requires nonempty strings')
            try:
                size = len(term.encode('utf-8'))
            except UnicodeEncodeError:
                raise invalid('queries must be valid UTF-8') from None
            if size > 256:
                raise invalid('each query must be at most 256 UTF-8 bytes')
        selection = sorted({term.casefold() for term in queries})
    else:
        selection = [query.casefold()] if query else []
    if 'ref:' in selection:
        raise invalid('ref: query requires a nonempty reference term')
    if offset and not expected_map_sha256:
        raise invalid('continuation requires expected_map_sha256')
    for expected in (expected_map_sha256, expected_source_snapshot_sha256):
        if expected is not None and (not isinstance(expected, str) or len(expected) != 64
                                     or any(char not in '0123456789abcdef' for char in expected)):
            raise invalid('expected navigation identity must be a lowercase SHA-256')
    workspace = caller._plain_path(workspace, name='workspace', directory=True)
    names, coverage = inventory(workspace, read_paths)
    revision = digest(Path(extractor.__file__).read_bytes() + Path(resolver.__file__).read_bytes())
    rows, sources, failures, fingerprints = [], {}, [], {}
    hits = misses = byte_count = 0
    with cache_directory(cache_root or CACHE_ROOT, workspace) as directory:
        for name in names:
            try:
                data = _read_exact(workspace, name, limit=NAVIGATION_FILE_BYTES)
                fingerprints[name] = digest(data)
                byte_count += len(data)
                if byte_count > MAX_BYTES:
                    failures.append({'path': name, 'reason': 'source_volume_requires_narrower_scope'})
                    break
                parsed, hit = cached_rows(directory, name, data, revision)
                sources[name] = digest(data)
                rows.extend(parsed)
                hits += hit
                misses += not hit
            except (caller.EmbeddedNokiyError, UnicodeError, SyntaxError, RecursionError, ValueError) as error:
                failures.append({'path': name, 'reason': type(error).__name__})
        cache_available = directory is not None
    # Failed parses still bind bytes; incomplete reads cannot certify snapshot reuse.
    snapshot_sha = (digest(encoded({'workspace': str(workspace), 'sources': fingerprints,
                                   'revision': revision, 'coverage': coverage, 'failures': failures,
                                   'read_paths': sorted(read_paths)}))
                    if coverage['inventory_complete'] and len(fingerprints) == len(names)
                    and byte_count <= MAX_BYTES else None)
    if expected_source_snapshot_sha256 is not None:
        if snapshot_sha is None:
            raise invalid('source snapshot unavailable; narrow scope or resolve unreadable sources')
        if expected_source_snapshot_sha256 != snapshot_sha:
            raise invalid('source snapshot changed; refresh navigation before reusing locations')
    # Re-resolve against only today's requested corpus: removed/new files and new
    # scopes must not leave yesterday's call targets in an otherwise warm map.
    resolved = (resolver.resolve_calls(SimpleNamespace(**row) for row in rows)
                if projection == 'full' else None)
    selected = [row for row in rows if not selection or any(
        (any(term[4:] in ref.casefold() for ref in row.get('references', []))
         if term.startswith('ref:') else term in (row['fqn'] + ' ' + row['path']).casefold())
        for term in selection)]
    map_sha = digest(encoded({'sources': sources, 'revision': revision, 'coverage': coverage,
                             'failures': failures, 'selection': selection, 'projection': projection,
                             'read_paths': sorted(read_paths),
                             'projection_revision': PROJECTION_REVISION,
                             'workspace': str(workspace), 'source_snapshot_sha256': snapshot_sha}))
    if expected_map_sha256 is not None and expected_map_sha256 != map_sha:
        raise invalid('navigation changed; restart from offset zero')
    if offset > len(selected):
        raise invalid('offset exceeds result')
    page = []
    for row in selected[offset:]:
        item = {'symbol': row['fqn'], 'path': row['path'], 'line': row['line'],
                'end_line': row['navigation']['end_line'], 'kind': row['kind'],
                'source_sha256': sources[row['path']]}
        if projection == 'full':
            item.update(summary=row['summary'], calls=call_relations(resolved[row['entity_id']]),
                        references=row.get('references', []))
        if len(encoded(page + [item])) > 24000:
            if not page:
                if projection == 'full':
                    item['calls'] = []
                    item['calls_omitted'] = True
                    item['references'] = []
                    item['references_omitted'] = True
                page.append(item)
            break
        page.append(item)
    next_offset = offset + len(page)
    return {'status': 'NAVIGATION_AVAILABLE', 'workspace': str(workspace),
            'context_kind': 'dcf_python_task_map', 'map_sha256': map_sha,
            'source_snapshot_sha256': snapshot_sha,
            'coverage': {**coverage, 'indexed_files': len(sources), 'parse_failures': failures[:16],
                         'parse_failure_count': len(failures), 'supported_languages': ['python'],
                         'complete_for_supported_scope': not failures and coverage['inventory_complete']},
            'symbols': page, 'matched_symbols': len(selected),
            'next_offset': next_offset if next_offset < len(selected) else None,
            'metrics': {'cache_state': 'warm' if hits and not misses else 'incremental' if hits else 'cold',
                        'cache_available': cache_available, 'file_cache_hits': hits, 'file_cache_misses': misses,
                        'source_bytes_read': byte_count, 'elapsed_seconds': time.perf_counter() - started},
            'authority_effect': 'none', 'selection_grants_read_write': False,
            'provider_execution_started': False,
            'notice': 'Syntax navigation only, not runtime behavior or write/deploy admission. Repeated identical call relations carry occurrences (otherwise one); this is not call-site order or runtime frequency. Scope exclusions and unsupported languages are not absence evidence. Use source SHA and exact line ranges for necessary behavior reads; reuse this map for planning, not a second whole-file discovery pass.'}
