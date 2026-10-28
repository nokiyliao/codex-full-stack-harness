#!/usr/bin/env python3
"""nokiy-nav: compact code navigation for coordinators.

Prints exact definition line ranges (and optionally call sites) for symbol names,
FQN fragments or paths, so a reader can open only those ranges instead of searching
and guessing. Uses the repository's full DCF index when one exists (complete
coverage, sub-second) and otherwise the installed Nokiy caller's navigate map, whose
coverage limits are reported rather than treated as absence.

usage: nokiy-nav [-C DIR] [--in PATH] [--refs] [--depth N] [--page N] TARGET [TARGET ...]
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

CALLER = Path(os.environ.get('NOKIY_NAV_CALLER') or Path.home() / '.local/bin/nokiy-embedded-run')
KINDS = {'python_function': 'fn', 'python_async_function': 'async fn', 'python_class': 'class',
         'python_method': 'fn', 'typescript_function': 'fn', 'typescript_class': 'class',
         'rust_function': 'fn', 'rust_struct': 'struct', 'rust_impl': 'impl'}


def git(args, cwd):
    try:
        return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def workspace_of(start):
    return Path(git(['rev-parse', '--show-toplevel'], start) or start).resolve()


def interpreter(ws):
    candidates = [ws / '.venv/bin/python']
    common = git(['rev-parse', '--path-format=absolute', '--git-common-dir'], ws)
    if common:
        candidates.append(Path(common).parent / '.venv/bin/python')
    return next((str(p) for p in candidates if p.exists()), None)


def short(symbol, path):
    module = path.rsplit('.', 1)[0].replace('/', '.')
    return symbol[len(module) + 1:] if symbol.startswith(module + '.') else symbol


def dcf_query(ws, target, page, depth):
    dcf = ws / 'scripts/ops/dcf.py'
    python = interpreter(ws)
    if not (dcf.is_file() and (ws / 'output/dcf_runtime/v2/current.json').is_file() and python):
        return None
    cmd = [python, '-B', str(dcf), '--root', str(ws), 'query', '--capability', 'source-navigation',
           '--target', target, '--projection', 'locations', '--depth', str(depth), '--json']
    if page:
        cmd += ['--offset', str(page)]
    proc = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=120)
    try:
        payload = json.loads(proc.stdout)
        result = payload['result']
    except (ValueError, KeyError, TypeError):
        return {'error': (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ['dcf query failed']}
    meta = result.get('_projection') or {}
    refs = [dict(zip(result.get('reference_fields') or [], row)) for row in result.get('references') or []]
    return {'backend': 'dcf ' + str(payload.get('freshness_status')) + ' ' + str(payload.get('generation_id', ''))[:15],
            'definitions': result.get('definitions') or [], 'references': refs,
            'reference_count': result.get('reference_count', len(refs)),
            'next': meta.get('next_offset') or None, 'notes': []}


def caller_query(ws, targets, scope, page):
    if not CALLER.exists():
        return {'error': ['no DCF index and no installed Nokiy caller']}
    cmd = [str(CALLER), 'navigate', '--workspace', str(ws), '--read-path', scope or '.', '--projection', 'locations']
    for target in targets:
        cmd += ['--query-any', target]
    if page:
        cmd += ['--offset', str(page)]
    proc = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=300)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return {'error': (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ['navigate failed']}
    if payload.get('status') != 'NAVIGATION_AVAILABLE':
        return {'error': [str(payload.get('status')) + ' ' + str(payload.get('first_typed_blocker') or payload.get('message') or '')]}
    coverage = payload.get('coverage') or {}
    notes = []
    deferred = coverage.get('deferred_scopes') or []
    if deferred:
        notes.append(f'map deferred {len(deferred)} scopes (not searched; narrow with --in): ' + ', '.join(deferred[:6]))
    if coverage.get('parse_failure_count'):
        notes.append(f"{coverage['parse_failure_count']} files failed to parse")
    if coverage.get('unsupported_extensions') and not payload.get('symbols'):
        notes.append('unsupported: ' + ', '.join(sorted(coverage['unsupported_extensions'])))
    return {'backend': 'nokiy-map ' + str(payload.get('map_sha256', ''))[:8], 'definitions': payload.get('symbols') or [],
            'references': [], 'reference_count': 0, 'next': payload.get('next_offset'), 'paged': 'definitions',
            'notes': notes}


def render(label, res, scope, want_refs):
    defs = [d for d in res['definitions'] if not scope or d['path'] == scope or d['path'].startswith(scope.rstrip('/') + '/')]
    refs = [r for r in res['references'] if r.get('targets')] if want_refs else []
    hidden = len(res['references']) - len([r for r in res['references'] if r.get('targets')])
    head = f"# {label}: {res['backend']} | {len(defs)} definitions"
    if want_refs:
        head += f" | refs {len(refs)} shown of {res['reference_count']}" + (f' ({hidden} unresolved omitted)' if hidden else '')
    if res['next'] and (want_refs or res.get('paged') == 'definitions'):
        head += f" | more: --page {res['next']}"
    lines = [head, *('# ' + note for note in res['notes'])]
    # Exact name matches first (whole short name or FQN suffix), then the rest by path.
    names = {t.split('/')[-1] for t in label.split()}

    def exact(d):
        name = short(d['symbol'], d['path'])
        return any(name == n or d['symbol'].endswith('.' + n) or d['symbol'] == n for n in names)
    current = None
    for d in sorted(defs, key=lambda d: (not exact(d), d['path'], d.get('line') or 0)):
        if d['path'] != current:
            current = d['path']
            lines.append(current)
        span = f"{d.get('line')}-{d.get('end_line')}" if d.get('end_line') not in (None, d.get('line')) else str(d.get('line'))
        lines.append(f"  {span:>11} {KINDS.get(d.get('kind'), d.get('kind') or '?')} {short(d['symbol'], d['path'])}")
    for r in refs:
        arrow = '->' if r.get('direction') == 'out' else '<-'
        mark = '' if r.get('confidence') == 'exact' else f" [{r.get('confidence')}]"
        lines.append(f"  {r['path']}:{r['line']} {short(str(r.get('caller')), r['path'])} {arrow} {r.get('targets')}{mark}")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(prog='nokiy-nav', description=__doc__.split('\n')[0])
    parser.add_argument('targets', nargs='+', metavar='TARGET', help='symbol name, FQN fragment or path')
    parser.add_argument('-C', '--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--in', dest='scope', default='', help='limit to this relative path')
    parser.add_argument('--refs', action='store_true', help='also list resolved call sites')
    parser.add_argument('--depth', type=int, default=1, choices=range(1, 5),
                        help='DCF call-graph depth; 1 = the target only, 2+ adds neighbouring definitions')
    parser.add_argument('--page', type=int, default=0)
    args = parser.parse_args()
    ws = workspace_of(args.workspace)
    found, failed, blocks = False, False, []
    dcf_results = {t: dcf_query(ws, t, args.page, args.depth) for t in args.targets}
    if all(r is not None and 'error' not in r for r in dcf_results.values()):
        for target, res in dcf_results.items():
            blocks.append(render(target, res, args.scope, args.refs))
            found = found or bool(res['definitions'] or res['references'])
    else:
        res = caller_query(ws, args.targets, args.scope, args.page)
        errors = [e for r in dcf_results.values() if r and 'error' in r for e in r['error']]
        if 'error' in res:
            print('nokiy-nav: ' + '; '.join(errors + res['error']), file=sys.stderr)
            return 2
        if errors:
            res['notes'].insert(0, 'DCF query failed, used the caller map: ' + '; '.join(errors)[:200])
        blocks.append(render(' '.join(args.targets), res, '', args.refs))
        found = bool(res['definitions'])
    print('\n'.join(blocks))
    return 0 if found else 1


if __name__ == '__main__':
    sys.exit(main())
