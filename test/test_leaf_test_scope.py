"""Pin the leaf-test reduction: every escalation fires, and CI calls the script.

The reduction this guards replaces the sharded backend suite with a handful of
files for a diff that touches only leaf test modules. That is only safe while
each escalation below holds, so each one is asserted directly rather than trusted
from the script's own self-test -- and the self-test is additionally executed
here, so `--test` rotting is a test failure rather than a silent no-op.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT.joinpath("scripts", "leaf_test_scope.py")
TEST_DIR = REPO_ROOT.joinpath("test")

sys.path.insert(0, str(REPO_ROOT.joinpath("scripts")))

import leaf_test_scope as mod  # noqa: E402  (path set immediately above)


def _targets(paths: list[str]) -> list[str] | None:
    return mod.classify(sorted(paths))[0]


def _reason(paths: list[str]) -> str:
    return mod.classify(sorted(paths))[1]


def test_script_exists_and_self_test_passes() -> None:
    assert SCRIPT.is_file(), "scripts/leaf_test_scope.py is missing"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--test"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, (
        f"leaf_test_scope.py --test failed (rc={proc.returncode}):\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


# ── the reduction is only offered for a genuinely test-only diff ───────────────


@pytest.mark.parametrize(
    "path",
    [
        "src/kiro_crew/session.py",
        "src/kiro_crew/dashboard/server.py",
        "docs/architecture/overview.md",
        "README.md",
        "setup.cfg",
        "website/src/App.tsx",
    ],
)
def test_any_non_test_path_escalates(path: str) -> None:
    assert _targets([path]) is None, f"{path} must not qualify for the leaf reduction"


def test_a_leaf_test_change_mixed_with_source_escalates() -> None:
    """The whole diff must be test-only; one source file forfeits the reduction."""
    assert _targets([__rel(__file__), "src/kiro_crew/agent.py"]) is None


# ── shared test input is never a leaf ─────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "test/conftest.py",
        "test/source_corpus.py",
        "test/spawn_test_helpers.py",
        "test/chat_test_helpers.py",
        "test/tmpdir_helpers.py",
        "test/windows-expected-failures.txt",
        "test/windows-collect-ignore.txt",
        "test/fixtures/npm-audit/report.json",
        "test/workflows/some_helper.py",
    ],
)
def test_shared_test_input_escalates(path: str) -> None:
    assert _targets([path]) is None, f"{path} is shared input and must escalate"


def test_every_named_shared_helper_still_exists() -> None:
    """A rule pinned against a path that no longer exists protects nothing."""
    for name in ("conftest.py", "source_corpus.py", "spawn_test_helpers.py"):
        assert TEST_DIR.joinpath(name).is_file(), f"test/{name} vanished; update this gate"


# ── a test module another file imports is shared input, not a leaf ────────────


def test_imported_test_modules_escalate() -> None:
    """These are real cross-test imports in this repo, not hypotheticals."""
    for stem in ("test_chat_slack", "test_connections_warm", "test_error_code_contract"):
        path = f"test/{stem}.py"
        assert TEST_DIR.joinpath(f"{stem}.py").is_file(), f"{path} vanished; update this gate"
        assert _targets([path]) is None, f"{path} is imported elsewhere and must escalate"


def test_reverse_import_scan_finds_a_function_body_import() -> None:
    """`test_chat_fork_error_codes` imports its gate INSIDE a test body.

    Anchoring the import pattern to column zero would call that module a leaf.
    """
    found = mod.importers_of({"test_error_code_contract"}, REPO_ROOT)
    assert "test_error_code_contract" in found


def test_reverse_import_scan_leaves_a_real_leaf_alone() -> None:
    assert mod.importers_of({"test_leaf_test_scope"}, REPO_ROOT) == {}


# ── corpus gates: tests that read the test/ tree as data ──────────────────────


def test_corpus_gate_derivation_is_non_empty() -> None:
    """An empty derivation and a broken scan look identical; fail on empty."""
    assert mod.corpus_gates(REPO_ROOT), "no corpus gates derived, which cannot be true here"


@pytest.mark.parametrize(
    "gate",
    [
        "test/test_coverage_omit_contract.py",
        "test/test_jsondecodeerror_redundancy_ratchet.py",
        "test/test_ci_surface_tests.py",
    ],
)
def test_known_corpus_gates_are_derived(gate: str) -> None:
    """These assert on the whole test/ tree, so ANY test edit can flip them."""
    assert REPO_ROOT.joinpath(gate).is_file(), f"{gate} vanished; update this gate"
    assert gate in mod.corpus_gates(REPO_ROOT)


def test_accepted_run_always_includes_the_corpus_gates() -> None:
    targets = _targets([__rel(__file__)])
    assert targets is not None
    assert set(mod.corpus_gates(REPO_ROOT)).issubset(set(targets))


def test_a_broken_corpus_scan_forfeits_the_reduction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation guard: if the derivation returns nothing, escalate, never reduce."""
    monkeypatch.setattr(mod, "corpus_gates", lambda root: [])
    assert _targets([__rel(__file__)]) is None


# ── the accepted case ─────────────────────────────────────────────────────────


def test_a_leaf_test_file_is_accepted_and_is_a_real_reduction() -> None:
    targets = _targets([__rel(__file__)])
    assert targets is not None
    assert __rel(__file__) in targets
    total = len(list(TEST_DIR.glob("test_*.py")))
    assert (
        len(targets) < total / 10
    ), f"{len(targets)} targets against {total} test files is not a useful reduction"


def test_a_deleted_leaf_is_eligible_but_not_handed_to_pytest() -> None:
    """A deletion can break a corpus gate, so stay eligible and run those gates.

    The vanished path itself must not reach pytest, which would fail on a missing
    file rather than on anything the diff did.
    """
    ghost = "test/test_definitely_deleted_zzz.py"
    assert not REPO_ROOT.joinpath(ghost).exists()
    targets = _targets([ghost])
    assert targets is not None
    assert ghost not in targets
    assert set(mod.corpus_gates(REPO_ROOT)).issubset(set(targets))


def test_empty_diff_escalates() -> None:
    assert _targets([]) is None
    assert "empty" in _reason([])


def test_accepted_targets_pass_the_shared_admission_check() -> None:
    """Targets become pytest argv, so they go through run_scoped_tests' validator."""
    from run_scoped_tests import validated_targets

    targets = _targets([__rel(__file__)])
    assert targets is not None
    assert validated_targets(targets, REPO_ROOT) == targets


# ── path form: the Windows shard caught this, so pin it on every platform ─────


def test_rel_posix_normalises_a_windows_style_path() -> None:
    """The only Linux-detectable form of the bug that broke the Windows shard.

    `str(PureWindowsPath(...).relative_to(...))` yields backslashes, which
    `validated_targets` refuses outright. Asserting on real `Path` objects would
    pass on Linux either way, so drive the helper with an explicitly Windows path.
    """
    win_root = PureWindowsPath(r"D:\a\KiroCrew\KiroCrew")
    win_file = win_root / "test" / "test_ai_agent_runner_coverage.py"
    assert str(win_file.relative_to(win_root)) == r"test\test_ai_agent_runner_coverage.py"
    assert mod._rel_posix(win_file, win_root) == "test/test_ai_agent_runner_coverage.py"


def test_no_derived_path_carries_a_backslash() -> None:
    """Vacuous on POSIX by construction; it is the Windows shard this guards."""
    for gate in mod.corpus_gates(REPO_ROOT):
        assert "\\" not in gate, f"corpus gate is not POSIX-form: {gate!r}"
    for importer in mod.importers_of({"test_chat_slack"}, REPO_ROOT).values():
        assert "\\" not in importer, f"importer path is not POSIX-form: {importer!r}"


def test_repeat_is_more_than_one_by_default() -> None:
    """A single isolated pass proves nothing about the flake it gates."""
    assert mod.DEFAULT_REPEAT >= 2


def test_run_argv_disables_coverage_and_ends_option_parsing() -> None:
    argv = mod.pytest_argv([__rel(__file__)])
    assert "--no-cov" in argv, "a subset's coverage is not comparable to the repo floor"
    assert "--" in argv, "option parsing must end before selector-provided paths"
    assert argv.index("--") < argv.index(__rel(__file__))


# ── CI must actually use it ───────────────────────────────────────────────────


def test_ci_workflow_invokes_the_selector() -> None:
    """A selector nothing calls is a dead path that looks exactly like a live one."""
    ci = REPO_ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")
    assert "leaf_test_scope.py" in ci, "ci.yml does not call the leaf-test selector"


def test_ci_keeps_an_escape_hatch_to_the_full_matrix() -> None:
    ci = REPO_ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")
    assert "ci-full-run" in ci, "the full-matrix escape hatch must remain available"


def __rel(path: str) -> str:
    """POSIX form, like the selector: `str()` gives backslashes on Windows."""
    return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
