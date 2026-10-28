# SPDX-License-Identifier: MIT
"""ps for inspect runs, served from outside the inspect fence.

Seatbelt refuses to exec setuid binaries, and macOS /bin/ps is setuid root, so inside the inspect
fence a plain `ps` fails with "operation not permitted" and process checks end as capability gaps.
The caller stays outside the fence for the whole run. It serves /bin/ps over a Unix socket, and a shim
named ps, first on the fenced shells' PATH, relays its argv and returns stdout, stderr and the exit
status unchanged. Only /bin/ps runs, without a shell; ps has no option that writes files, so the
fence's write boundary is unchanged and process visibility matches the unsandboxed user's.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socketserver
import stat
import subprocess
import threading

PS = "/bin/ps"
SOCKET_ENV = "NOKIY_PS_BROKER"
# Locale and width settings change how ps formats lstart, etime and columns.
PASSED_ENV = ("TZ", "LANG", "LC_ALL", "LC_TIME", "COLUMNS")
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_ARGS = 256
_MAX_ARG_BYTES = 16 * 1024
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_TIMEOUT_SECONDS = 60


def socket_path(inspect_root: Path) -> Path:
    # macOS limits Unix socket paths to 104 bytes; the inspect root path is far longer.
    digest = hashlib.sha256(str(inspect_root).encode()).hexdigest()[:12]
    return Path("/private/tmp") / f"nkps-{digest}.sock"


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _reply(code: int, stdout: bytes = b"", stderr: bytes = b"") -> dict:
    return {"exit_code": code, "stdout": _encode(stdout), "stderr": _encode(stderr)}


def run_request(request: object, executable: str = PS) -> dict:
    """Run the executable with the request argv; never through a shell."""
    argv = request.get("argv") if isinstance(request, dict) else None
    passed = request.get("env", {}) if isinstance(request, dict) else None
    if (not isinstance(argv, list) or len(argv) > _MAX_ARGS or not isinstance(passed, dict)
            or any(not isinstance(arg, str) or "\x00" in arg or len(arg.encode()) > _MAX_ARG_BYTES
                   for arg in argv)):
        return _reply(2, stderr=b"ps broker: invalid request\n")
    env = {"PATH": "/usr/bin:/bin"}
    for key in PASSED_ENV:
        value = passed.get(key)
        if isinstance(value, str) and "\x00" not in value and len(value) <= 256:
            env[key] = value
    try:
        done = subprocess.run([executable, *argv], stdin=subprocess.DEVNULL, capture_output=True,
                              env=env, timeout=_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return _reply(124, stderr=f"ps broker: ps exceeded {_TIMEOUT_SECONDS} s\n".encode())
    except OSError as error:
        return _reply(126, stderr=f"ps broker: {error.strerror or error}\n".encode())
    stdout, stderr = done.stdout, done.stderr
    if len(stdout) > _MAX_OUTPUT_BYTES:
        stdout = stdout[:_MAX_OUTPUT_BYTES]
        stderr += f"ps broker: output truncated at {_MAX_OUTPUT_BYTES} bytes\n".encode()
    return _reply(done.returncode, stdout, stderr[:_MAX_OUTPUT_BYTES])


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        try:
            request = json.loads(line) if len(line) <= _MAX_REQUEST_BYTES else None
        except ValueError:
            request = None
        reply = run_request(request, self.server.executable)
        self.wfile.write(json.dumps(reply).encode() + b"\n")


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ProcessBroker:
    """Serve ps on the root's socket for the duration of one fenced run.

    A start failure leaves the run unchanged: the shim then reports that the broker is unavailable,
    so the worker sees a named gap instead of a silently different ps.
    """

    def __init__(self, path: Path, executable: str = PS):
        self.path, self.executable = Path(path), executable
        self.error: str | None = None
        self._server = None
        self._thread = None

    @classmethod
    def for_root(cls, inspect_root: Path) -> "ProcessBroker":
        return cls(socket_path(inspect_root))

    def __enter__(self) -> "ProcessBroker":
        try:
            try:
                mode = self.path.lstat().st_mode
            except FileNotFoundError:
                mode = None
            if mode is not None:
                # Only a stale socket of this root is replaced; anything else is left alone.
                if not stat.S_ISSOCK(mode):
                    raise OSError(f"{self.path} exists and is not a socket")
                self.path.unlink()
            server = _Server(str(self.path), _Handler)
            server.executable = self.executable
            os.chmod(self.path, 0o600)
        except OSError as error:
            self.error = str(error)
            return self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="nokiy-ps-broker", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        return False


_SHIM = """#!{python} -I -S
# Nokiy inspect ps shim: relays to /bin/ps outside the fence (see process_broker.py).
import base64, json, os, socket, sys
path = os.environ.get({env!r})
if not path:
    sys.stderr.write("ps: the inspect process broker is not configured\\n")
    sys.exit(1)
try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(path)
    passed = {{key: os.environ[key] for key in {passed!r} if key in os.environ}}
    client.sendall(json.dumps({{"argv": sys.argv[1:], "env": passed}}).encode() + b"\\n")
    reply = json.loads(client.makefile("rb").readline())
except (OSError, ValueError) as error:
    sys.stderr.write("ps: the inspect process broker is not available: %s\\n" % error)
    sys.exit(1)
sys.stdout.buffer.write(base64.b64decode(reply["stdout"]))
sys.stdout.flush()
sys.stderr.buffer.write(base64.b64decode(reply["stderr"]))
sys.stderr.flush()
sys.exit(reply["exit_code"])
"""


def _zsh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _startup(home_file: str, zdot: Path, tail: str = "") -> str:
    # Load the user's own file as if ZDOTDIR were unset, then return to this directory so zsh keeps
    # reading the next startup file from here.
    return ("ZDOTDIR=\"$HOME\"\n"
            f"[[ -r \"$HOME/{home_file}\" ]] && source \"$HOME/{home_file}\"\n"
            f"ZDOTDIR={_zsh_quote(str(zdot))}\n" + tail)


def _write(path: Path, text: str, mode: int) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(text)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def shell_env(inspect_root: Path, python: str, path: str) -> dict[str, str]:
    """Install the ps shim and zsh startup files under the root's scratch; return the shell env.

    The runtime runs commands with zsh -lc, and /etc/zprofile's path_helper moves system
    directories ahead of an inherited PATH, so a PATH prefix alone would resolve ps to /bin/ps.
    ZDOTDIR startup files source the user's own .zshenv/.zprofile/.zlogin and put the shim first
    last; non-zsh shells and direct execs get the same order from PATH.
    """
    scratch = inspect_root / "scratch"
    bin_dir, zdot = scratch / "bin", scratch / "zdot"
    for directory in (bin_dir, zdot):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(bin_dir / "ps", _SHIM.format(python=python, env=SOCKET_ENV, passed=PASSED_ENV), 0o700)
    first = _zsh_quote(str(bin_dir))
    _write(zdot / ".zshenv", _startup(".zshenv", zdot), 0o600)
    _write(zdot / ".zprofile", _startup(".zprofile", zdot), 0o600)
    _write(zdot / ".zlogin", _startup(".zlogin", zdot,
           f"path=({first} ${{path:#{first}}})\n"), 0o600)
    return {"PATH": f"{bin_dir}:{path}" if path else str(bin_dir), "ZDOTDIR": str(zdot),
            SOCKET_ENV: str(socket_path(inspect_root))}

