"""Session-array MCP wiring: agent spec -> session/new mcpServers array.

A backend in ``ACP_BACKENDS_SESSION_MCP_ARRAY`` (claude-agent-acp today) receives
its MCP servers ONLY through the ``session/new`` / ``session/load`` parameter, so
these tests pin the shape the adapter's schema requires (``env``/``headers`` always arrays, an explicit transport ``type``) and
the mounting rules the kiro agent spec expresses (``tools`` references, the
registry pointer, Crew's own control plane).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    # Materialization would try to REBUILD the managed default from bundled
    # defaults; these tests supply the spec themselves.
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}.get(name),
    )
    # Registry mode reads the effective config; pinned off (the default for a
    # personal install) so the symmetric filter is deterministic here. The tests
    # that care flip it explicitly.
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_spec(agents_dir: Path, *, servers: dict, tools: list | None) -> None:
    spec: dict = {"name": "kirocrew", "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _by_name(elements: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in elements}


class TestElementShape:
    def test_stdio_entry_carries_env_array_and_explicit_type(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "args": ["--x"], "env": {"K": "v"}}},
            tools=["@foo"],
        )
        foo = _by_name(session_mcp.session_mcp_servers("kirocrew"))["foo"]
        assert foo == {
            "name": "foo",
            "command": "/bin/foo",
            "args": ["--x"],
            # An array, not a mapping, and PRESENT even when empty: the adapter's
            # schema requires it and rejects the whole session/new otherwise.
            "env": [{"name": "K", "value": "v"}],
            "type": "stdio",
        }

    def test_stdio_entry_without_env_still_emits_the_array(self, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        foo = _by_name(session_mcp.session_mcp_servers("kirocrew"))["foo"]
        assert foo["env"] == []
        assert foo["args"] == []

    def test_non_string_env_and_args_are_stringified(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "args": [7], "env": {"PORT": 8080}}},
            tools=["@foo"],
        )
        foo = _by_name(session_mcp.session_mcp_servers("kirocrew"))["foo"]
        assert foo["env"] == [{"name": "PORT", "value": "8080"}]
        assert foo["args"] == ["7"]

    def test_url_entry_defaults_to_http_with_headers_array(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"remote": {"url": "https://example.test/mcp", "headers": {"A": "b"}}},
            tools=["@remote"],
        )
        remote = _by_name(session_mcp.session_mcp_servers("kirocrew"))["remote"]
        assert remote == {
            "name": "remote",
            # Without an explicit type the adapter routes the entry to its stdio
            # branch and rejects it for having no command.
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": [{"name": "A", "value": "b"}],
        }

    def test_url_entry_keeps_sse_transport(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"remote": {"url": "https://example.test/sse", "type": "sse"}},
            tools=["@remote"],
        )
        remote = _by_name(session_mcp.session_mcp_servers("kirocrew"))["remote"]
        assert remote["type"] == "sse"
        assert remote["headers"] == []

    @pytest.mark.parametrize("bad_args", [8080, "--flag", {"a": 1}, True])
    def test_non_sequence_args_does_not_raise(self, agents_dir, bad_args):
        """``"args": 8080`` must not take the whole session/new down.

        The spec is hand-editable JSON, so a scalar there is an easy mistake.
        Iterating it raises ``TypeError`` (or, for a string, explodes into one
        argument per character), and nothing in this module may raise: the
        exception travels out through ``session_mcp_servers`` and fails the whole
        ``session/new``, costing the session every OTHER server too.
        """
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "args": bad_args}},
            tools=["@foo"],
        )
        assert _by_name(session_mcp.session_mcp_servers("kirocrew"))["foo"]["args"] == []

    def test_entry_with_no_transport_is_skipped(self, agents_dir):
        _write_spec(agents_dir, servers={"broken": {"args": ["--x"]}}, tools=["@broken"])
        assert "broken" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_kiro_only_keys_are_not_forwarded(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={
                "foo": {
                    "command": "/bin/foo",
                    "timeout": 120,
                    "disabledTools": ["x"],
                    "autoApprove": ["y"],
                }
            },
            tools=["@foo"],
        )
        foo = _by_name(session_mcp.session_mcp_servers("kirocrew"))["foo"]
        # autoApprove above all: Claude's equivalent means Claude never asks, so
        # the call would never reach the host gate.
        assert set(foo) == {"name", "command", "args", "env", "type"}


class TestMounting:
    def test_server_not_referenced_by_tools_is_withheld(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"granted": {"command": "/bin/a"}, "ungranted": {"command": "/bin/b"}},
            tools=["@granted"],
        )
        names = _by_name(session_mcp.session_mcp_servers("kirocrew"))
        assert "granted" in names
        assert "ungranted" not in names

    def test_tool_scoped_reference_mounts_the_server(self, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo/only_this"])
        assert "foo" in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_wildcard_reference_mounts_everything(self, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["*"])
        assert "foo" in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_spec_without_tools_mounts_every_declared_server(self, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=None)
        assert "foo" in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_at_wildcard_is_not_a_grant_all(self, agents_dir):
        # kiro documents `*`, `@builtin`, `@server` and `@server/tool` for
        # `tools`; `@*` parses as a server literally named `*`, so it mounts
        # NOTHING on kiro-cli. Reading it as grant-all here would mount every
        # declared server on this backend alone.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@*"])
        assert "foo" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_registry_pointer_is_withheld_outside_registry_mode(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"governed": {"command": "/bin/ignored", "type": "registry"}},
            tools=["@governed"],
        )
        assert "governed" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_registry_mode_withholds_every_spec_declared_server(self, agents_dir, monkeypatch):
        """A marker cannot AUTHORIZE a server here, so a governed install gets none.

        kiro-cli resolves a marked entry against the admin's catalog by map key,
        drops what the catalog omits and applies the catalog's command override.
        Nothing here can do any of that -- only kiro-cli fetches the registry URL,
        and it persists neither the URL nor the catalog. A ``"type": "registry"``
        line is one a user can add to their own spec, so treating it as proof of
        authorization would let a local edit mount a server the administrator
        withheld. The unmarked entries are dropped for the reason they always
        were: kiro-cli drops them too.
        """
        _write_spec(
            agents_dir,
            servers={
                "marked": {"command": "/bin/marked", "type": "registry"},
                "local_only": {"command": "/bin/local"},
            },
            tools=["@marked", "@local_only"],
        )
        monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: True)
        names = _by_name(session_mcp.session_mcp_servers("kirocrew"))
        assert "marked" not in names
        assert "local_only" not in names

    def test_registry_mode_still_keeps_crews_own_control_plane(self, agents_dir, monkeypatch):
        """The withholding is scoped to the user-editable spec, not to the host.

        ``kirocrew-core``/``kirocrew-cron`` are re-derived from the managed source
        rather than read from the spec, and they are the session's only way to
        report back to its channel. Withholding them would reproduce the very
        defect this module exists to fix, on exactly the installs that are most
        governed.
        """
        _write_spec(
            agents_dir,
            servers={"marked": {"command": "/bin/marked", "type": "registry"}},
            tools=["@marked", "@kirocrew-core", "@kirocrew-cron"],
        )
        monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: True)
        names = _by_name(session_mcp.session_mcp_servers("kirocrew"))
        assert "kirocrew-core" in names
        assert "kirocrew-cron" in names

    def test_a_stubbed_server_yields_to_its_broker_stub(self, agents_dir):
        """The caller appends the stub under the SAME name; two would collide.

        Either the raw entry shadows the stub and the session bypasses the broker,
        or both register and every pooled backend runs twice (#927).
        """
        _write_spec(
            agents_dir,
            servers={"pooled": {"command": "/bin/raw"}, "direct": {"command": "/bin/direct"}},
            tools=["@pooled", "@direct"],
        )
        names = _by_name(
            session_mcp.session_mcp_servers("kirocrew", stub_server_names=frozenset({"pooled"}))
        )
        assert "pooled" not in names
        assert "direct" in names

    def test_a_stubbed_control_plane_server_also_yields(self, agents_dir):
        # The control plane is re-derived AFTER the registry filter, so the stub
        # drop has to run after that re-add or a pooled kirocrew-core comes back.
        names = _by_name(
            session_mcp.session_mcp_servers(
                "kirocrew", stub_server_names=frozenset({"kirocrew-core"})
            )
        )
        assert "kirocrew-core" not in names
        assert "kirocrew-cron" in names

    def test_registry_type_matches_the_spec_writer(self):
        # A rename in agent.py must not silently stop this filter from matching.
        assert session_mcp._KIRO_REGISTRY_TYPE == agent_mod._MCP_REGISTRY_TYPE


class TestDenyRules:
    def test_disabled_tools_become_deny_rules(self, agents_dir):
        # disabledTools is a RESTRICTION: dropping it while forwarding the server
        # it narrows would widen the session's tool surface behind the user's back.
        _write_spec(
            agents_dir,
            servers={"srv": {"command": "/bin/srv", "disabledTools": ["danger", "worse"]}},
            tools=["@srv"],
        )
        assert session_mcp.session_mcp_deny_rules("kirocrew") == [
            "mcp__srv__danger",
            "mcp__srv__worse",
        ]

    def test_no_disabled_tools_means_no_rules(self, agents_dir):
        _write_spec(agents_dir, servers={"srv": {"command": "/bin/srv"}}, tools=["@srv"])
        assert session_mcp.session_mcp_deny_rules("kirocrew") == []

    def test_malformed_spec_yields_no_rules(self, agents_dir):
        (agents_dir / "kirocrew.json").write_text("{not json", encoding="utf-8")
        assert session_mcp.session_mcp_deny_rules("kirocrew") == []
        assert session_mcp.session_mcp_deny_rules(None) == []


class TestControlPlane:
    def test_loaded_when_no_spec_exists(self, agents_dir):
        names = _by_name(session_mcp.session_mcp_servers("kirocrew"))
        assert set(names) == {"kirocrew-core", "kirocrew-cron"}
        assert names["kirocrew-core"]["args"] == ["mcp-core"]

    def test_loaded_when_the_spec_is_malformed(self, agents_dir):
        (agents_dir / "kirocrew.json").write_text("{not json", encoding="utf-8")
        assert set(_by_name(session_mcp.session_mcp_servers("kirocrew"))) == {
            "kirocrew-core",
            "kirocrew-cron",
        }

    def test_stale_spec_command_is_refreshed_from_the_managed_source(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/gone/kirocrew", "args": ["mcp-core"]}},
            tools=["@kirocrew-core"],
        )
        core = _by_name(session_mcp.session_mcp_servers("kirocrew"))["kirocrew-core"]
        assert core["command"] == "/opt/kirocrew"

    def test_a_spec_that_drops_the_reference_still_drops_the_server(self, agents_dir):
        # The refresh must not become a re-grant: kiro-cli would not mount a
        # server its tools list does not name, and neither may claude.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        assert "kirocrew-core" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_no_agent_means_control_plane_only(self, agents_dir):
        assert set(_by_name(session_mcp.session_mcp_servers(None))) == {
            "kirocrew-core",
            "kirocrew-cron",
        }


class TestClientSeam:
    def test_kiro_backend_passes_no_array(self, tmp_path, agents_dir):
        client = AcpClient(work_dir=tmp_path)
        # kiro-cli receives the same servers via --agent; a duplicate here would
        # shadow the spec's own entries.
        assert client._session_mcp_servers() == []

    def test_claude_backend_translates_the_spec(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "foo" in _by_name(client._session_mcp_servers())

    def test_the_capability_set_is_what_decides(self, tmp_path, agents_dir, monkeypatch):
        """Membership drives the seam, not the harness's identity.

        The point of the capability set is that the next adapter which reads no
        agent spec joins it and works, with no edit here. Widening the set to
        kiro must therefore be enough to make the array populate -- if this
        passes only for claude, an identity branch has crept back in.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = AcpClient(work_dir=tmp_path, agent="kirocrew")
        assert client._session_mcp_servers() == []
        monkeypatch.setattr(
            client_mod, "ACP_BACKENDS_SESSION_MCP_ARRAY", frozenset({client.backend})
        )
        assert "foo" in _by_name(client._session_mcp_servers())

    def test_the_seam_hands_down_the_pooled_stub_names(self, tmp_path, agents_dir, monkeypatch):
        # The client owns the overlay, so it is the only layer that can answer
        # which servers will ALSO arrive as broker stubs.
        _write_spec(
            agents_dir,
            servers={"pooled": {"command": "/bin/raw"}, "direct": {"command": "/bin/direct"}},
            tools=["@pooled", "@direct"],
        )
        monkeypatch.setattr(
            client_mod, "injection_server_names", lambda _o, _a: frozenset({"pooled"})
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        names = _by_name(client._session_mcp_servers())
        assert "pooled" not in names
        assert "direct" in names

    def test_an_unreadable_overlay_does_not_cost_the_session_its_servers(
        self, tmp_path, agents_dir, monkeypatch
    ):
        # Empty is the safe direction: re-declaring a stubbed server lets the
        # injection outrank it, while withholding one nothing else supplies is a
        # session with missing tools.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])

        def _boom(_o, _a):
            raise RuntimeError("overlay unreadable")

        monkeypatch.setattr(client_mod, "injection_server_names", _boom)
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "foo" in _by_name(client._session_mcp_servers())

    def test_the_shared_call_site_reads_no_disk_for_kiro(self, tmp_path, agents_dir, monkeypatch):
        """harness-parity H13: the kiro construction path gains nothing.

        Both session-params call sites are shared with kiro-cli, so the accessor
        they call must be synchronous AND must not reach the translator for a
        backend outside the capability set. If it did, adapter work would have put
        a new scheduling and failure point on kiro's ``session/new``.
        """

        def _never(*_a, **_kw):
            raise AssertionError("the kiro path must not translate a spec")

        monkeypatch.setattr(client_mod, "session_mcp_servers", _never)
        monkeypatch.setattr(client_mod, "injection_server_names", _never)
        client = AcpClient(work_dir=tmp_path, agent="kirocrew")
        result = client._session_mcp_servers()
        assert result == []
        # A coroutine here would force the shared call site to await.
        assert not hasattr(result, "__await__")

    def test_the_array_is_resolved_once_and_dropped_on_reset(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """Cached per spawn, not per call site, and re-read on the next spawn.

        session/new and the session/load that resumes it both read the accessor;
        translating twice would double the disk work for one session. Clearing on
        reset is what keeps the "installing a server takes effect on the NEXT
        session" promise.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        calls: list[int] = []
        real = session_mcp.session_mcp_servers

        def _counted(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        monkeypatch.setattr(client_mod, "session_mcp_servers", _counted)
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "foo" in _by_name(client._session_mcp_servers())
        assert "foo" in _by_name(client._session_mcp_servers())
        assert len(calls) == 1
        client._reset_state()
        assert client._session_mcp_cache is None
        assert "foo" in _by_name(client._session_mcp_servers())
        assert len(calls) == 2

    def test_the_cached_array_is_not_aliased_to_callers(self, tmp_path, agents_dir):
        # The two call sites splat this list into their params; handing out the
        # cache itself would let one session/load mutation reach the next.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        first = client._session_mcp_servers()
        first.clear()
        assert "foo" in _by_name(client._session_mcp_servers())


class TestLocalSettingsSeed:
    def _client(self, tmp_path, **kw):
        return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kw)

    def test_seed_writes_the_model_allowlist(self, tmp_path):
        from kiro_crew import model_registry

        client = self._client(tmp_path)
        client._write_claude_local_settings()
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        # Without the allowlist the adapter can collapse a versioned [1m] id back
        # to the 200K window.
        assert data["availableModels"] == model_registry.available_models("claude_code")

    def test_no_permission_mode_leaves_the_adapter_default(self, tmp_path):
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert "permissions" not in data

    def test_permission_mode_is_written_when_requested(self, tmp_path):
        client = self._client(tmp_path, permission_mode="default")
        client._write_claude_local_settings()
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert data["permissions"]["defaultMode"] == "default"

    def test_resolved_model_written_but_auto_omitted(self, tmp_path):
        auto = self._client(tmp_path)
        auto._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        assert "model" not in json.loads(path.read_text())
        pinned = self._client(tmp_path, model="claude-sonnet-4-5")
        pinned._write_claude_local_settings()
        assert json.loads(path.read_text())["model"] == "claude-sonnet-4-5"

    def test_user_settings_are_merged_and_restored(self, tmp_path):
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        original = json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "env": {"X": "1"}}, indent=2)
        path.write_text(original, encoding="utf-8")

        client = self._client(tmp_path, permission_mode="default")
        client._write_claude_local_settings()
        seeded = json.loads(path.read_text())
        # The user's non-grant keys survive the seed...
        # An inherited grant is dropped instead -- see the dedicated test below.
        assert "allow" not in seeded["permissions"]
        assert seeded["env"] == {"X": "1"}
        assert seeded["permissions"]["defaultMode"] == "default"

        client._reset_state()
        # ...and the file is the user's own again afterwards, so no permission
        # mode outlives the session that asked for it.
        assert path.read_text() == original

    def test_a_file_crew_created_is_removed_on_reset(self, tmp_path):
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        assert path.exists()
        client._reset_state()
        assert not path.exists()

    def test_an_inherited_bypass_mode_is_stripped_for_the_session(self, tmp_path):
        """bypassPermissions is the one mode that takes every call out of the gate.

        The adapter short-circuits its canUseTool callback for it, so nothing
        reaches the deny floor, the sensitive-path check or the governance
        ceiling. The base code swept this whole file on every reset for exactly
        that reason; preserving the user's file instead must not also preserve
        this value for the window Crew drives the session.
        """
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        original = json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}, indent=2)
        path.write_text(original, encoding="utf-8")

        client = self._client(tmp_path)
        client._write_claude_local_settings()
        assert "defaultMode" not in json.loads(path.read_text()).get("permissions", {})

        client._reset_state()
        # The user's own file is not edited -- only the session was protected.
        assert path.read_text() == original

    def test_inherited_grants_are_stripped_for_the_session(self, tmp_path):
        """An allow rule pre-approves a call inside the adapter.

        It never reaches Crew's canUseTool callback, so the call skips the
        permission gate, the governance ceiling and the SEL audit -- the same
        short-circuit as bypassPermissions, only scoped to one tool. A project
        file written for a bare ``claude`` run legitimately carries these, so
        they are dropped for the session rather than edited out of the file.
        """
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        original = json.dumps(
            {
                "permissions": {
                    "allow": ["mcp__kirocrew-core__spawn_run", "Bash(git push)"],
                    "ask": ["Write"],
                    "deny": ["Bash(curl)"],
                }
            },
            indent=2,
        )
        path.write_text(original, encoding="utf-8")

        client = self._client(tmp_path)
        client._write_claude_local_settings()
        perms = json.loads(path.read_text())["permissions"]
        assert "allow" not in perms
        assert "ask" not in perms
        # deny is the one direction that only ever tightens, so it stays.
        assert perms["deny"] == ["Bash(curl)"]

        client._reset_state()
        # The user's own file is untouched -- only the session was protected.
        assert path.read_text() == original

    def test_an_empty_grant_list_is_left_alone(self, tmp_path):
        """Only a populated grant is worth a warning; an empty list grants nothing."""
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"permissions": {"allow": []}}', encoding="utf-8")
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        assert json.loads(path.read_text())["permissions"]["allow"] == []

    def test_a_settings_file_deleted_mid_session_is_not_resurrected(self, tmp_path):
        """Absence is a foreign change, not "unchanged".

        Reading the seeded file back fails the same way for a deleted file and an
        unreadable one. Treating that as "nobody touched it" would let reset
        re-create a file the user deliberately removed, restoring settings they
        had just deleted on purpose.
        """
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"env": {"X": "1"}}), encoding="utf-8")

        client = self._client(tmp_path)
        client._write_claude_local_settings()
        assert path.exists()

        path.unlink()
        client._reset_state()
        assert not path.exists()

    def test_an_explicit_mode_still_wins_over_an_inherited_bypass(self, tmp_path):
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"permissions": {"defaultMode": "bypassPermissions"}}', encoding="utf-8")
        client = self._client(tmp_path, permission_mode="default")
        client._write_claude_local_settings()
        assert json.loads(path.read_text())["permissions"]["defaultMode"] == "default"

    def test_disabled_tools_reach_the_settings_deny_list(self, tmp_path, agents_dir):
        _write_spec(
            agents_dir,
            servers={"srv": {"command": "/bin/srv", "disabledTools": ["danger"]}},
            tools=["@srv"],
        )
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        # A deny rule the user wrote themselves must survive the merge.
        path.write_text('{"permissions": {"deny": ["Bash(rm)"]}}', encoding="utf-8")
        client = self._client(tmp_path, agent="kirocrew")
        client._write_claude_local_settings()
        assert json.loads(path.read_text())["permissions"]["deny"] == [
            "Bash(rm)",
            "mcp__srv__danger",
        ]

    def test_reset_stands_aside_while_another_session_still_holds_the_file(self, tmp_path):
        """Two claude sessions can share one work_dir (every keyless client does).

        Without the ownership check the first to reset deletes the file the
        second's adapter is configured from.
        """
        path = tmp_path / ".claude" / "settings.local.json"
        first = self._client(tmp_path)
        first._write_claude_local_settings()
        second = self._client(tmp_path, model="claude-sonnet-4-5")
        second._write_claude_local_settings()

        first._reset_state()
        # The second session's file is untouched: it owns the undo now.
        assert json.loads(path.read_text())["model"] == "claude-sonnet-4-5"
        second._reset_state()
        assert not path.exists()

    def test_the_last_session_out_restores_the_users_own_file(self, tmp_path):
        """The second claimant must not mistake the first one's seed for the original.

        A per-client snapshot got this wrong: the second session read the file
        AFTER the first had seeded it, so its reset wrote Crew's own settings
        back as if the user had authored them, leaving nothing that would ever
        remove them.
        """
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        original = json.dumps({"env": {"X": "1"}}, indent=2)
        path.write_text(original, encoding="utf-8")

        first = self._client(tmp_path)
        first._write_claude_local_settings()
        second = self._client(tmp_path, model="claude-sonnet-4-5")
        second._write_claude_local_settings()

        first._reset_state()
        assert "availableModels" in json.loads(path.read_text())
        second._reset_state()
        assert path.read_text() == original

    def test_a_user_edit_during_the_session_is_not_overwritten(self, tmp_path):
        path = tmp_path / ".claude" / "settings.local.json"
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path.write_text('{"env": {"MINE": "1"}}', encoding="utf-8")
        client._reset_state()
        assert path.read_text() == '{"env": {"MINE": "1"}}'

    def test_reset_without_a_seed_leaves_a_foreign_file_alone(self, tmp_path):
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"permissions": {"allow": []}}', encoding="utf-8")
        client = self._client(tmp_path)
        client._reset_state()
        # Never seeded, so nothing here belongs to Crew to clean up.
        assert path.exists()

    def test_malformed_user_settings_do_not_block_the_seed(self, tmp_path):
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        assert "availableModels" in json.loads(path.read_text())
        client._reset_state()
        assert path.read_text() == "{not json"

    @pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform cannot create symlinks")
    def test_a_symlinked_settings_file_is_neither_read_nor_written(self, tmp_path):
        """A cloned project can ship a link to a credential file.

        Following it would pull those bytes into the seed merge, and the restore
        write REPLACES the inode -- so the credential contents would come back as
        an ordinary file inside the project, where the file browser, the agent and
        the next ``git add`` all reach them.
        """
        secret = tmp_path / "outside" / "credentials"
        secret.parent.mkdir()
        secret.write_text("[default]\naws_secret_access_key = shhh\n", encoding="utf-8")
        path = tmp_path / "proj" / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.symlink_to(secret)

        client = self._client(tmp_path / "proj")
        client._write_claude_local_settings()
        # Not read (nothing merged), not written (the link is intact and the
        # target is untouched), and the seed is recorded as skipped.
        assert path.is_symlink()
        assert secret.read_text() == "[default]\naws_secret_access_key = shhh\n"
        assert client._claude_settings_usable is False
        assert client._claude_settings_seeded is None

        client._reset_state()
        # And reset removes nothing: a None prior means "Crew created this" on the
        # normal path, which for a refused path would delete the user's own link.
        assert path.is_symlink()
        assert secret.exists()

    @pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform cannot create symlinks")
    def test_a_symlinked_claude_directory_is_refused_too(self, tmp_path):
        # The guard has to cover the parent: linking `.claude` reaches any file
        # under it just as well as linking the leaf.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "settings.local.json").write_text('{"mine": true}', encoding="utf-8")
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".claude").symlink_to(elsewhere, target_is_directory=True)

        client = self._client(proj)
        client._write_claude_local_settings()
        assert (elsewhere / "settings.local.json").read_text() == '{"mine": true}'
        assert client._claude_settings_usable is False

    def test_an_unreadable_existing_file_is_not_treated_as_absent(self, tmp_path):
        """Only FileNotFoundError means "Crew creates it, so reset removes it".

        Any other read failure on an EXISTING file used to collapse to the same
        ``None`` prior, which is the value that authorizes reset to unlink.
        """
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"mine": true}', encoding="utf-8")
        client = self._client(tmp_path)
        with patch.object(Path, "read_text", side_effect=PermissionError("nope")):
            client._write_claude_local_settings()
        assert client._claude_settings_usable is False
        client._reset_state()
        assert path.read_text() == '{"mine": true}'

    def test_the_seed_carries_the_files_access_control(self, tmp_path):
        """``atomic_write`` replaces the inode, so a named ACL needs carrying.

        Asserted at the seam rather than by planting a real ACL: the xattr
        syscalls exist only on Linux, and what must not regress is that both
        writes pass a descriptor for the file they are replacing.
        """
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"mine": true}', encoding="utf-8")
        client = self._client(tmp_path)
        with patch.object(client_mod, "open_access_control_source", return_value=None) as opened:
            client._write_claude_local_settings()
            assert opened.call_args_list, "seed did not carry the existing file's ACL"
            opened.reset_mock()
            client._reset_state()
            assert opened.call_args_list, "restore did not carry the file's ACL"

    def test_a_file_crew_creates_carries_nothing(self, tmp_path):
        # There is no source inode yet, and opening one that is not there raises.
        client = self._client(tmp_path)
        with patch.object(client_mod, "open_access_control_source") as opened:
            client._write_claude_local_settings()
        opened.assert_not_called()

    def test_seed_failure_does_not_break_the_spawn_path(self, tmp_path):
        client = self._client(tmp_path)
        with patch("kiro_crew.acp.client.atomic_write", side_effect=OSError("read-only")):
            with pytest.raises(OSError):
                client._write_claude_local_settings()
        # The caller (_spawn) swallows OSError; what matters is that the snapshot
        # was still taken, so reset does not leave a half-written file behind.
        assert client._claude_settings_captured is True
