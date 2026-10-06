"""Private, single-use verifier request transport (not an executor)."""

import json
import math
import re
import select
import socket
import threading
import time
from typing import Callable


class ChannelError(RuntimeError):
    """Invalid channel configuration or an incomplete channel shutdown."""


_BINDING = re.compile(r"[0-9a-f]{64}\Z")
_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
_REQUEST_KEYS = {"version", "binding", "call_id", "verifier_index"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite JSON number: " + value)


def _stop_socket(sock):
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # Already disconnected or closed.
        sock.close()


class VerifierChannel:
    """One sequential request stream; handler runs on the server thread only."""

    def __init__(
        self,
        binding: str,
        count: int,
        handler: Callable[[int, str, threading.Event], dict],
        *,
        timeout_seconds: float = 330,
    ):
        if not isinstance(binding, str) or _BINDING.fullmatch(binding) is None:
            raise ValueError("binding must be exactly 64 lowercase hex characters")
        if type(count) is not int or not 1 <= count <= 8:
            raise ValueError("count must be an integer from 1 to 8")
        if not callable(handler):
            raise TypeError("handler must be callable")
        if (isinstance(timeout_seconds, bool) or
                not isinstance(timeout_seconds, (int, float)) or
                not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be positive and finite")
        self.binding = binding
        self.count = count
        self.handler = handler
        self.timeout_seconds = float(timeout_seconds)
        self._cancel = threading.Event()
        self._server = None
        self._router = None
        self._thread = None
        self._buffer = bytearray()
        self._seen = set()
        self._failure = None
        self._entered = False

    @property
    def failure(self):
        return self._failure

    @property
    def router_fd(self):
        if self._router is None or self._router.fileno() < 0:
            raise ChannelError("router endpoint is not available")
        return self._router.fileno()

    def __enter__(self):
        if self._entered:
            raise ChannelError("channel cannot be entered twice")
        self._entered = True
        try:
            self._server, self._router = socket.socketpair(
                socket.AF_UNIX, socket.SOCK_STREAM
            )
            self._server.set_inheritable(False)
            self._router.set_inheritable(False)
            thread = threading.Thread(target=self._serve, name="verifier-channel", daemon=False)
            thread.start()
            self._thread = thread
        except BaseException:
            self.close()
            raise
        return self

    def release_router_copy(self):
        """Drop the parent's copy after the child has inherited/passed the fd."""
        if self._router is not None:
            self._router.close()
            self._router = None

    def close(self):
        self._cancel.set()
        _stop_socket(self._router)
        self._router = None
        _stop_socket(self._server)
        thread = self._thread
        if thread is not None:
            if thread is threading.current_thread():
                raise ChannelError("cannot join the channel from its server thread")
            thread.join(min(self.timeout_seconds, 30))
            if thread.is_alive():
                raise ChannelError("channel server thread did not stop")

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _fail(self, reason):
        if not self._cancel.is_set():
            self._failure = reason
            self._cancel.set()
        _stop_socket(self._server)

    def _read_frame(self):
        # A frame boundary may stay idle for the entire worker lifetime. Once
        # bytes arrive, bound the whole partial frame (not each subsequent recv).
        deadline = time.monotonic() + self.timeout_seconds if self._buffer else None
        while not self._cancel.is_set():
            end = self._buffer.find(b"\n")
            if end >= 0:
                if end + 1 > 4096:
                    raise ChannelError("request frame exceeds 4096 bytes")
                frame = bytes(self._buffer[:end])
                del self._buffer[:end + 1]
                return frame
            if len(self._buffer) >= 4096:
                raise ChannelError("request frame exceeds 4096 bytes")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise ChannelError("request read timeout")
            self._server.settimeout(remaining)
            data = self._server.recv(4096)
            if not data:
                if self._buffer:
                    raise ChannelError("partial request at EOF")
                return None  # Clean EOF at a frame boundary.
            if deadline is None:
                deadline = time.monotonic() + self.timeout_seconds
            self._buffer.extend(data)
        return None

    def _validate(self, frame):
        request = json.loads(
            frame.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
            raise ChannelError("invalid request fields")
        if type(request["version"]) is not int or request["version"] != 1:
            raise ChannelError("invalid request version")
        if request["binding"] != self.binding:
            raise ChannelError("wrong binding")
        call_id = request["call_id"]
        if not isinstance(call_id, str) or _CALL_ID.fullmatch(call_id) is None:
            raise ChannelError("invalid call_id")
        index = request["verifier_index"]
        if type(index) is not int or not 0 <= index < self.count:
            raise ChannelError("invalid verifier_index")
        return call_id, index

    def _serve(self):
        try:
            while not self._cancel.is_set():
                frame = self._read_frame()
                if frame is None:
                    break
                call_id, index = self._validate(frame)
                if call_id in self._seen:
                    raise ChannelError("duplicate call_id")
                if len(self._seen) >= 1024:
                    raise ChannelError("call limit exceeded")
                self._seen.add(call_id)  # Claim before invoking, never retry.
                if self._cancel.is_set():
                    break
                if self._buffer:
                    raise ChannelError("pipelined verifier requests are not allowed")
                finished = threading.Event()

                def watch_disconnect():
                    try:
                        deadline = time.monotonic() + self.timeout_seconds
                        while not finished.wait(.05) and not self._cancel.is_set():
                            if time.monotonic() >= deadline:
                                self._fail("handler timeout")
                                return
                            readable, _, _ = select.select([self._server], [], [], 0)
                            if readable:
                                data = self._server.recv(1, socket.MSG_PEEK)
                                self._fail("pipelined request" if data else "client disconnected during execution")
                                return
                    except (OSError, ValueError) as exc:
                        if not self._cancel.is_set():
                            self._fail(str(exc))

                watcher = threading.Thread(target=watch_disconnect, name="verifier-peer", daemon=False)
                watcher.start()
                try:
                    result = self.handler(index, call_id, self._cancel)
                finally:
                    finished.set()
                    watcher.join()
                if self._cancel.is_set():
                    break
                if not isinstance(result, dict):
                    raise ChannelError("handler result must be a dict")
                reply = {
                    "version": 1, "binding": self.binding, "call_id": call_id,
                    "verifier_index": index, "result": result,
                }
                encoded = (json.dumps(
                    reply, ensure_ascii=True, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                ) + "\n").encode("utf-8")
                if len(encoded) > 262144:
                    raise ChannelError("reply frame exceeds 262144 bytes")
                self._server.settimeout(self.timeout_seconds)
                self._server.sendall(encoded)
        except Exception as exc:
            if not self._cancel.is_set():
                self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            _stop_socket(self._server)
