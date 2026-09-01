"""Remote-crew execution binding: a local session whose turns run on a peer.

Covers the three layers the feature is made of, in the order a turn traverses
them: the ``_ChatSlot`` binding and its round-trip through history metadata, the
in-band mirror that lets an SSE stream carry a WebSocket turn's full vocabulary,
and the relay that replays a peer's stream into the local slot.

The relay tests drive :func:`relay_remote_turn` with a hand-written byte stream
rather than a tunnel: the peer's wire format is the contract under test, and a
fake iterator pins it exactly while keeping the test free of aiohttp, SSH and a
second gateway.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

import kiro_crew
from kiro_crew.dashboard import remote_mirror
from kiro_crew.dashboard.remote_relay import (
    RemoteTurnError,
    create_peer_slot,
    ensure_version_parity,
    forward_peer_selection,
    forward_peer_stop,
    iter_sse_records,
    parse_sse_record,
    relay_remote_turn,
)
from kiro_crew.dashboard.state import _ChatSlot

#: Every async test here needs the loop; the repo runs pytest-asyncio in strict
#: mode, so the marker is explicit rather than inferred from the coroutine.
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_mirror():
    """No test may leak a mirror registration into the next one."""
    remote_mirror.reset_for_tests()
    yield
    remote_mirror.reset_for_tests()


def _remote_slot(key: str = "chat-1") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot.executor = "remote"
    slot.instance_id = "nobita"
    slot.remote_slot = "peer-chat-9"
    return slot


async def _stream(*records: bytes) -> AsyncIterator[bytes]:
    for record in records:
        yield record


def _sse(row: dict) -> bytes:
    return f"data: {json.dumps(row)}\n\n".encode()


# ── The binding ────────────────────────────────────────────────────────────────


class TestRemoteBinding:
    def test_a_fresh_slot_runs_locally(self):
        assert _ChatSlot("chat-1").executor == "local"
        assert _ChatSlot("chat-1").is_remote is False

    @pytest.mark.parametrize("missing", ["instance_id", "remote_slot"])
    def test_a_half_present_binding_is_not_remote(self, missing):
        """A marker without its target must NOT read as a remote slot.

        This is the fail-closed direction that matters: if ``is_remote`` said
        True the dispatch would try a peer it cannot name, and if the *marker*
        alone were ignored the turn would silently run on THIS machine — work the
        user asked a named crew to do. Neither is acceptable, so the property is
        False and ``api_chat`` refuses on the marker instead.
        """
        slot = _remote_slot()
        setattr(slot, missing, "")
        assert slot.is_remote is False
        assert slot.executor == "remote"  # the marker is preserved for the refusal

    def test_the_binding_is_projected_on_every_slot(self):
        local = _ChatSlot("chat-1").to_dict()
        assert local["executor"] == "local"
        assert local["instance_id"] == ""
        remote = _remote_slot().to_dict()
        assert remote["executor"] == "remote"
        assert remote["instance_id"] == "nobita"

    def test_the_peers_slot_key_is_never_projected(self):
        """It is a peer-side identifier with no browser consumer — keep it server-side."""
        assert "remote_slot" not in _remote_slot().to_dict()


# ── The in-band mirror ─────────────────────────────────────────────────────────


class TestRelayMirror:
    def test_frames_are_not_mirrored_until_a_reader_attaches(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        state.broadcast_ws("tool_call", {"slot": slot.key, "tool": "fs_read"})
        assert slot._pending == []

    def test_an_attached_reader_receives_mirrored_frames(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        owned = remote_mirror.attach(slot.key)
        try:
            state.broadcast_ws("tool_call", {"slot": slot.key, "tool": "fs_read"})
        finally:
            remote_mirror.detach(slot.key, owned)
        assert len(slot._pending) == 1
        row = slot._pending[0]
        assert row["cls"] == "relay:tool_call"
        assert json.loads(row["content"])["tool"] == "fs_read"

    @pytest.mark.parametrize("event", sorted(remote_mirror.MIRROR_SKIP_EVENTS))
    def test_already_in_band_frames_are_never_mirrored(self, tmp_path, event):
        """The denylist is what stops every chunk arriving twice.

        Each of these frame types has a transcript row or its own wire frame
        already on the SSE stream, so mirroring them would double the content.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        owned = remote_mirror.attach(slot.key)
        try:
            state.broadcast_ws(event, {"slot": slot.key, "content": "x"})
        finally:
            remote_mirror.detach(slot.key, owned)
        assert slot._pending == []

    def test_a_frame_for_another_slot_is_not_mirrored(self, tmp_path):
        state = _make_state(tmp_path)
        watched = state.get_or_create_slot("chat-1")
        other = state.get_or_create_slot("chat-2")
        owned = remote_mirror.attach(watched.key)
        try:
            state.broadcast_ws("tool_call", {"slot": other.key, "tool": "fs_read"})
        finally:
            remote_mirror.detach(watched.key, owned)
        assert watched._pending == []
        assert other._pending == []

    def test_a_second_reader_cannot_truncate_the_first(self, tmp_path):
        """Overlapping relay readers on one slot must not cut each other off."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        first = remote_mirror.attach(slot.key)
        second = remote_mirror.attach(slot.key)
        assert first is True and second is False
        remote_mirror.detach(slot.key, second)  # the non-owner leaves
        # Asserted through the mirror's only output — a frame still lands for the
        # reader that is still attached.
        state.broadcast_ws("tool_call", {"slot": slot.key, "tool": "fs_read"})
        assert len(slot._pending) == 1
        remote_mirror.detach(slot.key, first)
        state.broadcast_ws("tool_call", {"slot": slot.key, "tool": "fs_read"})
        assert len(slot._pending) == 1  # nothing added after the owner left


# ── SSE framing ────────────────────────────────────────────────────────────────


class TestSseFraming:
    def test_records_split_on_the_blank_line_not_the_newline(self):
        """A JSON payload may contain an escaped newline; splitting on \\n cuts it."""
        buffer = bytearray()
        payload = json.dumps({"type": "chunk", "content": "line one\nline two"})
        records = list(iter_sse_records(buffer, f"data: {payload}\n\n".encode()))
        assert len(records) == 1
        assert parse_sse_record(records[0])["content"] == "line one\nline two"

    def test_a_record_split_across_chunks_is_reassembled(self):
        buffer = bytearray()
        assert list(iter_sse_records(buffer, b'data: {"type": "chunk", "cont')) == []
        records = list(iter_sse_records(buffer, b'ent": "hi"}\n\n'))
        assert [parse_sse_record(r)["content"] for r in records] == ["hi"]

    def test_the_terminator_is_reported_as_a_sentinel(self):
        assert parse_sse_record(b"data: [DONE]") == {"__done__": True}

    def test_a_keepalive_comment_is_not_a_row(self):
        assert parse_sse_record(b": keepalive") is None

    def test_an_undecodable_record_is_dropped_not_raised(self):
        assert parse_sse_record(b"data: {not json") is None

    def test_an_unterminated_record_is_refused_rather_than_buffered_forever(self):
        buffer = bytearray()
        with pytest.raises(RemoteTurnError):
            list(iter_sse_records(buffer, b"data: " + b"x" * (8 * 1024 * 1024 + 16)))


# ── The version gate ───────────────────────────────────────────────────────────


class TestVersionParity:
    async def test_an_equal_version_passes(self):
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))
        await ensure_version_parity(mgr, "nobita")  # does not raise

    async def test_a_different_version_is_refused_naming_both_sides(self):
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, "0.5.9"))
        with pytest.raises(RemoteTurnError) as excinfo:
            await ensure_version_parity(mgr, "nobita")
        assert "0.5.9" in str(excinfo.value)
        assert kiro_crew.__version__ in str(excinfo.value)

    async def test_an_unknown_version_is_a_mismatch_not_a_pass(self):
        """A peer too old to report its version cannot be proven equal.

        The optimistic reading — attempt it and see — is what the gate exists to
        prevent: the two ends exchange an unversioned frame vocabulary, so a skew
        surfaces as a session that half works.
        """
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(False, "capability_peer_too_old"))
        with pytest.raises(RemoteTurnError) as excinfo:
            await ensure_version_parity(mgr, "nobita")
        assert "older Kiro Crew" in str(excinfo.value)

    async def test_an_unreachable_peer_is_refused_with_a_different_remedy(self):
        """ "Too old" and "unreachable" need different advice, so they differ.

        A stale credential or a dropped tunnel is fixed by reconnecting; telling
        that user to update their crew sends them to the wrong place.
        """
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(False, "capability_no_credential"))
        with pytest.raises(RemoteTurnError) as excinfo:
            await ensure_version_parity(mgr, "nobita")
        assert "Reconnect the crew" in str(excinfo.value)


# ── The relay ──────────────────────────────────────────────────────────────────


class TestRelayReplay:
    async def test_chunks_replay_as_local_chat_chunk_frames(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "He", "cls": "chunk"}),
                _sse({"type": "chunk", "content": "llo", "cls": "chunk"}),
                b"data: [DONE]\n\n",
            ),
        )
        chunk_calls = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_chunk"]
        assert [c.args[1]["content"] for c in chunk_calls] == ["He", "llo"]
        # Local sequence numbers, not the peer's: the frontend orders within the
        # LOCAL slot, and a second relayed turn would restart the peer's count.
        assert [c.args[1]["seq"] for c in chunk_calls] == [1, 2]
        assert all(c.args[1]["slot"] == slot.key for c in chunk_calls)

    async def test_a_mirrored_frame_is_rebroadcast_under_the_local_key(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "relay:tool_call",
                        "cls": "relay:tool_call",
                        "content": json.dumps(
                            {"slot": "peer-chat-9", "tool": "fs_read", "tool_call_id": "t1"}
                        ),
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )
        tool_calls = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "tool_call"]
        assert len(tool_calls) == 1
        # Rewriting the slot identifier is the whole translation — this is what
        # lets the unmodified frontend consumption path render a peer's turn.
        assert tool_calls[0].args[1]["slot"] == slot.key
        assert tool_calls[0].args[1]["tool"] == "fs_read"

    async def test_the_peers_user_row_is_not_replayed(self, tmp_path):
        """The local side already appended the user's message before dispatch."""
        state = _make_state(tmp_path)
        slot = _remote_slot()
        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "user", "content": "hi", "cls": "msg msg-u"}),
                b"data: [DONE]\n\n",
            ),
        )
        assert [m["role"] for m in slot.messages] == []

    async def test_the_finalized_assistant_row_replaces_its_chunks(self, tmp_path):
        """Keeping both would render the answer twice, once streamed once final."""
        state = _make_state(tmp_path)
        slot = _remote_slot()
        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "He", "cls": "chunk"}),
                _sse({"type": "chunk", "content": "llo", "cls": "chunk"}),
                _sse({"type": "assistant", "content": "Hello", "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )
        assert [(m["role"], m["content"]) for m in slot.messages] == [("assistant", "Hello")]
        # The window rewrite is only half the release: append put the SAME dict
        # in the pending queue, so a leak there would strand every token.
        assert [r for r in slot._pending if r.get("role") == "chunk"] == []

    async def test_a_turn_always_ends_with_chat_done(self, tmp_path):
        """Without it the composer stays blocked and the session looks hung."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        await relay_remote_turn(state, slot, "hi", chunks=_stream(b"data: [DONE]\n\n"))
        assert [c.args[0] for c in state.broadcast_ws.call_args_list][-1] == "chat_done"

    async def test_a_failing_stream_yields_an_error_row_and_still_finishes(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        async def _boom() -> AsyncIterator[bytes]:
            yield _sse({"type": "chunk", "content": "par", "cls": "chunk"})
            raise ConnectionResetError("tunnel died")

        await relay_remote_turn(state, slot, "hi", chunks=_boom())
        assert slot.messages[-1]["role"] == "error"
        assert [c.args[0] for c in state.broadcast_ws.call_args_list][-1] == "chat_done"

    async def test_a_stream_that_ends_without_the_terminator_is_not_a_success(self, tmp_path):
        """EOF is not completion, and the difference is invisible to the reader.

        A dropped tunnel, a peer that died mid-turn, or a proxy that closed the
        response early all end the iterator cleanly with no ``[DONE]``. Treating
        that as a finished turn broadcast ``chat_done`` over a transcript that
        stops mid-sentence, so a truncated answer read as a complete one with
        nothing anywhere saying otherwise.
        """
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(_sse({"type": "chunk", "content": "half an ans", "cls": "chunk"})),
        )

        assert slot.messages[-1]["role"] == "error"
        assert "incomplete" in slot.messages[-1]["content"]
        # Still unblocked: a truncation is reported, not left hanging.
        assert [c.args[0] for c in state.broadcast_ws.call_args_list][-1] == "chat_done"

    async def test_an_empty_stream_is_not_a_success(self, tmp_path):
        """The degenerate case of the same defect: nothing arrived at all."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(state, slot, "hi", chunks=_stream())

        assert slot.messages[-1]["role"] == "error"

    async def test_rows_after_the_terminator_are_not_replayed(self, tmp_path):
        """The terminator still ends the replay, exactly as the early return did.

        Tracking it with a flag instead of returning must not turn ``[DONE]`` into
        a mere marker: anything a peer sends after it belongs to no turn here.
        """
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "done", "cls": "chunk"}),
                b"data: [DONE]\n\n",
                _sse({"type": "chunk", "content": "after", "cls": "chunk"}),
            ),
        )

        chunk_calls = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_chunk"]
        assert [c.args[1]["content"] for c in chunk_calls] == ["done"]
        assert all(m["role"] != "error" for m in slot.messages)


# ── Metadata round-trip ────────────────────────────────────────────────────────


class TestBindingPersistence:
    def test_the_binding_survives_a_save_and_rehydrate(self, tmp_path):
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"
        slot.append("assistant", "hello", "msg msg-a")
        _save_slot_to_history(state, slot, force=True)

        # Drop the in-memory slot so the rehydrate reads from disk rather than
        # returning the live object it would otherwise find in the registry.
        del state._slots["chat-1"]
        restored = _rehydrate_slot_from_history(state, "chat-1")
        assert restored is not None
        assert restored.executor == "remote"
        assert restored.instance_id == "nobita"
        assert restored.remote_slot == "peer-chat-9"
        assert restored.is_remote is True

    def test_the_empty_window_merge_persists_a_complete_binding(self, tmp_path):
        """The window is empty for the whole gap before the first relayed row.

        A peer-bound newborn has no messages until the relay appends one, so the
        empty-window metadata merge is the only writer its binding sees. If that
        path skips the three fields, a restart inside that gap brings the session
        back as an ordinary local one and the next turn runs here instead of on
        the crew the user picked.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"
        # Materialize the metadata line first: the merge only ever updates an
        # existing record, so a slot that never had one has nothing to reconcile.
        slot.append("assistant", "hello", "msg msg-a")
        _save_slot_to_history(state, slot, force=True)

        slot.messages.clear()
        _save_slot_to_history(state, slot, force=True)

        meta = state.conversation_log.get_metadata("dashboard:chat-1")
        assert meta.get("executor") == "remote"
        assert meta.get("instance_id") == "nobita"
        assert meta.get("remote_slot") == "peer-chat-9"

    def test_the_empty_window_merge_writes_no_half_binding(self, tmp_path):
        """A marker with no target refuses every send, and says nothing useful.

        Coming back local is the recoverable reading (see the rehydrate test
        below), so the merge must not be the writer that creates the shape
        rehydrate then has to repair.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"  # no remote_slot: the peer open never landed
        slot.append("assistant", "hello", "msg msg-a")
        _save_slot_to_history(state, slot, force=True)

        slot.messages.clear()
        _save_slot_to_history(state, slot, force=True)

        meta = state.conversation_log.get_metadata("dashboard:chat-1")
        assert "executor" not in meta
        assert "instance_id" not in meta

    def test_an_incomplete_stored_binding_comes_back_local(self, tmp_path):
        """A truncated write or a hand-edit must not resurrect a dead session.

        A slot carrying the marker with no target refuses every send, and with no
        way for the user to tell why. Coming back as an ordinary local session is
        the recoverable reading.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.append("assistant", "hello", "msg msg-a")
        _save_slot_to_history(state, slot, force=True)
        path = state.conversation_log._path("dashboard:chat-1")
        lines = path.read_text().splitlines()
        meta = json.loads(lines[0])
        meta["executor"] = "remote"  # marker only, no instance_id / remote_slot
        lines[0] = json.dumps(meta)
        path.write_text("\n".join(lines) + "\n")

        del state._slots["chat-1"]
        restored = _rehydrate_slot_from_history(state, "chat-1")
        assert restored is not None
        assert restored.executor == "local"
        assert restored.is_remote is False


# ── Authorization before the peer is touched ───────────────────────────────────


def _create_app(state, *, app_name: str = "", user: str = "local-app"):
    """The real create handler behind a TestServer, optionally app-authenticated.

    Middleware sets ``request["app"]`` and ``request["user"]`` in production;
    setting them in a wrapper keeps the handler itself — and therefore the
    ordering under test — real.

    *user* defaults to ``local-app`` because the binding's first gate is a
    POSITIVE owner assertion (``is_owner_dashboard_request``): with no configured
    owner_id only the local dashboard subjects pass it, so a bare truthy user is
    an authenticated NON-owner and is refused. That is what
    ``test_a_non_owner_identity_cannot_bind_a_session_to_a_crew`` drives.
    """
    from aiohttp import web

    from kiro_crew.dashboard.chat import api_chat_slot_create

    async def handler(request: web.Request) -> web.Response:
        request["app"] = app_name
        request["user"] = user
        return await api_chat_slot_create(request)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", handler)
    return app


class TestBindingAuthorization:
    """A refused create must not have already written to the peer.

    ``create_peer_slot`` is a write on ANOTHER machine, spending the owner's
    tunnel credential. Both gates here are about ORDER, not about the verdict:
    the handler already refused these callers, but only after opening a session
    over there and — on the existing-slot path — after stamping the binding onto
    a slot it does not own. Asserting the verdict alone would pass with the side
    effects intact, so every test asserts the peer was never called.
    """

    @pytest.fixture
    def peer(self, monkeypatch):
        """A spy standing in for the peer write, so a call is observable."""
        spy = AsyncMock(return_value="peer-chat-9")
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.create_peer_slot", spy)
        return spy

    async def test_a_non_owner_identity_cannot_bind_a_session_to_a_crew(self, tmp_path, peer):
        """Authenticated is not the same as authorized on this route.

        A messaging identity admitted by an allow-list holds a dashboard
        credential whose subject is not the owner and whose ``app`` claim is
        EMPTY — so the app gate passes it. The peer write it would reach spends
        the OWNER's manager-held tunnel credential, which is why the gate is a
        positive owner assertion rather than the absence of an app claim.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        app = _create_app(state, user="slack:U123")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots", json={"instance_id": "nobita"})
            assert resp.status == 403
        peer.assert_not_awaited()

    async def test_an_unbound_create_is_untouched_by_the_owner_gate(self, tmp_path, peer):
        """The gate gates the BINDING, not the endpoint.

        A plain local create carries no ``instance_id``, reaches no peer and
        spends no tunnel credential, so a non-owner dashboard caller keeps the
        session-creation it has always had. Scoping the gate to the peer path is
        the whole reason it sits inside ``if instance_id:``.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state, user="slack:U123"))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "chat-local"})
            assert resp.status == 200
        peer.assert_not_awaited()

    async def test_an_app_token_cannot_bind_a_session_to_a_crew(self, tmp_path, peer):
        """Binding is a human act from the composer's crew picker.

        An app token has no surface for it, so the request is refused outright
        rather than allowed to spend the user's peer credential unattended.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state, app_name="notes"))) as client:
            resp = await client.post("/api/chat/slots", json={"instance_id": "nobita"})
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
        peer.assert_not_awaited()

    async def test_an_app_token_refusal_names_no_slot(self, tmp_path, peer):
        """One code for both refusal reasons, so it cannot be an existence oracle."""
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-1")
        async with TestClient(TestServer(_create_app(state, app_name="notes"))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 404
            body = await resp.json()
            assert body["code"] == "slot_not_found"
            assert "chat-1" not in json.dumps(body)
        peer.assert_not_awaited()

    async def test_an_already_bound_slot_is_not_rebound(self, tmp_path, peer):
        """`name` can address an EXISTING slot, and rebinding it is destructive.

        The old binding's peer session keeps running with no local reader, and
        the caller silently takes over a session someone else placed on a crew.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "shizuka"
        slot.remote_slot = "peer-chat-1"
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "remote_already_bound"
        peer.assert_not_awaited()
        # The original binding is intact — the refusal changed nothing.
        assert slot.instance_id == "shizuka"
        assert slot.remote_slot == "peer-chat-1"

    async def test_an_existing_local_slot_is_not_converted_to_remote(self, tmp_path, peer):
        """The destructive half of the same hole: local -> remote is not a create.

        The transcript stays here while EXECUTION moves to an empty peer slot, so
        the next turn runs with none of the conversation on screen. A binding is
        only ever stamped at birth, so the whole existing-slot path is refused.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.append("user", "the context that would stop being in play", "msg msg-u")
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "remote_already_bound"
        peer.assert_not_awaited()
        # Still an ordinary local session, and still the one the user was reading.
        assert slot.executor == "local"
        assert slot.instance_id == ""
        assert slot.is_remote is False
        assert len(slot.messages) == 1

    async def test_a_dashboard_caller_binds_a_new_slot(self, tmp_path, peer):
        """The allowed path, so the gates above are not just refusing everything."""
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["executor"] == "remote"
        assert body["instance_id"] == "nobita"
        # The peer's slot key is bound server-side but never projected.
        assert state._slots["chat-1"].remote_slot == "peer-chat-9"
        peer.assert_awaited_once()

    async def test_a_peer_that_refuses_leaves_no_local_slot_bound(self, tmp_path, monkeypatch):
        """Peer first, local slot second: a failure over there creates nothing here."""
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.create_peer_slot",
            AsyncMock(side_effect=RemoteTurnError("crew is on an older version")),
        )
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 502
            body = await resp.json()
        assert body["code"] == "remote_bind_failed"
        assert "older version" in body["error"]
        assert "chat-1" not in state._slots

    async def test_an_app_token_may_still_create_an_ordinary_local_slot(self, tmp_path, peer):
        """The gate is scoped to `instance_id`, not a blanket app refusal."""
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state, app_name="notes"))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "chat-1"})
            assert resp.status == 200
            assert (await resp.json())["executor"] == "local"
        peer.assert_not_awaited()

    @pytest.mark.parametrize(
        "body, code",
        [
            ({"mode": "bogus"}, "invalid_mode"),
            ({"memory_mode": "bogus"}, None),
            ({"name": "...", "mode": "crew"}, "crew_unsupported_slot"),
        ],
        ids=["mode", "memory_mode", "crew_unsupported_slot"],
    )
    async def test_a_request_refused_for_its_body_never_reaches_the_peer(
        self, tmp_path, peer, body, code
    ):
        """A 400 must not cost the user a session on the crew.

        These three validations read only the request body, so nothing forced
        them to run after the peer write — and running them after it meant a
        malformed request opened a session over there, returned 400, and left it
        orphaned with no local slot pointing at it to ever release it. Every
        refusal an operator can trigger by hand is therefore reachable without
        touching the peer at all.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={**body, "instance_id": "nobita"})
            assert resp.status == 400
            if code:
                assert (await resp.json())["code"] == code
        peer.assert_not_awaited()

    async def test_an_invalid_folder_project_never_reaches_the_peer(
        self, tmp_path, peer, monkeypatch
    ):
        """Same ordering rule for the one refusal that needs a folder read.

        Resolving a folder's project directory is I/O, not a body check, so it
        was the refusal most easily left on the far side of the peer write.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        state._folders.append({"id": "f1", "name": "Folder"})
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._resolve_folder_project_dir",
            lambda folders, folder_id: ("", "no such directory"),
        )
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"instance_id": "nobita", "folder_id": "f1"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "folder_project_invalid"
        peer.assert_not_awaited()


class TestBoundCreateDefaults:
    """What a bound session records when the user picked nothing.

    A local create stamps THIS machine's default agent so the sidebar names what
    will actually answer. For a peer-bound create that reasoning inverts: the
    default names a crew from this machine's roster, the peer applies its own, and
    stamping ours would make the shelf advertise one crew while another answered.
    """

    @pytest.fixture
    def peer(self, monkeypatch):
        spy = AsyncMock(return_value="peer-chat-9")
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.create_peer_slot", spy)
        return spy

    @pytest.fixture
    def local_default(self, monkeypatch):
        """A config whose default agent exists only on THIS machine."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load",
            staticmethod(
                lambda: SimpleNamespace(
                    default_agent="local-only-crew",
                    dashboard=SimpleNamespace(default_project=""),
                )
            ),
        )

    async def test_a_bound_create_records_no_agent(self, tmp_path, peer, local_default):
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            body = await (
                await client.post(
                    "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
                )
            ).json()

        assert body["agent"] == ""
        # And nothing was asked of the peer either: an omitted agent is how the
        # peer is told to apply its own default.
        assert peer.await_args.kwargs == {"agent": "", "model": ""}

    async def test_a_local_create_still_stamps_the_default(self, tmp_path, peer, local_default):
        """The counterpart, so the skip above is scoped to the binding."""
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            body = await (await client.post("/api/chat/slots", json={"name": "chat-1"})).json()

        assert body["agent"] == "local-only-crew"

    async def test_an_explicit_pick_is_recorded_and_forwarded(self, tmp_path, peer, local_default):
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            body = await (
                await client.post(
                    "/api/chat/slots",
                    json={"name": "chat-1", "instance_id": "nobita", "agent": "peer-crew"},
                )
            ).json()

        assert body["agent"] == "peer-crew"
        assert peer.await_args.kwargs["agent"] == "peer-crew"


class TestRemotePickApplication:
    """Mirror AFTER the peer took the pick, never before.

    The local field is what the header renders and what the next turn reports, so
    writing it first would leave the user looking at a pick the peer refused.
    """

    @pytest.fixture
    def forward(self, monkeypatch):
        """Returns ``{}``: the peer's accepted state, empty when it reports none."""
        spy = AsyncMock(return_value={})
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.forward_peer_selection", spy)
        return spy

    async def test_an_agent_pick_mirrors_the_workspace_the_peer_committed(
        self, tmp_path, monkeypatch
    ):
        """The peer resolves the agent against ITS bindings and answers with the
        workspace that resolution chose — the same derivation the local switch
        does. Ignoring it left this slot naming a workspace the machine running
        the turns had already moved off, so the header and the persisted record
        disagreed with the only side that executes anything.
        """
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(return_value={"ok": True, "agent": "reviewer", "workspace": "peer-ws"}),
        )
        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.workspace = "stale-local"

        resp = await _apply_remote_pick(state, slot, "agent", {"agent": "reviewer"})

        assert resp.status == 200
        assert slot.agent == "reviewer"
        assert slot.workspace == "peer-ws"

    async def test_a_peer_that_names_no_workspace_changes_nothing(self, tmp_path, forward):
        """Only a non-empty string is taken, so an older or terser peer is safe:
        a missing field leaves the local value alone rather than blanking it."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.workspace = "kept"

        await _apply_remote_pick(state, slot, "agent", {"agent": "reviewer"})

        assert slot.workspace == "kept"

    async def test_only_an_agent_pick_moves_the_workspace(self, tmp_path, monkeypatch):
        """A model or effort pick does not re-resolve bindings, so a `workspace`
        key in its reply is not a commitment this slot should adopt."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(return_value={"ok": True, "workspace": "not-a-commitment"}),
        )
        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.workspace = "kept"

        await _apply_remote_pick(state, slot, "model", {"model": "opus"})

        assert slot.workspace == "kept"

    async def test_the_mirrored_workspace_is_persisted_with_the_agent(self, tmp_path, monkeypatch):
        """One write, or a restart restores the pair inconsistent — an agent from
        the peer next to a workspace the peer never agreed to."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(return_value={"ok": True, "workspace": "peer-ws"}),
        )
        state = _make_state(tmp_path)
        state.conversation_log = MagicMock()
        slot = _remote_slot()

        await _apply_remote_pick(state, slot, "agent", {"agent": "reviewer"})

        written = state.conversation_log.update_metadata.call_args.args[1]
        assert written == {"agent": "reviewer", "workspace": "peer-ws"}

    async def test_an_accepted_pick_is_mirrored_on_the_slot(self, tmp_path, forward):
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        slot = _remote_slot()

        resp = await _apply_remote_pick(state, slot, "agent", {"agent": "reviewer"})

        assert resp.status == 200
        assert json.loads(resp.body.decode()) == {
            "ok": True,
            "agent": "reviewer",
            "remote": True,
        }
        assert slot.agent == "reviewer"

    async def test_a_refused_pick_leaves_the_slot_untouched(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(side_effect=RemoteTurnError("that crew has no such agent")),
        )
        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.agent = "coder"

        resp = await _apply_remote_pick(state, slot, "agent", {"agent": "reviewer"})

        assert resp.status == 502
        body = json.loads(resp.body.decode())
        assert body["code"] == "remote_pick_failed"
        assert "no such agent" in body["error"]
        assert slot.agent == "coder"

    async def test_a_model_pick_bumps_the_pick_generation(self, tmp_path, forward):
        """Same reason the local path bumps it: an explicit pick has to outrank
        the model-fallback restore probe, which would otherwise treat the pick as
        automatic backfill and override it."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        slot = _remote_slot()
        before = slot._model_pick_gen

        await _apply_remote_pick(state, slot, "model", {"model": "opus"})

        assert slot.model == "opus"
        assert slot._model_pick_gen == before + 1

    @pytest.mark.parametrize(
        "control,value",
        [
            ("agent", "reviewer"),
            ("model", "opus"),
            ("workspace", "peer-ws"),
            ("reasoning_effort", "high"),
        ],
    )
    async def test_an_accepted_pick_is_persisted_immediately(
        self, tmp_path, forward, control, value
    ):
        """The peer committed the value when it answered; the two ends must agree.

        Leaving this to the periodic flush opens a window in which a restart
        restores a local field the crew no longer agrees with — and the crew is the
        side that runs the next turn. A purely local pick can only ever disagree
        with itself, which is why the local model/effort/workspace routes can leave
        it to the flush and this one cannot.
        """
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"

        await _apply_remote_pick(state, slot, control, {control: value})

        assert state.conversation_log.get_metadata("dashboard:chat-1").get(control) == value

    async def test_a_refused_pick_persists_nothing(self, tmp_path, monkeypatch):
        """The write is downstream of the peer's acceptance, like the mirror."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(side_effect=RemoteTurnError("that crew has no such agent")),
        )
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"

        await _apply_remote_pick(state, slot, "agent", {"agent": "reviewer"})

        assert "agent" not in state.conversation_log.get_metadata("dashboard:chat-1")

    async def test_a_workspace_pick_does_not_rewrite_the_local_project(self, tmp_path, forward):
        """``project`` is a path on THIS machine (file search, @-mentions).

        Deriving it from the peer's workspace name would write a local directory
        that has nothing to do with the workspace the turn actually runs in.
        """
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.project = "/local/project"

        await _apply_remote_pick(state, slot, "workspace", {"workspace": "peer-ws"})

        assert slot.workspace == "peer-ws"
        assert slot.project == "/local/project"


# ── Talking to the peer ────────────────────────────────────────────────────────


class _FakeUpstream:
    """The async-context-manager reply shape ``proxy_request`` yields."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.content = SimpleNamespace(read=AsyncMock(return_value=body))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _mgr_returning(status: int, body: bytes):
    """An instances manager whose proxy answers once with *status* / *body*."""
    mgr = MagicMock()
    mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))
    mgr.proxy_request = MagicMock(return_value=_FakeUpstream(status, body))
    return mgr


class TestCreatePeerSlot:
    async def test_the_peers_slot_key_is_returned(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, b'{"key": "peer-chat-9"}')

        assert await create_peer_slot(state, "nobita") == "peer-chat-9"

    async def test_no_agent_is_sent_to_the_peer(self, tmp_path):
        """The peer has its own roster, its own default and its own projects.

        Naming this machine's default would either fail there or silently bind a
        different crew than the name implies; letting the peer choose is the
        whole point of the session running on it.
        """
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b'{"key": "peer-chat-9"}')
        state.instances_manager = mgr

        await create_peer_slot(state, "nobita")

        _, kwargs = mgr.proxy_request.call_args
        assert json.loads(kwargs["data"]) == {}

    async def test_an_explicit_pick_rides_the_create(self, tmp_path):
        """A pick made in the crew picker comes from the PEER's roster.

        It travels with the create rather than in a follow-up call: a second
        round-trip can fail after the peer session already exists, which would
        leave a bound session running a crew the user did not choose.
        """
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b'{"key": "peer-chat-9"}')
        state.instances_manager = mgr

        await create_peer_slot(state, "nobita", agent="reviewer", model="opus")

        _, kwargs = mgr.proxy_request.call_args
        assert json.loads(kwargs["data"]) == {"agent": "reviewer", "model": "opus"}

    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            ({"agent": "reviewer"}, {"agent": "reviewer"}),
            ({"model": "opus"}, {"model": "opus"}),
            ({"agent": "", "model": ""}, {}),
        ],
    )
    async def test_only_the_named_picks_are_sent(self, tmp_path, kwargs, expected):
        """An empty pick is OMITTED, not sent as "".

        Sending ``{"agent": ""}`` is a different request from sending nothing: the
        peer would read it as an explicit blank rather than "apply your default".
        """
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b'{"key": "peer-chat-9"}')
        state.instances_manager = mgr

        await create_peer_slot(state, "nobita", **kwargs)

        assert json.loads(mgr.proxy_request.call_args.kwargs["data"]) == expected

    async def test_a_gateway_without_instances_refuses(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = None

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "not available" in str(excinfo.value)

    async def test_a_version_skewed_peer_is_never_asked_to_open_a_slot(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b'{"key": "peer-chat-9"}')
        mgr.peer_version = AsyncMock(return_value=(True, "0.0.1"))
        state.instances_manager = mgr

        with pytest.raises(RemoteTurnError):
            await create_peer_slot(state, "nobita")
        mgr.proxy_request.assert_not_called()

    async def test_a_refusing_peer_reports_its_status(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(503, b"{}")

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "503" in str(excinfo.value)

    async def test_an_unreachable_peer_says_so_without_leaking_the_cause(self, tmp_path):
        """The transcript is a surface the user copies out of.

        Only the exception TYPE reaches the log; the message names no port, no
        credential and no library internals.
        """
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))
        mgr.proxy_request = MagicMock(side_effect=ConnectionResetError("port 17777 refused"))
        state.instances_manager = mgr

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "17777" not in str(excinfo.value)
        assert "Reconnect" in str(excinfo.value)

    async def test_an_oversized_reply_is_refused_rather_than_decoded(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, b"x" * (64 * 1024 + 8))

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "oversized" in str(excinfo.value)

    async def test_a_malformed_reply_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, b"{not json")

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "malformed" in str(excinfo.value)

    @pytest.mark.parametrize("body", [b"{}", b'{"key": ""}', b'{"key": 7}', b"[]"])
    async def test_a_reply_that_names_no_slot_is_refused(self, tmp_path, body):
        """A missing key would otherwise bind the session to the empty string."""
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, body)

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "did not name it" in str(excinfo.value)


class TestForwardPeerSelection:
    """A header pick has to land on the machine that answers the turn.

    The pickers for a bound session are populated from the PEER's rosters, so a
    pick applied only locally would name an agent, model or workspace the peer
    never hears about — the header would show one thing and the reply would come
    from another, with nothing reporting a problem.
    """

    @pytest.mark.parametrize(
        "control,body,segment",
        [
            ("agent", {"agent": "reviewer"}, "agent"),
            ("model", {"model": "opus"}, "model"),
            ("workspace", {"workspace": "main"}, "workspace"),
            ("reasoning_effort", {"reasoning_effort": "high"}, "reasoning-effort"),
        ],
    )
    async def test_each_control_reaches_the_peers_own_slot(self, tmp_path, control, body, segment):
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b'{"ok": true}')
        state.instances_manager = mgr

        await forward_peer_selection(state, _remote_slot(), control, body)

        args, kwargs = mgr.proxy_request.call_args
        # The PEER's slot key, and the peer's route spelling — the local key
        # addresses nothing over there, and `reasoning_effort` is hyphenated in
        # the URL while the body key is not.
        assert args[2] == f"api/chat/slots/peer-chat-9/{segment}"
        assert json.loads(kwargs["data"]) == body

    async def test_the_peers_accepted_state_is_returned_not_discarded(self, tmp_path):
        """What the peer ACCEPTED is not always what was asked for.

        Its ``/agent`` route resolves the agent against its own bindings and
        answers with the workspace that resolution chose, so the caller needs the
        body to mirror it. Dropping it was how the two ends came to disagree.
        """
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(
            200, b'{"ok": true, "agent": "reviewer", "workspace": "peer-ws"}'
        )

        accepted = await forward_peer_selection(
            state, _remote_slot(), "agent", {"agent": "reviewer"}
        )

        assert accepted["workspace"] == "peer-ws"

    @pytest.mark.parametrize(
        "body",
        [b"not json at all", b'"a bare string"', b"", b"[]"],
        ids=["unparseable", "not-an-object", "empty", "array"],
    )
    async def test_a_reply_that_is_not_an_object_yields_nothing_to_mirror(self, tmp_path, body):
        """The pick SUCCEEDED over there — a reply this side cannot read must not
        turn it into a failure. An empty dict degrades to "nothing to mirror"."""
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, body)

        assert await forward_peer_selection(state, _remote_slot(), "model", {"model": "opus"}) == {}

    async def test_an_oversized_success_reply_is_not_decoded(self, tmp_path):
        """Same byte ceiling the refusal path enforces, and for the same reason:
        a hostile or broken peer must not be able to make the hub decode an
        unbounded body just because it answered 200."""
        from kiro_crew.dashboard.remote_relay import _MAX_PEER_SLOT_REPLY_BYTES

        state = _make_state(tmp_path)
        padding = b" " * (_MAX_PEER_SLOT_REPLY_BYTES + 1)
        state.instances_manager = _mgr_returning(200, b'{"workspace": "peer-ws"}' + padding)

        assert await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "r"}) == {}

    async def test_an_unlisted_control_is_a_programming_error(self, tmp_path):
        """A closed map, because ``control`` is interpolated into a proxied URL.

        Every caller passes a literal, so reaching this is a bug rather than a
        peer problem — and a soft failure here would read to the user as an
        offline crew.
        """
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b"{}")
        state.instances_manager = mgr

        with pytest.raises(ValueError):
            await forward_peer_selection(state, _remote_slot(), "project", {"project": "/etc"})
        mgr.proxy_request.assert_not_called()

    async def test_a_version_skewed_peer_is_never_asked_to_apply_a_pick(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b"{}")
        mgr.peer_version = AsyncMock(return_value=(True, "0.0.1"))
        state.instances_manager = mgr

        with pytest.raises(RemoteTurnError):
            await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "x"})
        mgr.proxy_request.assert_not_called()

    async def test_the_peers_own_refusal_is_what_the_user_reads(self, tmp_path):
        """Only the peer knows WHY: the agent was deleted there, the model is not
        available to that account, the workspace already has messages."""
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(409, b'{"error": "agent is pinned there"}')

        with pytest.raises(RemoteTurnError) as excinfo:
            await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "x"})
        assert str(excinfo.value) == "agent is pinned there"

    @pytest.mark.parametrize(
        "body", [b"{}", b"not json", b'{"error": 7}', b'["error"]', b"x" * (64 * 1024 + 8)]
    )
    async def test_an_unusable_refusal_falls_back_to_the_status(self, tmp_path, body):
        """A peer reply is untrusted: only a short ``error`` STRING is surfaced."""
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(500, body)

        with pytest.raises(RemoteTurnError) as excinfo:
            await forward_peer_selection(state, _remote_slot(), "model", {"model": "x"})
        assert "500" in str(excinfo.value)

    async def test_an_overlong_peer_message_is_truncated(self, tmp_path):
        state = _make_state(tmp_path)
        detail = json.dumps({"error": "y" * 900}).encode()
        state.instances_manager = _mgr_returning(400, detail)

        with pytest.raises(RemoteTurnError) as excinfo:
            await forward_peer_selection(state, _remote_slot(), "model", {"model": "x"})
        assert len(str(excinfo.value)) == 200

    async def test_an_unreachable_peer_says_so_without_leaking_the_cause(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))
        mgr.proxy_request = MagicMock(side_effect=ConnectionResetError("port 17777 refused"))
        state.instances_manager = mgr

        with pytest.raises(RemoteTurnError) as excinfo:
            await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "x"})
        assert "17777" not in str(excinfo.value)
        assert "Reconnect" in str(excinfo.value)

    async def test_a_gateway_without_instances_refuses(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = None

        with pytest.raises(RemoteTurnError):
            await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "x"})


class TestForwardPeerStop:
    async def test_a_local_slot_is_not_forwarded(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, b"{}")

        assert await forward_peer_stop(state, _ChatSlot("chat-1"), False) is False

    async def test_an_accepted_stop_reports_true(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b"{}")
        state.instances_manager = mgr

        assert await forward_peer_stop(state, _remote_slot(), False) is True
        args, kwargs = mgr.proxy_request.call_args
        # The PEER's slot key, not the local one: the local key means nothing
        # over there and would stop a different session or none at all.
        assert args[2] == "api/chat/slots/peer-chat-9/stop"
        assert kwargs["params"] is None

    async def test_a_forced_stop_carries_the_flag(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b"{}")
        state.instances_manager = mgr

        await forward_peer_stop(state, _remote_slot(), True)

        assert mgr.proxy_request.call_args.kwargs["params"] == {"force": "true"}

    async def test_a_refused_stop_reports_false_rather_than_raising(self, tmp_path):
        """The caller turns False into an actionable error for the user.

        Raising here would surface as a generic 500 on a stop press, which reads
        as "the button is broken" rather than "the crew is unreachable".
        """
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(502, b"{}")

        assert await forward_peer_stop(state, _remote_slot(), False) is False

    async def test_an_unreachable_peer_reports_false(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.proxy_request = MagicMock(side_effect=ConnectionResetError("tunnel died"))
        state.instances_manager = mgr

        assert await forward_peer_stop(state, _remote_slot(), False) is False


class TestPeerTurnRequest:
    async def test_the_turn_asks_the_peer_to_mirror_its_frames(self, tmp_path):
        """Without ``relay=1`` the reply carries the prose and no tool activity."""
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))

        class _Streaming(_FakeUpstream):
            def __init__(self):
                super().__init__(200, b"")
                self.content = SimpleNamespace(iter_any=self._iter)

            async def _iter(self):
                yield b"data: [DONE]\n\n"

        mgr.proxy_request = MagicMock(return_value=_Streaming())
        state.instances_manager = mgr
        state.broadcast_ws = MagicMock()

        await relay_remote_turn(state, _remote_slot(), "hi")

        args, kwargs = mgr.proxy_request.call_args
        assert args[2] == "api/chat"
        assert kwargs["params"] == {"relay": "1"}
        # The PEER's slot key, and only the message — see the known gap in the PR.
        assert json.loads(kwargs["data"]) == {"message": "hi", "slot": "peer-chat-9"}

    async def test_a_peer_that_refuses_the_turn_becomes_an_error_row(self, tmp_path):
        """The refusal must reach the transcript, not just the log.

        A silent failure leaves the user looking at a session that accepted
        their message and then said nothing.
        """
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))
        mgr.proxy_request = MagicMock(return_value=_FakeUpstream(503, b"{}"))
        state.instances_manager = mgr
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(state, slot, "hi")

        assert slot.messages[-1]["role"] == "error"
        assert "503" in slot.messages[-1]["content"]
        assert [c.args[0] for c in state.broadcast_ws.call_args_list][-1] == "chat_done"

    async def test_a_version_skew_found_at_send_time_is_a_transcript_error(self, tmp_path):
        """The gate is re-checked per turn: a peer can be updated mid-session."""
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, "0.0.1"))
        mgr.proxy_request = MagicMock()
        state.instances_manager = mgr
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(state, slot, "hi")

        assert slot.messages[-1]["role"] == "error"
        assert "0.0.1" in slot.messages[-1]["content"]
        mgr.proxy_request.assert_not_called()


class TestRowReplayDetails:
    async def test_a_thinking_row_is_replayed_as_reasoning(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "thinking", "content": "hmm", "cls": "thinking"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert [(m["role"], m["content"]) for m in slot.messages] == [("thinking", "hmm")]
        assert [c.args[0] for c in state.broadcast_ws.call_args_list].count("chat_thinking") == 1

    async def test_a_rows_meta_survives_the_replay(self, tmp_path):
        """Tool rows carry their identity in `meta`; dropping it breaks folding."""
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "tool",
                        "content": "fs_read",
                        "cls": "tool",
                        "meta": {"tool_call_id": "t1"},
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        # A subset check, not equality: ``append`` mints a local ``mid`` into the
        # same dict, and the peer's id is what the frontend folds tool rows by.
        assert slot.messages[-1]["meta"]["tool_call_id"] == "t1"

    async def test_a_non_string_content_is_carried_as_json(self, tmp_path):
        """A structured row must not vanish because it is not a string."""
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "assistant", "content": {"a": 1}, "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert json.loads(slot.messages[-1]["content"]) == {"a": 1}

    async def test_a_row_with_no_type_is_ignored(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(_sse({"content": "orphan"}), b"data: [DONE]\n\n"),
        )

        assert slot.messages == []

    async def test_a_mirrored_frame_keyed_by_key_is_also_rewritten(self, tmp_path):
        """`slot_title` and `session_summary` name the session in `key`."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "relay:slot_title",
                        "content": json.dumps({"key": "peer-chat-9", "title": "Deploy"}),
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        titles = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "slot_title"]
        assert titles[0].args[1] == {"key": slot.key, "title": "Deploy"}

    @pytest.mark.parametrize("payload", ["{not json", '"a string"', "[1, 2]"])
    async def test_an_undecodable_mirrored_frame_is_dropped_not_raised(self, tmp_path, payload):
        """One bad frame must not end an otherwise healthy turn."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "relay:tool_call", "content": payload}),
                b"data: [DONE]\n\n",
            ),
        )

        assert [c.args[0] for c in state.broadcast_ws.call_args_list] == ["chat_done"]
        assert slot.messages == []

    async def test_the_done_sentinel_stops_the_replay(self, tmp_path):
        """Rows after the terminator belong to no turn and must not be appended."""
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                b"data: [DONE]\n\n",
                _sse({"type": "assistant", "content": "late", "cls": "msg msg-a"}),
            ),
        )

        assert slot.messages == []

    async def test_a_record_with_no_data_line_is_not_a_row(self):
        assert parse_sse_record(b"event: ping\nid: 7") is None

    async def test_a_garbled_record_does_not_end_the_turn(self, tmp_path):
        """The rest of the stream is still worth replaying."""
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                b"data: {not json\n\n",
                b": keepalive\n\n",
                _sse({"type": "assistant", "content": "Hello", "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert [(m["role"], m["content"]) for m in slot.messages] == [("assistant", "Hello")]

    async def test_only_the_trailing_run_of_chunks_is_replaced(self, tmp_path):
        """An earlier segment's finished message must survive the next finalize.

        The scan walks back from the end and stops at the first non-chunk row, so
        a two-segment turn keeps both answers instead of collapsing to the last.
        """
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "One", "cls": "chunk"}),
                _sse({"type": "assistant", "content": "One", "cls": "msg msg-a"}),
                _sse({"type": "chunk", "content": "Two", "cls": "chunk"}),
                _sse({"type": "assistant", "content": "Two", "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert [(m["role"], m["content"]) for m in slot.messages] == [
            ("assistant", "One"),
            ("assistant", "Two"),
        ]


# A peer row carrying both shapes the local pass catches: a credential and a
# URL that would carry it off-box. The peer redacts its own copy with the same
# pass, so in the healthy case the local one is a no-op — these tests are about
# what happens when it is NOT, which is the only case that matters for a
# guarantee that has to hold on THIS side of the wire.
_SECRET = "AKIAIOSFODNN7EXAMPLE"
_TAINTED = f"board updated {_SECRET} see http://evil.example.com/x?d={_SECRET}"


def _assert_redacted(text: str) -> None:
    assert _SECRET not in text
    assert "[REDACTED: credential]" in text
    # The exfil marker names the host by design, so assert the marker rather
    # than the absence of the domain substring.
    assert "[REDACTED: suspicious URL to evil.example.com]" in text


class TestRelayedRedaction:
    """A relayed row reaches the same local surfaces as a locally-run one.

    The transcript, the WebSocket and the ConversationLog are shared by both
    paths, so the redaction pair a local turn applies before ``append`` has to
    be applied to peer-supplied text too — a peer that skips it (older build,
    different config, or simply compromised) must not be able to write raw
    credentials into this machine's history.
    """

    async def test_an_assistant_row_is_redacted_before_it_is_stored(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "assistant", "content": _TAINTED, "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )

        _assert_redacted(slot.messages[-1]["content"])

    async def test_a_streamed_chunk_is_redacted_on_the_wire(self, tmp_path):
        """The chunk frame is its own broadcast, not a by-product of ``append``."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": _TAINTED, "cls": "chunk"}),
                b"data: [DONE]\n\n",
            ),
        )

        chunks = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_chunk"]
        _assert_redacted(chunks[0].args[1]["content"])
        _assert_redacted(slot.messages[-1]["content"])

    async def test_a_thinking_row_is_redacted_on_both_surfaces(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "thinking", "content": _TAINTED, "cls": "thinking"}),
                b"data: [DONE]\n\n",
            ),
        )

        thinking = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_thinking"]
        _assert_redacted(thinking[0].args[1]["content"])
        _assert_redacted(slot.messages[-1]["content"])

    async def test_a_rows_meta_is_redacted_too(self, tmp_path):
        """`meta` holds the tool input — the field most likely to carry a secret."""
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "tool",
                        "content": "fs_read",
                        "cls": "tool",
                        "meta": {"tool_call_id": "t1", "tool_input": _TAINTED},
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        meta = slot.messages[-1]["meta"]
        _assert_redacted(meta["tool_input"])
        # The recursive pass leaves non-matching strings byte-identical, so the
        # row's identity still folds.
        assert meta["tool_call_id"] == "t1"

    async def test_a_mirrored_frames_nested_strings_are_redacted(self, tmp_path):
        """The mirror is a denylist, so the frame shape is open-ended."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "relay:tool_result",
                        "content": json.dumps(
                            {
                                "slot": "peer-chat-9",
                                "output": _TAINTED,
                                "nested": {"lines": [_TAINTED]},
                            }
                        ),
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        results = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "tool_result"]
        payload = results[0].args[1]
        _assert_redacted(payload["output"])
        _assert_redacted(payload["nested"]["lines"][0])
        # Redaction runs BEFORE the key rewrite, so the local key still lands.
        assert payload["slot"] == slot.key

    async def test_clean_prose_is_passed_through_unchanged(self, tmp_path):
        """Both redactors return their input when nothing matches."""
        state = _make_state(tmp_path)
        slot = _remote_slot()
        prose = "Ratio 12:34 on port 8080 — see https://github.com/acme/widgets/pull/12"

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "assistant", "content": prose, "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert slot.messages[-1]["content"] == prose
