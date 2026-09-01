"""Tests for the per-crew avatar override on KiroCrewAgentConfig.

Covers:
- _safe_avatar validation (shape guards, trait coercion, tile hex pinning)
- The field's defaults and asdict serialization
- Round-trip through the agents-section from-dict parse
"""

import dataclasses
import json
import tempfile
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    _safe_avatar,
)


def _load_from_dict(data: dict) -> KiroCrewConfig:
    """Write *data* to a temp config file and load via KiroCrewConfig.load()."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


_GHOST = {
    "kind": "ghost",
    "traits": {
        "eyes": "wink",
        "brows": "none",
        "mouth": "smile",
        "accessory": "halo",
        "prop": "none",
        "blush": True,
        "flip": False,
        "tile": "#21a5de",
    },
}


class TestSafeAvatar:
    """Unit tests for the _safe_avatar coercer."""

    def test_valid_ghost_round_trips(self):
        assert _safe_avatar(_GHOST) == _GHOST

    def test_non_dict_collapses(self):
        assert _safe_avatar("ghost") == {}
        assert _safe_avatar(None) == {}
        assert _safe_avatar(["ghost"]) == {}
        assert _safe_avatar(42) == {}

    def test_unknown_kind_collapses(self):
        assert _safe_avatar({"kind": "hologram", "traits": {}}) == {}

    def test_image_kind_not_yet_accepted(self):
        """The upload tier ships separately; until then 'image' collapses."""
        assert _safe_avatar({"kind": "image", "file": "x.png"}) == {}

    def test_missing_traits_collapses(self):
        assert _safe_avatar({"kind": "ghost"}) == {}

    def test_non_dict_traits_collapses(self):
        assert _safe_avatar({"kind": "ghost", "traits": "canon"}) == {}

    def test_missing_trait_keys_get_defaults(self):
        out = _safe_avatar({"kind": "ghost", "traits": {}})
        assert out["traits"] == {
            "eyes": "",
            "brows": "",
            "mouth": "",
            "accessory": "",
            "prop": "",
            "blush": False,
            "flip": False,
            "tile": "",
        }

    def test_non_string_trait_collapses_to_empty(self):
        out = _safe_avatar({"kind": "ghost", "traits": {"eyes": 7}})
        assert out["traits"]["eyes"] == ""

    def test_unknown_trait_keys_dropped(self):
        out = _safe_avatar({"kind": "ghost", "traits": {"hat": "tall", "eyes": "canon"}})
        assert "hat" not in out["traits"]
        assert out["traits"]["eyes"] == "canon"

    def test_overlong_trait_value_truncated(self):
        out = _safe_avatar({"kind": "ghost", "traits": {"eyes": "x" * 500}})
        assert len(out["traits"]["eyes"]) == 32

    def test_bools_require_real_booleans(self):
        """bool("false") is True, so string-typed values must NOT coerce on."""
        out = _safe_avatar({"kind": "ghost", "traits": {"blush": 1, "flip": "true"}})
        assert out["traits"]["blush"] is False
        assert out["traits"]["flip"] is False
        on = _safe_avatar({"kind": "ghost", "traits": {"blush": True}})
        assert on["traits"]["blush"] is True

    def test_tile_pinned_to_hex(self):
        """tile is interpolated into SVG, so junk must not survive."""
        bad = dict(_GHOST, traits=dict(_GHOST["traits"], tile='"><script>'))
        assert _safe_avatar(bad)["traits"]["tile"] == ""

    def test_tile_normalized_lowercase(self):
        raw = dict(_GHOST, traits=dict(_GHOST["traits"], tile="#21A5DE"))
        assert _safe_avatar(raw)["traits"]["tile"] == "#21a5de"


class TestKiroCrewAgentConfigAvatar:
    """avatar field on KiroCrewAgentConfig."""

    def test_default_empty(self):
        assert KiroCrewAgentConfig().avatar == {}

    def test_default_is_not_shared_between_instances(self):
        a, b = KiroCrewAgentConfig(), KiroCrewAgentConfig()
        a.avatar["kind"] = "ghost"
        assert b.avatar == {}

    def test_serializes_in_asdict(self):
        d = dataclasses.asdict(KiroCrewAgentConfig(avatar=_GHOST))
        assert d["avatar"] == _GHOST

    def test_empty_serializes(self):
        d = dataclasses.asdict(KiroCrewAgentConfig())
        assert d["avatar"] == {}


class TestAvatarLoadRoundTrip:
    """The agents-section parse keeps a stored avatar and drops junk."""

    def test_round_trips_through_to_dict(self):
        cfg = KiroCrewConfig()
        cfg.agents["radar"] = KiroCrewAgentConfig(avatar=_GHOST)
        assert cfg.to_dict()["agents"]["radar"]["avatar"] == _GHOST

    def test_loads_from_agents_section(self):
        cfg = _load_from_dict({"agents": {"radar": {"kiro_agent": "kirocrew", "avatar": _GHOST}}})
        assert cfg.agents["radar"].avatar == _GHOST

    def test_junk_avatar_collapses_on_load(self):
        cfg = _load_from_dict({"agents": {"radar": {"kiro_agent": "kirocrew", "avatar": "ghost"}}})
        assert cfg.agents["radar"].avatar == {}


class TestAvatarEndpoints:
    """Create/update refuse junk with a code; valid overrides persist."""

    @staticmethod
    def _app():
        from aiohttp import web

        from kiro_crew.dashboard.handlers import (
            api_kirocrew_agent_update,
            api_kirocrew_agents_create,
        )

        app = web.Application()
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        return app

    @pytest.fixture(autouse=True)
    def _owner_caller(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    @pytest.fixture()
    def seeded_agent(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["existing"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        return "existing"

    @pytest.mark.asyncio
    async def test_update_persists_a_valid_override(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"avatar": _GHOST})
            assert resp.status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].avatar == _GHOST

    @pytest.mark.asyncio
    async def test_update_refuses_junk_with_a_code(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"avatar": "ghost"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_avatar"
        assert KiroCrewConfig.load().agents[seeded_agent].avatar == {}

    @pytest.mark.asyncio
    async def test_update_empty_resets(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        cfg = KiroCrewConfig.load()
        cfg.agents[seeded_agent].avatar = dict(_GHOST)
        cfg.save()
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"avatar": {}})
            assert resp.status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].avatar == {}

    @pytest.mark.asyncio
    async def test_create_accepts_an_override(self):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "radar2", "kiro_agent": "kirocrew", "avatar": _GHOST},
            )
            assert resp.status == 200
        assert KiroCrewConfig.load().agents["radar2"].avatar == _GHOST

    @pytest.mark.asyncio
    async def test_create_refuses_junk_with_a_code(self):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "radar3", "kiro_agent": "kirocrew", "avatar": ["x"]},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_avatar"
        assert "radar3" not in KiroCrewConfig.load().agents
