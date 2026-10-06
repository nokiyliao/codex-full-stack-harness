"""Focused protocol tests for the private verifier transport."""

import json
import os
import socket
import threading
import unittest
from itertools import count
from unittest.mock import Mock, call, patch

from codex_collaboration_harness import verifier_channel
from codex_collaboration_harness.verifier_channel import ChannelError, VerifierChannel


BINDING = "a" * 64


def client_for(channel):
    client = socket.socket(fileno=os.dup(channel.router_fd))
    client.settimeout(2)
    channel.release_router_copy()
    return client


def request(call_id="first", binding=BINDING, index=0, **extra):
    return dict(version=1, binding=binding, call_id=call_id,
                verifier_index=index, **extra)


def send(client, message):
    client.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode())


def reply(client):
    frame = bytearray()
    while not frame.endswith(b"\n"):
        chunk = client.recv(4096)
        if not chunk:
            raise AssertionError("server closed before reply")
        frame.extend(chunk)
    return json.loads(frame)


class VerifierChannelTests(unittest.TestCase):
    def test_frame_boundaries_have_no_idle_timeout(self):
        channel = VerifierChannel(BINDING, 1, lambda *_: {}, timeout_seconds=1)
        channel._server = Mock()
        channel._server.recv.side_effect = [b"first\n", b"second\n"]
        with patch.object(verifier_channel.time, "monotonic", side_effect=[1000000, 2000000]):
            self.assertEqual(channel._read_frame(), b"first")
            self.assertEqual(channel._read_frame(), b"second")
        self.assertEqual(channel._server.settimeout.call_args_list, [call(None), call(None)])

    def test_partial_frame_deadline_does_not_reset_on_each_byte(self):
        channel = VerifierChannel(BINDING, 1, lambda *_: {}, timeout_seconds=1)
        channel._server = Mock()
        channel._server.recv.side_effect = [b"a", b"b", b"c\n"]
        with patch.object(verifier_channel.time, "monotonic", side_effect=[100, 100.25, 101.1]):
            with self.assertRaisesRegex(ChannelError, "request read timeout"):
                channel._read_frame()
        self.assertEqual(channel._server.recv.call_count, 2)
        self.assertEqual(channel._server.settimeout.call_args_list, [call(None), call(.75)])

    def test_close_cancels_and_joins_an_idle_channel(self):
        channel = VerifierChannel(BINDING, 1, lambda *_: {}, timeout_seconds=1)
        with channel, client_for(channel):
            channel.close()
            self.assertFalse(channel._thread.is_alive())
        self.assertIsNone(channel.failure)

    def test_handler_deadline_cancels_and_joins_without_idle_cutoff(self):
        finished = threading.Event()
        def handler(_index, _call, cancel):
            if cancel.wait(2):
                finished.set()
            return {}
        channel = VerifierChannel(BINDING, 1, handler, timeout_seconds=1)
        with patch.object(verifier_channel.time, "monotonic", side_effect=count(step=10)), \
                channel, client_for(channel) as client:
            send(client, request())
            self.assertEqual(client.recv(1), b"")
        self.assertTrue(finished.is_set())
        self.assertFalse(channel._thread.is_alive())
        self.assertEqual(channel.failure, "handler timeout")

    def test_input_validation_and_fd_lifetime(self):
        for binding, count in [("A" * 64, 1), (BINDING[:-1], 1),
                               (BINDING, True), (BINDING, 0), (BINDING, 9)]:
            with self.subTest(binding=binding, count=count):
                with self.assertRaises(ValueError):
                    VerifierChannel(binding, count, lambda *_: {})
        channel = VerifierChannel(BINDING, 1, lambda *_: {}, timeout_seconds=1)
        with channel:
            self.assertFalse(os.get_inheritable(channel.router_fd))
            with client_for(channel):
                with self.assertRaises(ChannelError):
                    _ = channel.router_fd
        self.assertIsNone(channel.failure)  # Clean EOF is not a failure.

    def test_distinct_calls_are_sequential_and_replied_to(self):
        calls = []

        def handler(index, call_id, cancel):
            calls.append((index, call_id, cancel.is_set()))
            return {"ok": True, "ordinal": len(calls)}

        channel = VerifierChannel(BINDING, 2, handler, timeout_seconds=1)
        with channel, client_for(channel) as client:
            send(client, request())
            self.assertEqual(reply(client), dict(version=1, binding=BINDING,
                call_id="first", verifier_index=0, result={"ok": True, "ordinal": 1}))
            send(client, request("second", index=1))
            self.assertEqual(reply(client), dict(version=1, binding=BINDING,
                call_id="second", verifier_index=1, result={"ok": True, "ordinal": 2}))
        self.assertEqual(calls, [(0, "first", False), (1, "second", False)])
        self.assertIsNone(channel.failure)

    def test_wrong_binding_and_unknown_field(self):
        for message, expected in [(request(binding="b" * 64), "binding"),
                                  (request(argv=["echo"]), "fields")]:
            with self.subTest(message=message):
                calls = []
                channel = VerifierChannel(BINDING, 1, lambda *args: calls.append(args) or {},
                                          timeout_seconds=1)
                with channel, client_for(channel) as client:
                    send(client, message)
                    self.assertEqual(client.recv(1), b"")
                self.assertIn(expected, channel.failure)
                self.assertEqual(calls, [])

    def test_duplicate_call_is_claimed_before_handler(self):
        calls = []
        channel = VerifierChannel(BINDING, 1, lambda i, c, e: calls.append(c) or {},
                                  timeout_seconds=1)
        with channel, client_for(channel) as client:
            send(client, request())
            self.assertEqual(reply(client)["result"], {})
            send(client, request())
            self.assertEqual(client.recv(1), b"")
        self.assertEqual(calls, ["first"])
        self.assertIn("duplicate", channel.failure)

    def test_oversized_request_and_reply(self):
        calls = []
        channel = VerifierChannel(BINDING, 1, lambda *args: calls.append(args) or {},
                                  timeout_seconds=1)
        with channel, client_for(channel) as client:
            client.sendall(b"x" * 4096)  # No newline within the frame limit.
            self.assertEqual(client.recv(1), b"")
        self.assertEqual(calls, [])
        self.assertIn("4096", channel.failure)

        channel = VerifierChannel(BINDING, 1, lambda *_: {"blob": "x" * 262144},
                                  timeout_seconds=1)
        with channel, client_for(channel) as client:
            send(client, request())
            self.assertEqual(client.recv(1), b"")
        self.assertIn("262144", channel.failure)

    def test_partial_eof_and_strict_json(self):
        channel = VerifierChannel(BINDING, 1, lambda *_: {}, timeout_seconds=1)
        with channel, client_for(channel) as client:
            client.sendall(b'{"version":1')
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(client.recv(1), b"")
        self.assertIn("partial", channel.failure)

        bad_frames = [
            b'{"version":1,"version":1}\n',
            b'{"version":NaN}\n',
            b'\xff\n',
            (json.dumps(request(index=True)) + "\n").encode(),
        ]
        for frame in bad_frames:
            with self.subTest(frame=frame):
                channel = VerifierChannel(BINDING, 1, lambda *_: {}, timeout_seconds=1)
                with channel, client_for(channel) as client:
                    client.sendall(frame)
                    self.assertEqual(client.recv(1), b"")
                self.assertIsNotNone(channel.failure)

    def test_blocking_handler_observes_close_and_is_joined(self):
        started = threading.Event()
        finished = threading.Event()

        def handler(index, call_id, cancel):
            started.set()
            if cancel.wait(2):
                finished.set()
            return {"done": True}

        channel = VerifierChannel(BINDING, 1, handler, timeout_seconds=1)
        with channel, client_for(channel) as client:
            send(client, request())
            self.assertTrue(started.wait(1))
        self.assertTrue(finished.is_set())
        self.assertIsNone(channel.failure)

    def test_peer_disconnect_cancels_inflight_handler(self):
        started, finished = threading.Event(), threading.Event()
        def handler(_index, _call, cancel):
            started.set()
            if cancel.wait(2):
                finished.set()
            return {}
        channel = VerifierChannel(BINDING, 1, handler, timeout_seconds=2)
        with channel:
            client = client_for(channel)
            send(client, request())
            self.assertTrue(started.wait(1))
            client.close()
            self.assertTrue(finished.wait(1))
        self.assertIn("disconnected", channel.failure)


if __name__ == "__main__":
    unittest.main()
