"""Task-local exact-URL GET evidence; no model, shell, credentials or daemon."""

import codecs
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import urllib.error
import urllib.parse
import urllib.request


MAX_BODY_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
TIMEOUT_SECONDS = 30
SCHEMA_VERSION = 'nokiy_http_evidence_v1'
_PAYLOAD_FIELDS = {'operation', 'artifact_root', 'urls', 'observation_id'}
_OBSERVATION_FIELDS = {
    'url', 'http_status', 'observed_at', 'body_path', 'body_sha256',
    'body_bytes', 'complete', 'utf8', 'issue',
}
_ISSUES = {
    None, 'http_error', 'redirect_forbidden', 'transport_error', 'oversize',
    'non_utf8', 'content_length_mismatch', 'invalid_content_length',
    'invalid_http_status', 'authenticated_proxy_forbidden',
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False)


def _validate(payload, thread):
    if (type(thread) is not str or not re.fullmatch(
            r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', thread)):
        raise ValueError('A native UUID thread identity is required')
    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        raise ValueError('Exact read_http payload fields are required')
    if payload['operation'] != 'read_http':
        raise ValueError('Only read_http is supported')
    observation_id = payload['observation_id']
    if (type(observation_id) is not str or not re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', observation_id)):
        raise ValueError('A bounded observation identity is required')
    root = payload['artifact_root']
    if (type(root) is not str or not root.startswith('/') or root.startswith('//')
            or os.path.normpath(root) != root or '\x00' in root):
        raise ValueError('An existing canonical absolute artifact directory is required')
    urls = payload['urls']
    if (type(urls) is not list or not 1 <= len(urls) <= 4
            or any(type(url) is not str for url in urls) or len(set(urls)) != len(urls)):
        raise ValueError('One through four distinct exact HTTPS URLs are required')
    for url in urls:
        if (not url or len(url.encode('utf-8')) > 8192 or '*' in url or '#' in url
                or '\\' in url or any(char.isspace() or ord(char) < 32
                                       or ord(char) == 127 for char in url)):
            raise ValueError('Exact bounded HTTPS URLs without controls or wildcards are required')
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise ValueError('A valid exact HTTPS URL is required') from error
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.fragment
                or parsed.username is not None or parsed.password is not None
                or '@' in parsed.netloc or (port is not None and not 1 <= port <= 65535)):
            raise ValueError('HTTPS without credentials or fragments is required')
    return {**payload, 'urls': list(urls)}


def _open_directory(path):
    # Anchored descriptors never follow a swapped parent or final symlink.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path[1:].split('/') if path != '/' else ():
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _same_directory(path, fd):
    current = _open_directory(path)
    try:
        before, after = os.fstat(fd), os.fstat(current)
        return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    finally:
        os.close(current)


def _read_regular(directory, filename, limit):
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > limit):
            raise ValueError('A bounded unique regular evidence file is required')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        named = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        if (len(data) > limit or len(data) != after.st_size
                or (after.st_dev, after.st_ino) != (named.st_dev, named.st_ino)
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('Evidence changed while being read')
        return data
    finally:
        os.close(fd)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate receipt field')
        result[key] = value
    return result


def _source_context(snapshot, observations):
    return {
        'workspace': snapshot,
        'read_scopes': [Path(item['body_path']).name for item in observations
                        if item['complete'] and item['utf8']],
        'source_read': True, 'command_templates': [], 'operations': ['read', 'command'],
    }


def _status(observations):
    return ('EVIDENCE_AVAILABLE' if all(
        item['issue'] is None and item['complete'] and item['utf8']
        and type(item['http_status']) is int and 200 <= item['http_status'] < 300
        for item in observations) else 'EVIDENCE_PARTIAL')


def _unknown(payload, thread, digest, snapshot, cached, reason):
    return {
        'schema_version': SCHEMA_VERSION, 'native_thread_id': thread,
        'observation_id': payload['observation_id'], 'request_sha256': digest,
        'snapshot_dir': snapshot, 'receipt_path': str(Path(snapshot) / 'receipt.json'),
        'status': 'UNKNOWN', 'cached': cached, 'reason': reason, 'observations': [],
        'source_context': _source_context(snapshot, []),
    }


def _cached(directory, payload, thread, digest, snapshot):
    data = _read_regular(directory, 'receipt.json', MAX_RECEIPT_BYTES)
    receipt = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object)
    if (type(receipt) is not dict or set(receipt) != {
            'schema_version', 'native_thread_id', 'observation_id', 'request_sha256',
            'snapshot_dir', 'receipt_path', 'payload', 'status', 'observations', 'source_context'}
            or receipt['schema_version'] != SCHEMA_VERSION
            or receipt['native_thread_id'] != thread or receipt['payload'] != payload
            or receipt['observation_id'] != payload['observation_id']
            or receipt['request_sha256'] != digest or receipt['snapshot_dir'] != snapshot
            or receipt['receipt_path'] != str(Path(snapshot) / 'receipt.json')):
        raise ValueError('Receipt identity mismatch')
    observations = receipt['observations']
    if type(observations) is not list or len(observations) != len(payload['urls']):
        raise ValueError('Receipt observation mismatch')
    filenames = {'receipt.json'}
    for index, item in enumerate(observations, 1):
        filename = f'body-{index:02d}.txt'
        filenames.add(filename)
        if (type(item) is not dict or set(item) != _OBSERVATION_FIELDS
                or item['url'] != payload['urls'][index - 1]
                or item['body_path'] != str(Path(snapshot) / filename)
                or type(item['body_sha256']) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', item['body_sha256'])
                or type(item['body_bytes']) is not int or not 0 <= item['body_bytes'] <= MAX_BODY_BYTES
                or type(item['complete']) is not bool or type(item['utf8']) is not bool
                or (item['http_status'] is not None and (
                    type(item['http_status']) is not int or not 100 <= item['http_status'] <= 599))
                or type(item['observed_at']) is not str
                or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z', item['observed_at'])
                or (item['issue'] is not None and type(item['issue']) is not str)
                or item['issue'] not in _ISSUES):
            raise ValueError('Receipt body metadata mismatch')
        datetime.strptime(item['observed_at'], '%Y-%m-%dT%H:%M:%S.%fZ')
        body = _read_regular(directory, filename, MAX_BODY_BYTES)
        try:
            body.decode('utf-8')
            utf8 = True
        except UnicodeDecodeError:
            utf8 = False
        if (len(body) != item['body_bytes'] or hashlib.sha256(body).hexdigest() != item['body_sha256']
                or utf8 != item['utf8']
                or (item['issue'] is None and not (item['complete'] and item['utf8']
                    and type(item['http_status']) is int and 200 <= item['http_status'] < 300))
                or (item['issue'] == 'non_utf8' and item['utf8'])
                or (item['issue'] == 'http_error' and not (item['complete'] and item['utf8']
                    and type(item['http_status']) is int and not 200 <= item['http_status'] < 400))
                or (item['issue'] == 'redirect_forbidden' and not (item['complete'] and item['utf8']
                    and type(item['http_status']) is int and 300 <= item['http_status'] < 400))
                or (item['issue'] in {'oversize', 'transport_error', 'content_length_mismatch',
                                     'invalid_content_length', 'invalid_http_status',
                                     'authenticated_proxy_forbidden'} and item['complete'])
                or (item['issue'] == 'oversize' and item['body_bytes'] != MAX_BODY_BYTES)):
            raise ValueError('Receipt body identity mismatch')
    if (set(os.listdir(directory)) != filenames or receipt['status'] != _status(observations)
            or receipt['source_context'] != _source_context(snapshot, observations)
            or not _same_directory(snapshot, directory)):
        raise ValueError('Snapshot identity mismatch')
    return receipt


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise urllib.error.HTTPError(request.full_url, code, 'Redirects forbidden', headers, fp)


def _opener():
    proxies = urllib.request.getproxies()
    for value in proxies.values():
        parsed = urllib.parse.urlsplit(value if '://' in value else 'https://' + value)
        if parsed.username is not None or parsed.password is not None or '@' in parsed.netloc:
            raise ValueError('Authenticated proxy forbidden')
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies), _NoRedirect())


def _observe(directory, snapshot, index, url):
    filename = f'body-{index:02d}.txt'
    fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    item = {
        'url': url, 'http_status': None,
        'observed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        'body_path': str(Path(snapshot) / filename), 'body_sha256': '', 'body_bytes': 0,
        'complete': False, 'utf8': True, 'issue': None,
    }
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    response = None
    try:
        with os.fdopen(fd, 'wb', closefd=False) as body:
            try:
                try:
                    opener = _opener()
                except ValueError:
                    item['issue'] = 'authenticated_proxy_forbidden'
                    opener = None
                if opener is not None:
                    request = urllib.request.Request(url, method='GET')
                    try:
                        response = opener.open(request, timeout=TIMEOUT_SECONDS)
                    except urllib.error.HTTPError as error:
                        response = error
                    status = response.getcode()
                    if type(status) is not int or not 100 <= status <= 599:
                        item['issue'] = 'invalid_http_status'
                    else:
                        item['http_status'] = status
                    content_length = response.headers.get('Content-Length')
                    expected = None
                    if content_length is not None:
                        if not re.fullmatch(r'[0-9]+', content_length):
                            item['issue'] = 'invalid_content_length'
                        else:
                            expected = int(content_length)
                    while True:
                        chunk = response.read(min(64 * 1024, MAX_BODY_BYTES - item['body_bytes'] + 1))
                        if not chunk:
                            item['complete'] = item['issue'] is None
                            break
                        oversize = len(chunk) > MAX_BODY_BYTES - item['body_bytes']
                        chunk = chunk[:MAX_BODY_BYTES - item['body_bytes']]
                        body.write(chunk)
                        digest.update(chunk)
                        item['body_bytes'] += len(chunk)
                        if item['utf8']:
                            try:
                                decoder.decode(chunk, final=False)
                            except UnicodeDecodeError:
                                item['utf8'] = False
                        if oversize:
                            item['issue'] = 'oversize'
                            break
                    if (item['complete'] and expected is not None
                            and expected != item['body_bytes']):
                        item['complete'] = False
                        item['issue'] = 'content_length_mismatch'
            except (OSError, http.client.HTTPException, UnicodeError):
                item['complete'] = False
                item['issue'] = 'transport_error'
            finally:
                if response is not None:
                    response.close()
            if item['utf8']:
                try:
                    decoder.decode(b'', final=True)
                except UnicodeDecodeError:
                    item['utf8'] = False
            if item['issue'] is None:
                if not item['utf8']:
                    item['issue'] = 'non_utf8'
                elif 300 <= item['http_status'] < 400:
                    item['issue'] = 'redirect_forbidden'
                elif not 200 <= item['http_status'] < 300:
                    item['issue'] = 'http_error'
            body.flush()
            os.fsync(fd)
        item['body_sha256'] = digest.hexdigest()
        return item
    finally:
        os.close(fd)


def read_http_evidence(payload, thread):
    """Read once, or verify a prior observation without any network replay."""
    payload = _validate(payload, thread)
    root = payload['artifact_root']
    try:
        root_fd = _open_directory(root)
    except OSError as error:
        raise ValueError('An existing canonical non-symlink artifact directory is required') from error
    digest = hashlib.sha256(canonical({'thread': thread, 'payload': payload}).encode('utf-8')).hexdigest()
    dirname = 'native-http-' + digest
    snapshot = str(Path(root) / dirname)
    cached = False
    directory = None
    try:
        try:
            os.mkdir(dirname, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            cached = True
        directory = os.open(dirname, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _unknown(payload, thread, digest, snapshot, cached, 'observation_in_progress')
        if not _same_directory(root, root_fd) or not _same_directory(snapshot, directory):
            return _unknown(payload, thread, digest, snapshot, cached, 'artifact_identity_changed')
        if not cached:
            observations = [_observe(directory, snapshot, index, url)
                            for index, url in enumerate(payload['urls'], 1)]
            receipt = {
                'schema_version': SCHEMA_VERSION, 'native_thread_id': thread,
                'observation_id': payload['observation_id'], 'request_sha256': digest,
                'snapshot_dir': snapshot, 'receipt_path': str(Path(snapshot) / 'receipt.json'),
                'payload': payload, 'status': _status(observations), 'observations': observations,
                'source_context': _source_context(snapshot, observations),
            }
            receipt_fd = os.open('receipt.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(receipt_fd, 'wb') as stream:
                stream.write((canonical(receipt) + '\n').encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
            os.fsync(root_fd)
        receipt = _cached(directory, payload, thread, digest, snapshot)
        if not _same_directory(root, root_fd):
            return _unknown(payload, thread, digest, snapshot, cached, 'artifact_identity_changed')
        return {**receipt, 'cached': cached}
    except (OSError, ValueError, UnicodeError, RecursionError):
        return _unknown(payload, thread, digest, snapshot, cached, 'snapshot_unavailable_or_inconsistent')
    finally:
        if directory is not None:
            os.close(directory)
        os.close(root_fd)
