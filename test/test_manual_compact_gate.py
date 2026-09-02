"""Manual ``/compact`` is gated on backend capability (#7800).

The defect these pin: KAS never answers the ``/compact`` prompt with a
compaction status — its ``summarization_*`` frames (mapped to compaction status
by ``acp.kas_wire``) fire only for KAS-initiated auto-summarization. The
dashboard forwarded the command anyway and then awaited
``wait_for_compaction()``, stranding the user for the full 300s
``COMPACT_WAIT_TIMEOUT_SECS`` before failing.

The fix is the capability-set pattern: ``ACP_BACKENDS_COMPACT`` names the
backends whose manual ``/compact`` works (kiro-cli emits compaction/status,
claude-agent-acp compacts natively in-prompt), the capability is declared on
the ``LLMProvider`` ABC with a safe default (harness-parity H14) and answered
by the ACP implementations from set membership (H6), and the dashboard refuses
the command up front — before dispatching the prompt — when a provider
positively names an unsupported backend.

Diagnosis credit: awsdataarchitect (issue #7800).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider


class TestCompactCapabilitySet:
    def test_membership_is_kiro_and_claude_only(self) -> None:
        """Opting a harness in is a deliberate edit with evidence (H6).

        KAS stays out until it acts on the /compact prompt; granting it here
        would re-introduce the 300s strand this set exists to prevent.
        """
        assert ACP_BACKENDS_COMPACT == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE})
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_COMPACT

    def test_subset_of_known_backends(self) -> None:
        """H8: a capability cannot be granted to an identifier nothing recognizes."""
        assert ACP_BACKENDS_COMPACT <= ACP_BACKENDS_KNOWN

    def test_capability_is_answered_by_membership_not_negation(self) -> None:
        """H6: both ACP implementations must read the set, so a harness added to
        ``ACP_BACKENDS_KNOWN`` and nowhere else is refused by default instead of
        inheriting the capability."""
        for impl in (
            AcpProvider.manual_compact_unsupported_backend.fget,
            AcpSessionProvider.manual_compact_unsupported_backend.fget,
        ):
            source = inspect.getsource(impl)
            assert "ACP_BACKENDS_COMPACT" in source


class TestManualCompactUnsupportedBackend:
    """Unit surface of the capability property across the provider shapes."""

    def test_kas_acp_provider_names_itself(self) -> None:
        provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
        assert provider.manual_compact_unsupported_backend == ACP_BACKEND_KAS

    def test_kiro_acp_provider_passes(self) -> None:
        provider = AcpProvider(acp_backend=ACP_BACKEND_KIRO)
        assert provider.manual_compact_unsupported_backend is None

    def test_claude_acp_provider_passes(self) -> None:
        provider = AcpProvider(acp_backend=ACP_BACKEND_CLAUDE)
        assert provider.manual_compact_unsupported_backend is None

    def test_kas_session_provider_names_itself(self) -> None:
        """The shared-subagent shape: a bare AcpSessionProvider, no AcpProvider
        wrap. Built via __new__ so the test exercises the property's real logic
        without spawning a runtime."""
        provider = AcpSessionProvider.__new__(AcpSessionProvider)
        provider._runtime = SimpleNamespace(acp_backend=ACP_BACKEND_KAS)  # type: ignore[attr-defined]
        assert provider.manual_compact_unsupported_backend == ACP_BACKEND_KAS

    def test_kiro_session_provider_passes(self) -> None:
        provider = AcpSessionProvider.__new__(AcpSessionProvider)
        provider._runtime = SimpleNamespace(acp_backend=ACP_BACKEND_KIRO)  # type: ignore[attr-defined]
        assert provider.manual_compact_unsupported_backend is None

    def test_abc_default_is_supported(self) -> None:
        """H14: a provider that never declared the capability passes through —
        claude_code and any future non-ACP provider handle /compact on their
        own terms, by ABC default rather than by isinstance inference."""
        assert LLMProvider.manual_compact_unsupported_backend.fget(None) is None  # type: ignore[union-attr, arg-type]

    def test_non_string_backend_passes(self) -> None:
        """An AcpProvider whose client carries a non-str backend (a spec'd mock
        double) must not read as an unsupported backend — the gate fires only
        on a positive string match outside the set, mirroring
        ``provider_label``'s MagicMock caution."""
        provider = AcpProvider(acp_backend=ACP_BACKEND_KIRO)
        provider._client = MagicMock()  # backend attribute becomes a MagicMock
        assert provider.manual_compact_unsupported_backend is None


def _state_and_slot(tmp_path, client):
    state = _make_state(tmp_path)
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("compact-gate-slot")
    return state, slot


def _empty_stream(client: MagicMock) -> MagicMock:
    """Install empty async-generator stream doubles and return a call recorder."""
    calls = MagicMock()

    async def _empty(msg):
        calls(msg)
        return
        yield  # pragma: no cover - generator shape only

    client.stream = _empty
    client.stream_command = _empty
    return calls


class TestDashboardManualCompactGate:
    @pytest.mark.asyncio
    async def test_kas_compact_is_refused_before_dispatch(self, tmp_path) -> None:
        """RED on main: the /compact prompt was dispatched to KAS and the turn
        then stranded on wait_for_compaction. The gate must refuse with an
        immediate user-visible error and never dispatch the prompt."""
        client = MagicMock(spec=AcpProvider)
        client.manual_compact_unsupported_backend = ACP_BACKEND_KAS
        dispatched = _empty_stream(client)
        client.wait_for_compaction = AsyncMock(
            side_effect=AssertionError("must not wait for compaction on KAS")
        )
        state, slot = _state_and_slot(tmp_path, client)

        await _run_chat(state, slot, "/compact")

        dispatched.assert_not_called()
        texts = [m.get("content", "") for m in slot.messages]
        assert any(
            "not supported by the kas backend" in t.lower() for t in texts
        ), f"expected an immediate refusal message, got: {texts!r}"
        # The session permit taken by get_or_create must still be handed back.
        state.sessions.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_member_backend_compact_still_dispatches(self, tmp_path) -> None:
        """A backend inside ACP_BACKENDS_COMPACT keeps the pre-fix behavior:
        the property answers None (the member answer) and the /compact prompt
        is forwarded to the harness."""
        client = MagicMock(spec=AcpProvider)
        client.manual_compact_unsupported_backend = None
        dispatched = _empty_stream(client)
        state, slot = _state_and_slot(tmp_path, client)

        await _run_chat(state, slot, "/compact")

        dispatched.assert_called_once()
        texts = [m.get("content", "") for m in slot.messages]
        assert not any("not supported" in t.lower() for t in texts)

    @pytest.mark.asyncio
    async def test_mocked_provider_attribute_never_reads_as_refusal(self, tmp_path) -> None:
        """The gate acts only on a non-empty str: a bare MagicMock provider
        (whose attribute read returns a truthy child mock) and any non-ACP
        provider inheriting the ABC's None default both pass through."""
        client = MagicMock()  # attribute read yields a MagicMock, not a str
        dispatched = _empty_stream(client)
        client.wait_for_compaction = AsyncMock(return_value={"type": "completed", "summary": ""})
        state, slot = _state_and_slot(tmp_path, client)

        await _run_chat(state, slot, "/compact")

        dispatched.assert_called_once()
        texts = [m.get("content", "") for m in slot.messages]
        assert not any("not supported" in t.lower() for t in texts)
