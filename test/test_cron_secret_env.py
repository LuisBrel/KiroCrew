"""Tests for operator-granted vault secrets on script/command crons.

Covers the grant validator, the code pin, fail-closed resolution, env
injection in both sandboxed runners, and the persistence-layer gate in
CronService (agent jobs refused, pin required, revoke clears).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cron import CronService
from kiro_crew.cron_script import (
    _filter_grant_env,
    _secret_env_precheck,
    compute_secret_env_pin,
    run_command_sandboxed,
    run_script_sandboxed,
    validate_secret_env_grant,
)
from kiro_crew.secrets import SecretVault


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Cron writes require a nameable caller; these tests assume it."""


@pytest.fixture(autouse=True)
def cron_home(monkeypatch, tmp_path):
    """Point cron_script.config_dir at the per-test patched home.

    Same redirect as test_cron_script.py: scripts are written under
    ``<home>/.kirocrew/crons`` and the vault lives beside them, so the
    resolver, the pin computation, and the vault all agree on one root.

    Tests that do NOT patch ``Path.home`` still reach this resolver through
    the grant-pin key (every ``compute_secret_env_pin`` call), so those fall
    back to a per-test tmp dir — never the operator's real ``~/.kirocrew``,
    which this fixture must not create, read, or key. Returns the resolver
    callable so tests can plant script files where the resolver looks.
    """
    real_home = Path.home()
    fallback = tmp_path / "kirocrew-home-fallback"

    def _dir() -> Path:
        home = Path.home()
        if home == real_home:
            return fallback
        return home / ".kirocrew"

    monkeypatch.setattr("kiro_crew.cron_script.config_dir", _dir)
    return _dir


def _make_script(tmp_path: Path, body: str, name: str = "job.py") -> Path:
    crons_dir = tmp_path / ".kirocrew" / "crons"
    crons_dir.mkdir(parents=True, exist_ok=True)
    script = crons_dir / name
    script.write_text(body)
    return script


def _grant_script(cron_home, body: str = "def run(ctx): pass\n", name: str = "grantee.py") -> str:
    """Plant a script where cron_script's resolver looks; return its spec.

    For tests that do NOT patch ``Path.home`` (endpoint/store/MCP flows):
    grants apply only to script jobs, so every grant fixture needs a real,
    resolvable script file.
    """
    crons_dir = cron_home() / "crons"
    crons_dir.mkdir(parents=True, exist_ok=True)
    script = crons_dir / name
    script.write_text(body)
    return str(script) + ":run"


class TestValidateSecretEnvGrant:
    def test_valid_grant_passes(self):
        validate_secret_env_grant({"MY_SANDBOX_TOKEN": "slack-sandbox"})

    def test_lowercase_key_rejected(self):
        with pytest.raises(ValueError, match="valid env-var name"):
            validate_secret_env_grant({"my_token": "x"})

    @pytest.mark.parametrize(
        "key",
        [
            "PATH",
            "SLACK_BOT_TOKEN",  # _CRON_ENV_DENY member
            "KIROCREW_INTERNAL_SECRET",
            "KIROCREW_ANYTHING",
            "_KIROCREW_SECRET_FILE",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
        ],
    )
    def test_protected_names_rejected(self, key):
        # A leading-underscore name (_KIROCREW_*) fails the grammar check
        # first; the rest hit the protected-name check. Both refuse.
        with pytest.raises(ValueError, match="env-var name"):
            validate_secret_env_grant({key: "x"})

    @pytest.mark.parametrize("name", ["", " padded ", "trailing "])
    def test_bad_vault_name_rejected(self, name):
        with pytest.raises(ValueError, match="vault"):
            validate_secret_env_grant({"MY_TOKEN": name})

    def test_entry_cap_enforced(self):
        grant = {f"KEY_{i}": "v" for i in range(17)}
        with pytest.raises(ValueError, match="max 16"):
            validate_secret_env_grant(grant)


class TestComputeSecretEnvPin:
    def test_command_job_is_refused(self, cron_home):
        """A pin over command TEXT cannot cover helper-file bytes the command
        invokes, so command jobs cannot carry grants at all."""
        with pytest.raises(ValueError, match="script jobs"):
            compute_secret_env_pin("", "echo hi")

    def test_script_pin_tracks_body(self, tmp_path):
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin1 = compute_secret_env_pin(spec, "")
            script.write_text("def run(ctx): return 1\n")
            pin2 = compute_secret_env_pin(spec, "")
        assert pin1 != pin2

    def test_neither_script_nor_command_raises(self):
        with pytest.raises(ValueError, match="script jobs"):
            compute_secret_env_pin("", "")


class TestSecretEnvPrecheck:
    def test_empty_grant_is_noop(self):
        resolved, err = _secret_env_precheck(None, "", command="echo hi")
        assert resolved == {} and err is None
        resolved, err = _secret_env_precheck({}, "", command="echo hi")
        assert resolved == {} and err is None

    def test_missing_pin_fails_closed(self):
        resolved, err = _secret_env_precheck({"T": "x"}, "", command="echo hi")
        assert resolved == {}
        assert err is not None and "re-approve" in err

    def test_changed_body_fails_closed(self, tmp_path):
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", grant={"T": "x"})
            script.write_text("def run(ctx): return 1\n")
            body = script.read_bytes()
            resolved, err = _secret_env_precheck({"T": "x"}, pin, script=spec, script_body=body)
        assert resolved == {}
        assert err is not None and "code changed" in err

    def test_missing_vault_entry_fails_closed(self, tmp_path):
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", grant={"MY_TOKEN": "not-stored"})
            resolved, err = _secret_env_precheck(
                {"MY_TOKEN": "not-stored"}, pin, script=spec, script_body=script.read_bytes()
            )
        assert resolved == {}
        assert err is not None and "does not exist" in err
        # The vault secret NAME must not be echoed (CWE-117 discipline is
        # key-only messages); the env-var key is operator config and may be.
        assert "not-stored" not in err
        assert "MY_TOKEN" in err

    def test_resolves_from_vault(self, tmp_path):
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-123")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", grant={"MY_SANDBOX_TOKEN": "slack-sandbox"})
            resolved, err = _secret_env_precheck(
                {"MY_SANDBOX_TOKEN": "slack-sandbox"},
                pin,
                script=spec,
                script_body=script.read_bytes(),
            )
        assert err is None
        assert resolved == {"MY_SANDBOX_TOKEN": "xoxb-123"}


class TestRunCommandSandboxedSecretEnv:
    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch, posix_test_shell):
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr(
            "kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell
        )

    def test_command_grant_fails_closed(self, tmp_path):
        """Grants apply to script jobs only; a command job carrying one means
        the store was edited outside the product — refuse the run entirely."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_command_sandboxed(
                "echo hi",
                timeout=30,
                secret_env={"MY_TOKEN": "slack-sandbox"},
                secret_env_pin="deadbeef" * 8,
            )
        assert result["status"] == "error"
        assert "script jobs" in result["output"]

    def test_no_grant_runs_normally(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_command_sandboxed('printf "%s" "${MY_TOKEN:-unset}"', timeout=30)
        assert result["status"] == "ok", result
        assert result["output"] == "unset"


class TestRunScriptSandboxedSecretEnv:
    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (["/usr/bin/env", *argv], None)
        )

    def test_unsandboxed_host_refuses_granted_run(self, tmp_path, monkeypatch):
        """A wrap_argv that hands back the argv unmodified (no-backend host
        with the unsandboxed opt-in) loses the crons-dir hiding — a granted
        run must refuse rather than degrade its approved isolation."""
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", job_id="job1", grant=grant)
            result = run_script_sandboxed(
                spec, "job1", timeout=120, secret_env=grant, secret_env_pin=pin
            )
        assert result["status"] == "error"
        assert "sandbox" in result["error"]

    def test_pending_pin_copied_to_active_fields_fails_closed(self, tmp_path):
        """The domain-separation exploit: a store writer copies a valid
        PENDING pin into the active grant fields. It must not verify."""
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            pending_pin = compute_secret_env_pin(
                spec, "", job_id="job1", grant=grant, domain="pending"
            )
            result = run_script_sandboxed(
                spec, "job1", timeout=120, secret_env=grant, secret_env_pin=pending_pin
            )
        assert result["status"] == "error"
        assert "code changed" in result["error"]

    def test_foreign_job_pin_fails_closed(self, tmp_path):
        """A pin minted for one job must not authorize another job's grant."""
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            other_pin = compute_secret_env_pin(spec, "", job_id="other-job", grant=grant)
            result = run_script_sandboxed(
                spec, "job1", timeout=120, secret_env=grant, secret_env_pin=other_pin
            )
        assert result["status"] == "error"

    def test_granted_secret_reaches_script(self, tmp_path):
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(
            tmp_path,
            "import os\n"
            "from kiro_crew.cron_script import Done\n"
            "def run(ctx):\n"
            "    raise Done(os.environ.get('MY_SANDBOX_TOKEN', 'MISSING'))\n",
        )
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(
                spec, "", job_id="job1", grant={"MY_SANDBOX_TOKEN": "slack-sandbox"}
            )
            result = run_script_sandboxed(
                spec,
                "job1",
                timeout=120,
                secret_env={"MY_SANDBOX_TOKEN": "slack-sandbox"},
                secret_env_pin=pin,
            )
        assert result["status"] == "done", result
        assert result["message"] == "xoxb-script"

    def test_body_rewrite_after_grant_fails_closed(self, tmp_path):
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        canary = tmp_path / "exfil-canary"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "")
            # The agent-writeable script is swapped after the operator granted.
            script.write_text(
                "import os\n"
                "def run(ctx):\n"
                f"    open({str(canary)!r}, 'w').write(os.environ.get('MY_SANDBOX_TOKEN',''))\n"
            )
            result = run_script_sandboxed(
                spec,
                "job1",
                timeout=120,
                secret_env={"MY_SANDBOX_TOKEN": "slack-sandbox"},
                secret_env_pin=pin,
            )
        assert result["status"] == "error"
        assert "code changed" in result["error"]
        # Fail closed means the payload never RAN, not merely that it errored.
        assert not canary.exists()


class TestCronServiceSecretEnvGate:
    def _service(self, tmp_path: Path) -> CronService:
        return CronService(base_dir=tmp_path / "crons-store")

    def _script_job(self, svc, cron_home, grant=None):
        spec = _grant_script(cron_home)
        job = svc.add_job("j", "m", every_secs=3600, script=spec)
        pin = compute_secret_env_pin(spec, "", "m", job_id=job.id, grant=grant, domain="active")
        return job, spec, pin

    def test_refused_approval_leaves_active_grant_valid(self, tmp_path, cron_home):
        """A refused grant operation must not invalidate the grant it did not
        replace: minting peeks the next epoch and commits only after the
        store swap, so the existing pin survives a 409."""
        from kiro_crew.cron_script import peek_grant_epoch

        svc = self._service(tmp_path)
        job, spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "slack-sandbox"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin)
        # A refused operation minted-but-never-committed: peek alone must not
        # move the epoch the verifier reads.
        peek_grant_epoch(job.id)
        resolved_ok, err_ok = _secret_env_precheck(
            {"MY_TOKEN": "slack-sandbox"},
            pin,
            script=spec,
            script_body=(cron_home() / "crons" / "grantee.py").read_bytes(),
            message="m",
            job_id=job.id,
        )
        assert err_ok is None or "does not exist" in err_ok  # pin verified; vault may be empty

    def test_revoked_grant_cannot_be_replayed_from_store(self, tmp_path, cron_home):
        """The agent can write the cron store: a saved copy of a formerly
        valid mapping+pin written back after a revoke must not verify —
        the revoke bumped the grant epoch held in the agent-fenced vault."""
        from kiro_crew.cron_script import bump_grant_epoch

        svc = self._service(tmp_path)
        job, spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "slack-sandbox"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin)
        # Owner revokes (the endpoint bumps the epoch alongside the clear).
        bump_grant_epoch(job.id)
        svc.update_job(job.id, secret_env={})
        # Agent replays the saved mapping+pin into the writable store.
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin)
        resolved, err = _secret_env_precheck(
            {"MY_TOKEN": "slack-sandbox"},
            pin,
            script=spec,
            script_body=(cron_home() / "crons" / "grantee.py").read_bytes(),
            message="m",
            job_id=job.id,
        )
        assert resolved == {}
        assert err is not None and "code changed" in err

    def test_grant_persists_and_round_trips(self, tmp_path, cron_home):
        svc = self._service(tmp_path)
        job, _spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "slack-sandbox"})
        updated = svc.update_job(
            job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin
        )
        assert updated is not None
        assert updated.secret_env == {"MY_TOKEN": "slack-sandbox"}
        assert updated.secret_env_pin == pin
        # Round-trip through a fresh service instance (disk reload).
        svc2 = self._service(tmp_path)
        reloaded = next(j for j in svc2.list_jobs() if j.id == job.id)
        assert reloaded.secret_env == {"MY_TOKEN": "slack-sandbox"}
        assert reloaded.secret_env_pin == pin

    def test_agent_job_refused(self, tmp_path):
        svc = self._service(tmp_path)
        job = svc.add_job("j", "check the queue", every_secs=3600)
        with pytest.raises(ValueError, match="SCRIPT jobs"):
            svc.update_job(job.id, secret_env={"MY_TOKEN": "x"}, secret_env_pin="deadbeef")

    def test_command_job_refused(self, tmp_path):
        """A pin over command TEXT cannot cover helper-file bytes the command
        invokes, so the store refuses grants on command jobs outright."""
        svc = self._service(tmp_path)
        job = svc.add_job("j", "m", every_secs=3600, command="echo hi")
        with pytest.raises(ValueError, match="SCRIPT jobs"):
            svc.update_job(job.id, secret_env={"MY_TOKEN": "x"}, secret_env_pin="deadbeef")

    def test_grant_without_pin_refused(self, tmp_path, cron_home):
        svc = self._service(tmp_path)
        job, _spec, _pin = self._script_job(svc, cron_home)
        with pytest.raises(ValueError, match="secret_env_pin"):
            svc.update_job(job.id, secret_env={"MY_TOKEN": "x"})

    def test_protected_name_refused_at_persistence(self, tmp_path, cron_home):
        svc = self._service(tmp_path)
        job, _spec, _pin = self._script_job(svc, cron_home)
        with pytest.raises(ValueError, match="protected env-var name"):
            svc.update_job(
                job.id,
                secret_env={"SLACK_BOT_TOKEN": "x"},
                secret_env_pin="deadbeef",
            )

    def test_revoke_clears_pin(self, tmp_path, cron_home):
        svc = self._service(tmp_path)
        job, _spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "n"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "n"}, secret_env_pin=pin)
        revoked = svc.update_job(job.id, secret_env={})
        assert revoked is not None
        assert revoked.secret_env == {}
        assert revoked.secret_env_pin == ""

    def test_unrelated_update_leaves_grant_alone(self, tmp_path, cron_home):
        svc = self._service(tmp_path)
        job, _spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "n"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "n"}, secret_env_pin=pin)
        updated = svc.update_job(job.id, name="renamed")
        assert updated is not None
        assert updated.secret_env == {"MY_TOKEN": "n"}
        assert updated.secret_env_pin == pin


class TestInjectionDefenseInDepth:
    def test_protected_keys_skipped_at_injection(self):
        """A hand-edited store could hold a protected key; the delivery
        filter drops it on every path (env preamble and stdin payload), so
        product-internal keys can never be named by a grant."""
        kept = _filter_grant_env({"_KIROCREW_SECRET_FILE": "evil", "OK_TOKEN": "fine"})
        assert kept == {"OK_TOKEN": "fine"}

    def test_forged_plain_hash_pin_is_rejected(self, tmp_path):
        """The pin is keyed HMAC: a store writer who recomputes the old plain
        sha256 shape (or any digest without the vault-fenced key) cannot mint
        a pin the runner accepts."""
        import hashlib

        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        forged = hashlib.sha256(
            b"script\x00" + spec.encode() + b"\x00" + script.read_bytes()
        ).hexdigest()
        with patch("pathlib.Path.home", return_value=tmp_path):
            resolved, err = _secret_env_precheck(
                {"T": "x"}, forged, script=spec, script_body=script.read_bytes()
            )
        assert resolved == {}
        assert err is not None and "code changed" in err

    def test_granted_script_cannot_import_live_crons_sibling(self, tmp_path):
        """The pin covers one approved body; a sibling module in the live,
        agent-writeable crons/ dir must not be importable at fire time."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            script = _make_script(
                tmp_path,
                "import helper\n"
                "from kiro_crew.cron_script import Done\n"
                "def run(ctx):\n"
                "    raise Done(helper.VALUE)\n",
            )
            _make_script(tmp_path, "VALUE = 'sibling ran'\n", name="helper.py")
            spec = str(script) + ":run"
            pin = compute_secret_env_pin(
                spec, "", job_id="job1", grant={"MY_TOKEN": "slack-sandbox"}
            )
            with patch(
                "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (["/usr/bin/env", *argv], None)
            ):
                SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-1")
                result = run_script_sandboxed(
                    spec,
                    "job1",
                    timeout=120,
                    secret_env={"MY_TOKEN": "slack-sandbox"},
                    secret_env_pin=pin,
                )
        # The sibling import fails (ModuleNotFoundError surfaces as the
        # script error), rather than the unpinned helper running with the
        # secret in env.
        assert result["status"] == "error", result
        assert "helper" in result.get("error", "")


class TestMcpSecretRequest:
    """The agent-reachable half: cron_secret_request writes PENDING only."""

    def _svc_and_job(self, cron_home=None, *, agent: bool = False):
        from kiro_crew.config.loader import config_dir as loader_config_dir

        svc = CronService(base_dir=loader_config_dir())
        spec = "" if agent else _grant_script(cron_home)
        job = svc.add_job(
            "j",
            "m" if agent else "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        return svc, job

    def _vault(self):
        from kiro_crew.config.loader import config_dir as loader_config_dir

        return SecretVault(loader_config_dir())

    def test_request_records_pending_not_active(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        self._vault().set_sync("slack-sandbox", "xoxb-1")
        out = mcp_cron._call_tool(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}},
        )
        assert "PENDING" in out
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}
        assert reloaded.secret_env_pending_pin
        # The active grant is untouched — the tool cannot grant.
        assert reloaded.secret_env == {}
        assert reloaded.secret_env_pin == ""

    def test_request_does_not_reveal_vault_name_existence(self, cron_home):
        """No existence probe on the agent surface: a request naming an
        unstored secret is accepted as pending exactly like a stored one, so
        an agent cannot enumerate the owner's vault names by guessing.
        Validation happens on the owner-only approval surfaces."""
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        out = mcp_cron._call_tool(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "never-stored"}},
        )
        assert not out.startswith("Error:")
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env_pending == {"MY_TOKEN": "never-stored"}

    def test_request_refuses_agent_job(self):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(agent=True)
        self._vault().set_sync("slack-sandbox", "xoxb-1")
        out = mcp_cron._call_tool(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}},
        )
        assert out.startswith("Error:")

    def test_request_refuses_protected_env_name(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        self._vault().set_sync("slack-sandbox", "xoxb-1")
        out = mcp_cron._call_tool(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"SLACK_BOT_TOKEN": "slack-sandbox"}},
        )
        assert out.startswith("Error:")

    def test_empty_request_withdraws_pending(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        self._vault().set_sync("slack-sandbox", "xoxb-1")
        mcp_cron._call_tool(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}},
        )
        out = mcp_cron._call_tool("cron_secret_request", {"job_id": job.id, "secrets": {}})
        assert "Withdrew" in out
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env_pending == {}
        assert reloaded.secret_env_pending_pin == ""

    def test_cron_update_cannot_write_active_grant(self, cron_home):
        """The general-purpose MCP update tool must never carry the grant."""
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        mcp_cron._call_tool(
            "cron_update",
            {
                "job_id": job.id,
                "name": "renamed",
                "secret_env": {"MY_TOKEN": "slack-sandbox"},
                "secret_env_pin": "deadbeef",
            },
        )
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env == {}
        assert reloaded.secret_env_pin == ""


class TestGrantEndpointPendingFlow:
    """The operator half: approve/deny via the dashboard endpoint."""

    @pytest.fixture(autouse=True)
    def _owner_view(self, monkeypatch):
        """These tests exercise grant flow, not identity; grant is owner-only."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    def _app_and_svc(self):
        from types import SimpleNamespace

        from aiohttp import web

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.dashboard.handlers.cron import api_cron_secret_grant

        svc = CronService(base_dir=loader_config_dir())
        app = web.Application()
        app["state"] = SimpleNamespace(crons=svc, push_refresh=lambda *a, **k: None)
        app.router.add_put("/api/crons/{job_id}/secrets", api_cron_secret_grant)
        return app, svc

    def _request_pending(self, svc, cron_home) -> str:
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        pin = compute_secret_env_pin(
            spec,
            "",
            "args",
            job_id=job.id,
            grant={"MY_TOKEN": "slack-sandbox"},
            domain="pending",
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=pin,
            secret_env_pending_ts=1.0,
        )
        return job.id

    @pytest.mark.asyncio
    async def test_approve_promotes_pending_to_active(self, cron_home):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json={"approve_pending": True})
        assert resp.status == 200
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {"MY_TOKEN": "slack-sandbox"}
        assert job.secret_env_pin
        assert job.secret_env_pending == {}

    @pytest.mark.asyncio
    async def test_approve_refuses_when_code_changed_after_request(self, cron_home):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        # A pending request whose pin was taken over DIFFERENT code than the
        # job now carries — the shape a post-request script rewrite produces.
        spec = _grant_script(cron_home, body="def run(ctx): return 'current'\n")
        other_spec = _grant_script(
            cron_home, body="def run(ctx): return 'requested'\n", name="other.py"
        )
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=compute_secret_env_pin(
                other_spec,
                "",
                "args",
                job_id=job.id,
                grant={"MY_TOKEN": "slack-sandbox"},
                domain="pending",
            ),
            secret_env_pending_ts=1.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{job.id}/secrets", json={"approve_pending": True}
                )
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "code_changed"
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None and reloaded.secret_env == {}

    @pytest.mark.asyncio
    async def test_deny_with_stale_expected_refuses(self, cron_home):
        """A denial restating request A must not clear a replacement B."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        jid = self._request_pending(svc, cron_home)
        job_spec = str(cron_home() / "crons" / "grantee.py") + ":run"
        svc.update_job(
            jid,
            secret_env_pending={"MY_TOKEN": "other-secret"},
            secret_env_pending_pin=compute_secret_env_pin(
                job_spec,
                "",
                "args",
                job_id=jid,
                grant={"MY_TOKEN": "other-secret"},
                domain="pending",
            ),
            secret_env_pending_ts=2.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "deny_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                    },
                )
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "stale_request"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env_pending == {"MY_TOKEN": "other-secret"}

    @pytest.mark.asyncio
    async def test_deny_clears_pending(self, cron_home):
        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json={"deny_pending": True})
        assert resp.status == 200
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env_pending == {}
        assert job.secret_env == {}


class TestGrantEndpointBoundaries:
    """Identity boundaries of the owner-only grant endpoint."""

    def _apps(self, *, internal_auth: bool):
        from types import SimpleNamespace

        from aiohttp import web

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.dashboard.handlers.cron import api_cron_secret_grant

        svc = CronService(base_dir=loader_config_dir())

        @web.middleware
        async def _mark_internal(request, handler):
            if internal_auth:
                request["internal_auth"] = True
            return await handler(request)

        state = SimpleNamespace(
            crons=svc,
            push_refresh=lambda *a, **k: None,
            _slots={"chat-1-abc": object()},
        )
        app = web.Application(middlewares=[_mark_internal])
        app["state"] = state
        app.router.add_put("/api/crons/{job_id}/secrets", api_cron_secret_grant)
        return app, svc, state

    def _pending_job(self, svc, cron_home) -> str:
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:chat-1-abc",
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=compute_secret_env_pin(
                spec,
                "",
                "args",
                job_id=job.id,
                grant={"MY_TOKEN": "slack-sandbox"},
                domain="pending",
            ),
            secret_env_pending_ts=1.0,
        )
        return job.id

    @pytest.fixture(autouse=True)
    def _owner_view(self, monkeypatch):
        """Default to owner view; the non-owner test overrides it."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    @pytest.mark.asyncio
    async def test_direct_grant_is_refused(self, cron_home):
        """Direct grants are removed: nothing binds what the owner saw to
        what gets pinned, so the request->approve flow is the only mint
        path. The endpoint keeps revoke and pending decisions only."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc, _state = self._apps(internal_auth=False)
        jid = self._pending_job(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={"secret_env": {"MY_TOKEN": "slack-sandbox"}},
                )
                body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "direct_grant_removed"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}

    @pytest.mark.asyncio
    async def test_replaced_pending_request_is_not_promoted_by_stale_approval(self, cron_home):
        """The agent can overwrite a pending request at any moment; an
        approval that restates request A must not promote a replacement B."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc, _state = self._apps(internal_auth=False)
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        SecretVault(loader_config_dir()).set_sync("jira-token", "fake-2")
        jid = self._pending_job(svc, cron_home)  # pending: MY_TOKEN <- slack-sandbox
        # Agent replaces the request AFTER the operator's view rendered.
        svc.update_job(
            jid,
            secret_env_pending={"MY_TOKEN": "jira-token"},
            secret_env_pending_pin=compute_secret_env_pin(
                str(cron_home() / "crons" / "grantee.py") + ":run",
                "",
                "args",
                job_id=jid,
                grant={"MY_TOKEN": "jira-token"},
                domain="pending",
            ),
            secret_env_pending_ts=2.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "approve_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                    },
                )
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "stale_request"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_token_cannot_grant(self, monkeypatch, cron_home):
        """A dashboard token minted for an allowed Slack user (not the owner)
        must not approve grants — the vault boundary is owner-only."""
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        app, svc, _state = self._apps(internal_auth=False)
        jid = self._pending_job(svc, cron_home)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(f"/api/crons/{jid}/secrets", json={"approve_pending": True})
        assert resp.status == 403
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}

    @pytest.mark.asyncio
    async def test_grant_endpoint_refuses_machine_credential(self, cron_home):
        """/api/crons is a prefix internal path, so the grant route IS
        reachable with X-Internal-Secret — the handler must refuse it."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc, _state = self._apps(internal_auth=True)
        jid = self._pending_job(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json={"approve_pending": True})
                body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "operator_only"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}
