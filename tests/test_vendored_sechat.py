"""Regression tests for the vendored sechat library.

We vendor `sechat/` (the upstream project is unmaintained) so we can fix a
crash that silently killed the room-listener thread: when SE chat sends a
non-string frame (a dict), `json.loads` raises `TypeError`, and upstream's
error handler then crashed trying to concatenate `"..." + data` where `data`
is a dict.  That unhandled exception killed the listener thread, leaving the
ingest daemon connected-looking but permanently deaf.

These tests exercise the fixed `Room.run` receive loop directly with a fake
socket so we don't need a live SE chat connection.
"""

import json
import threading
import time

import websocket

import sechat


class _FakeSession:
    """Minimal stand-in for requests.Session used during room shutdown."""

    def post(self, *args, **kwargs):
        class _R:
            text = "{}"
        return _R()


class _FakeSocket:
    """Feeds a scripted sequence of frames, then blocks forever."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False

    def recv(self):
        if self._frames:
            return self._frames.pop(0)
        # No more frames: block until told to stop (mimics a live socket).
        while not self.closed:
            time.sleep(0.05)
        # Empty payload: the loop's `data != ""` check skips it and, with
        # `running=False`, the while loop exits cleanly.
        return ""

    def close(self):
        self.closed = True


class _FakeRoom(sechat.Room):
    """A Room with a fake socket and no real network connect()."""

    def __init__(self, frames):
        # Bypass Room.__init__'s autoConnect entirely.
        self.session = _FakeSession()
        self._fkey = "fake"
        self.userID = 0
        self.roomID = 14524
        self.logRequestErrors = False
        self.cooldown = 2
        self.logger = __import__("logging").getLogger("Room-14524")
        self.thread = None
        self.socket = _FakeSocket(frames)
        self.running = False
        self.handlers = {i.value: set() for i in sechat.Events}
        self.internalHandlers = {
            sechat.Events.REPLY.value: self._replyHandler,
            sechat.Events.MENTION.value: self._replyHandler,
        }
        self.lastPing = 0
        self.processed = []

    def connect(self):
        # No-op: we already have a fake socket.
        pass

    def process(self, data):
        self.processed.append(data)

    def run(self):
        # Run the real (fixed) receive loop.
        super().run()


def _run_room(frames, timeout=3.0):
    room = _FakeRoom(frames)
    t = threading.Thread(target=room.run, daemon=True)
    t.start()
    # Give the thread a moment to consume all scripted frames.
    deadline = time.time() + timeout
    while time.time() < deadline and len(room.processed) < len(
        [f for f in frames if isinstance(f, str) and f != ""]
    ):
        time.sleep(0.02)
    # Stop the the fake socket so the loop exits.
    room.socket.close()
    room.running = False
    t.join(timeout=1.0)
    return room


def test_dict_frame_does_not_kill_listener():
    """A dict frame (non-string) must be logged and skipped, not crash."""
    good = json.dumps({"r14524": {"e": [{"event_type": 1, "message_id": 1}]}})
    room = _run_room([good, {"r14524": "non-string-dict"}, good])

    # The listener survived: it processed the valid frames on both sides of
    # the bad dict frame.
    assert len(room.processed) == 2


def test_malformed_json_frame_does_not_kill_listener():
    """Garbage JSON is skipped without killing the listener."""
    good = json.dumps({"r14524": {"e": [{"event_type": 1, "message_id": 2}]}})
    room = _run_room(["this is not json", good])

    assert len(room.processed) == 1


class _FakeSocketClosed(_FakeSocket):
    """A socket whose recv() always raises WebSocketConnectionClosedException
    (the old socket stays closed after a drop). The loop keeps hitting it and
    retrying connect() until connect() swaps in a fresh socket."""

    def __init__(self):
        super().__init__([])

    def recv(self):
        raise websocket.WebSocketConnectionClosedException("closed")


class _FakeRoomReconnect(_FakeRoom):
    """A Room whose connect() fails once (ConnectionError), then succeeds.

    This simulates the real-world failure: the ws-auth handshake times out
    during a reconnect. Before the fix, that ConnectionError escaped the
    receive loop and killed the listener thread.
    """

    def __init__(self):
        super().__init__([])
        self.socket = _FakeSocketClosed()
        self._connect_calls = 0

    def connect(self):
        self._connect_calls += 1
        if self._connect_calls == 1:
            # First reconnect attempt fails (e.g. handshake timeout).
            raise sechat.errors.ConnectionError("Failed to connect to socket")
        # Second attempt succeeds: give the loop a live socket again.
        self.socket = _FakeSocket([])


def test_failed_reconnect_does_not_kill_listener():
    """A ConnectionError during reconnect must be caught and retried, not
    allowed to escape and kill the listener thread."""
    room = _FakeRoomReconnect()
    t = threading.Thread(target=room.run, daemon=True)
    t.start()
    # The reconnect path sleeps 2s before calling connect(), so poll until the
    # loop has failed once and retried (connect called >= 2 times).
    deadline = time.time() + 6.0
    while time.time() < deadline and room._connect_calls < 2:
        time.sleep(0.05)
    assert room._connect_calls >= 2, "reconnect should have been retried"
    # The thread is still alive (listener survived the failed reconnect).
    assert t.is_alive(), "listener thread must survive a failed reconnect"
    # Stop cleanly.
    room.socket.close()
    room.running = False
    t.join(timeout=1.0)
    assert not t.is_alive()

