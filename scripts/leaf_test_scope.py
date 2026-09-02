#!/usr/bin/env python3
"""Run only the test files a test-only PR touched, repeated, instead of the suite.

Why this reduction is sound when the general one is not
------------------------------------------------------
`run_scoped_tests.py` deliberately refuses to narrow WITHIN a surface, and its
docstring says why: answering "which tests reach this changed module?" needs a
real import graph, and six review rounds proved a text scan cannot enumerate the
ways a test can reach a module.

This script answers the OPPOSITE question, which is decidable by a text scan:

    the diff changes only test files -> can those test files affect any OTHER
    test file?

A test module that nothing imports is a leaf: pytest collects it, and no other
file's outcome depends on its contents. "Does anything import this module?" is a
reverse lookup over a closed set of import statements, not an open-ended guess
about reachability. Measured on this checkout: 1,886 backend `test_*.py` files,
of which 21 are imported by another file and 1,865 are leaves.

What a test-only diff can still break, and how each is closed
-------------------------------------------------------------
* Another test IMPORTS the changed module (`test_chat_backfill` imports
  `test_chat_slack`; `test_connections_handoff` imports autouse fixtures from
  `test_connections_warm`). Closed by the reverse-import scan -- any changed file
  that some other file imports escalates to the full suite.
* The change is not really a leaf test: `conftest.py`, a shared helper
  (`source_corpus.py`, `spawn_test_helpers.py`), a fixture tree, a data file
  (`windows-expected-failures.txt`). Closed by requiring every changed path to be
  `test/test_*.py`, plus `run_scoped_tests.has_broad_impact`.
* A CORPUS GATE reads the `test/` tree as DATA and asserts on its contents, so
  editing any test file can flip a gate that names none of them --
  `test_coverage_omit_contract` scans `REPO_ROOT / "test"`,
  `test_jsondecodeerror_redundancy_ratchet` has it in `SCAN_ROOTS`, and
  `test_ci_surface_tests` asserts every name in `windows-collect-ignore.txt`
  still exists (so a DELETION breaks it). Closed by always appending those gates
  to the run. They are DERIVED by scanning for the reference, not hardcoded, so a
  new corpus gate joins the set the day it is written rather than being missed;
  the self-test fails if the derivation returns nothing, because a dead scan and
  a working one look identical.

Repeats, not a single pass
--------------------------
A flaky test run once in isolation usually passes, so a single green here would
prove nothing about the fix it is gating. The target set is run REPEAT times in
SEPARATE pytest processes (this repo has no pytest-repeat, and separate processes
are stronger anyway: they re-roll per-process state, load order and worker
assignment instead of reusing one warmed interpreter).

This does NOT reproduce whole-suite conditions -- a flake that needs a specific
xdist worker neighbour cannot appear in a 4-file run. That residual is the price
of the reduction, and it is bounded to test outcomes: the leaf check means no
PRODUCTION code path changed. Force the full matrix with the `ci-full-run` label
when a fix depends on suite-wide interleaving.

Usage
-----
    SCOPED_TESTS_BASE_REF="$(git merge-base HEAD origin/main)" \\
        python3 scripts/leaf_test_scope.py --plan
    ... --targets     # bare newline-separated list for CI
    ... --run         # execute the narrow set, repeated
    ... --repeat 5    # override the repeat count (default 3)
    ... --files test/test_a.py    # decide for an explicit list
    ... --test        # self-test

Exit codes: 0 eligible / run green, 1 tests failed, 2 usage or environment error,
3 NOT eligible -- the caller must run the full suite.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The selector's stdout becomes argv for pytest, so it is validated with the SAME
# helper `run_scoped_tests.py` uses rather than a second copy of the rule -- two
# spellings of one admission check drift, and this one would drift silently.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from run_scoped_tests import (  # noqa: E402  (path set immediately above)
    SelectionUntrustworthy,
    changed_files,
    has_broad_impact,
    resolve_base,
    validated_targets,
)

NOT_ELIGIBLE = 3
DEFAULT_REPEAT = 3

# Only files matching this, directly under `test/`, can be leaves. Anything else
# in the tree -- conftest.py, a helper module, fixtures/, a .txt corpus, the
# workflows/ and metrics/ subtrees -- is shared input to other files' outcomes.
_LEAF_NAME = re.compile(r"^test_[A-Za-z0-9_]+\.py$")

# `from <stem> import ...` / `import <stem>`, at any indentation: several of this
# repo's cross-test imports sit inside a function body (`test_chat_fork_error_codes`
# does `import test_error_code_contract as gate` inside the test), so anchoring to
# column zero would call a non-leaf a leaf.
_IMPORT = re.compile(
    r"^[ \t]*(?:from[ \t]+([A-Za-z_][\w.]*)[ \t]+import|import[ \t]+([A-Za-z_][\w.]*))",
    re.M,
)

# A test that treats the `test/` tree as DATA. Both spellings of the join appear
# in this repo (`REPO_ROOT / "test"`, `ROOT / "test"`, `_REPO_ROOT / "test"`), and
# the quote style varies, so match the join itself rather than any one prefix.
_SCANS_TEST_TREE = re.compile(r"""/[ \t]*['"]test['"]""")

_PY_SCAN_ROOTS = ("test", "scripts", "src")
_PY_SCAN_EXCLUDE = ("_vendor", "build", "node_modules", "__pycache__", ".venv")


def _rel_posix(path: Path, root: Path) -> str:
    """Repo-relative path with FORWARD slashes on every platform.

    `str(Path.relative_to(...))` yields `test\\test_x.py` on Windows, and that
    breaks this selector in two places at once: `run_scoped_tests._SAFE_TARGET`
    does not admit a backslash, so EVERY target is refused as "not a plain
    relative path", and the `test/` prefix rules in `classify` stop matching. Git
    reports paths with forward slashes on all platforms, so POSIX form is also
    what the diff side of the comparison already speaks.
    """
    return path.relative_to(root).as_posix()


def _iter_python(root: Path) -> list[Path]:
    out: list[Path] = []
    for base in _PY_SCAN_ROOTS:
        start = root / base
        if not start.is_dir():
            continue
        for path in start.rglob("*.py"):
            if any(part in _PY_SCAN_EXCLUDE for part in path.parts):
                continue
            out.append(path)
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Unreadable input cannot be cleared, and a reduction that silently
        # skipped a file it could not read would be exactly the wrong failure.
        raise SelectionUntrustworthy(f"cannot read {path} while classifying the diff") from None


def importers_of(stems: set[str], root: Path) -> dict[str, str]:
    """Map each stem in ``stems`` that another file imports to that importer.

    The reverse direction of the question `run_scoped_tests` refuses: this asks
    who names THIS module, over the finite set of import statements in the tree,
    rather than which modules a test can transitively reach.
    """
    if not stems:
        return {}
    found: dict[str, str] = {}
    for path in _iter_python(root):
        text = _read(path)
        for match in _IMPORT.finditer(text):
            module = (match.group(1) or match.group(2) or "").split(".")[0]
            if module in stems and module != path.stem:
                found.setdefault(module, _rel_posix(path, root))
    return found


def corpus_gates(root: Path) -> list[str]:
    """Test files that read the ``test/`` tree as data, so ANY test edit can flip them.

    Derived, never hardcoded. Over-inclusion is cheap (a dozen files against
    1,886) and fails safe; a missed one silently skips a gate the diff really can
    break, which is the whole failure mode this reduction has to avoid.
    """
    gates: list[str] = []
    test_dir = root / "test"
    if not test_dir.is_dir():
        return gates
    for path in sorted(test_dir.glob("test_*.py")):
        if _SCANS_TEST_TREE.search(_read(path)):
            gates.append(_rel_posix(path, root))
    return gates


def classify(paths: list[str], root: Path = REPO_ROOT) -> tuple[list[str] | None, str]:
    """Return (targets, reason). ``None`` targets means: run the full suite."""
    if not paths:
        return None, "full suite: diff is empty against the base"

    broad = has_broad_impact(paths)
    if broad:
        return None, f"full suite: broad-impact change {broad}"

    outside = [p for p in paths if not p.startswith("test/")]
    if outside:
        return None, (
            f"full suite: the diff is not test-only ({len(outside)} path(s) outside "
            f"test/, e.g. {outside[0]})"
        )

    non_leaf_shape = [p for p in paths if not _LEAF_NAME.match(p[len("test/") :])]
    if non_leaf_shape:
        return None, (
            f"full suite: {non_leaf_shape[0]} is shared test input (a helper, fixture, "
            "conftest or data file), not a leaf test module, so other files' outcomes "
            "depend on it"
        )

    stems = {Path(p).stem for p in paths}
    imported = importers_of(stems, root)
    if imported:
        stem, importer = sorted(imported.items())[0]
        return None, (
            f"full suite: test/{stem}.py is imported by {importer}, so it is shared "
            "input rather than a leaf (this repo has 21 such files, several exporting "
            "autouse fixtures)"
        )

    # A deleted or renamed-away path cannot be run. It stays eligible -- the
    # corpus gates below are exactly what catches a deletion that breaks a
    # tree-wide assertion -- but it must not reach pytest as a missing target.
    live = [p for p in paths if (root / p).is_file()]
    gates = corpus_gates(root)
    if not gates:
        return None, (
            "full suite: the corpus-gate derivation found no test that scans the "
            "test/ tree, which cannot be true in this repo -- treating the scan as "
            "broken rather than trusting an empty result"
        )

    targets = sorted(set(live) | set(gates))
    if not targets:
        return None, "full suite: nothing runnable resolved from the diff"
    deleted = len(paths) - len(live)
    note = f", {deleted} deleted path(s) not run" if deleted else ""
    return targets, (
        f"leaf tests: {len(live)} changed leaf test file(s) + {len(gates)} corpus "
        f"gate(s) that read the test/ tree{note}"
    )


def plan(base: str) -> tuple[list[str] | None, str]:
    base_sha = resolve_base(base)
    try:
        paths = changed_files(base_sha)
    except SelectionUntrustworthy as exc:
        return None, f"full suite: {exc}"
    return classify(paths)


def pytest_argv(targets: list[str]) -> list[str]:
    """Match CI's reduced lane: no coverage (a subset's number is not comparable).

    `--` ends option parsing so nothing after it can be read as a flag, and
    `validated_targets` is the real protection either way.
    """
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-cov",
        "--",
        *validated_targets(targets, REPO_ROOT),
    ]


def run(targets: list[str], repeat: int) -> int:
    argv = pytest_argv(targets)
    for attempt in range(1, repeat + 1):
        print(f"leaf_test_scope: pass {attempt}/{repeat}: $ {' '.join(argv)}", flush=True)
        # argv is always a list and shell=True is never used, so there is no shell
        # to inject into; the argument-injection risk is closed by
        # validated_targets(). Same rule and reasoning as run_scoped_tests.py:424.
        rc = subprocess.run(
            argv, cwd=str(REPO_ROOT), check=False
        ).returncode  # noqa: E501  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
        if rc != 0:
            print(
                f"leaf_test_scope: FAILED on pass {attempt}/{repeat} (rc={rc}). A flake "
                "that only fails sometimes is still failing -- do not re-run to green.",
                file=sys.stderr,
            )
            return 1
    print(f"leaf_test_scope: {repeat} consecutive passes over {len(targets)} file(s).")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print the verdict and targets")
    mode.add_argument("--targets", action="store_true", help="print targets only, one per line")
    mode.add_argument("--run", action="store_true", help="run the narrow set, repeated")
    mode.add_argument("--test", action="store_true", help="run this script's self-test")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT)
    parser.add_argument("--files", nargs="*", help="classify this list instead of the git diff")
    args = parser.parse_args(argv)

    if args.test:
        return _self_test()
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")

    try:
        if args.files is not None:
            targets, reason = classify(sorted(args.files))
        else:
            targets, reason = plan(os.environ.get("SCOPED_TESTS_BASE_REF", ""))
    except ValueError as exc:
        print(f"leaf_test_scope: {exc}", file=sys.stderr)
        return 2
    except SelectionUntrustworthy as exc:
        print(f"leaf_test_scope: full suite: {exc}", file=sys.stderr)
        return NOT_ELIGIBLE

    if targets is None:
        if not args.targets:
            print(f"leaf_test_scope: {reason}")
        return NOT_ELIGIBLE

    if args.targets:
        print("\n".join(targets))
        return 0

    print(f"leaf_test_scope: {reason}")
    for target in targets:
        print(f"  - {target}")
    if args.run:
        try:
            return run(targets, args.repeat)
        except SelectionUntrustworthy as exc:
            print(f"leaf_test_scope: refusing to run: {exc}", file=sys.stderr)
            return NOT_ELIGIBLE
    return 0


def _self_test() -> int:
    """Prove every escalation fires. A reducer trusted without these is a guess."""
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    def verdict(paths: list[str]) -> list[str] | None:
        return classify(sorted(paths))[0]

    # The derivations must resolve against the real tree. An empty result and a
    # broken scan are indistinguishable, which is how a dead path survived four
    # review rounds in run_scoped_tests.py.
    gates = corpus_gates(REPO_ROOT)
    check("corpus-gate derivation is non-empty", bool(gates))
    for known in (
        "test/test_coverage_omit_contract.py",
        "test/test_jsondecodeerror_redundancy_ratchet.py",
        "test/test_ci_surface_tests.py",
    ):
        check(f"corpus gate derived: {known}", known in gates)
        check(f"corpus gate exists on disk: {known}", (REPO_ROOT / known).is_file())

    # The reverse-import scan must find this repo's real cross-test imports.
    known_non_leaf = {
        "test_chat_slack": "test_chat_backfill imports _make_slack_app",
        "test_connections_warm": "test_connections_handoff imports autouse fixtures",
        "test_error_code_contract": "imported inside a function body",
    }
    found = importers_of(set(known_non_leaf), REPO_ROOT)
    for stem, why in known_non_leaf.items():
        check(f"reverse-import finds {stem} ({why})", stem in found)
    check(
        "reverse-import does not flag a real leaf",
        "test_leaf_test_scope" not in importers_of({"test_leaf_test_scope"}, REPO_ROOT),
    )

    # Escalations.
    check("empty diff escalates", verdict([]) is None)
    check("source change escalates", verdict(["src/kiro_crew/session.py"]) is None)
    check("conftest escalates", verdict(["test/conftest.py"]) is None)
    check("shared helper escalates", verdict(["test/source_corpus.py"]) is None)
    check("fixture tree escalates", verdict(["test/fixtures/npm-audit/x.json"]) is None)
    check("data corpus escalates", verdict(["test/windows-expected-failures.txt"]) is None)
    check("nested subtree escalates", verdict(["test/workflows/x.py"]) is None)
    check("imported test escalates", verdict(["test/test_chat_slack.py"]) is None)
    check(
        "mixed leaf + source escalates",
        verdict(["test/test_ask_question_roundtrip.py", "src/kiro_crew/agent.py"]) is None,
    )
    check("workflow change escalates", verdict([".github/workflows/ci.yml"]) is None)

    # The one accepted case, and the shape of what it runs.
    leaf = "test/test_leaf_test_scope.py"
    accepted = verdict([leaf])
    check("a leaf test file is accepted", accepted is not None)
    if accepted is not None:
        check("accepted set carries the changed file", leaf in accepted)
        check("accepted set carries the corpus gates", set(gates).issubset(set(accepted)))
        check(
            "accepted set is a real reduction",
            len(accepted) < len(list((REPO_ROOT / "test").glob("test_*.py"))) / 10,
        )

    # A deletion stays eligible (the corpus gates are what catch it) but is not
    # handed to pytest as a target that no longer exists.
    ghost = "test/test_definitely_deleted_zzz.py"
    deleted = verdict([ghost])
    check("a deleted leaf stays eligible", deleted is not None)
    if deleted is not None:
        check("a deleted leaf is not a pytest target", ghost not in deleted)

    # Targets must survive the shared admission check.
    if accepted is not None:
        try:
            validated_targets(accepted, REPO_ROOT)
        except SelectionUntrustworthy as exc:
            failures.append(f"validated_targets rejected the accepted set: {exc}")

    for name in failures:
        print(f"leaf_test_scope self-test FAILED: {name}", file=sys.stderr)
    if failures:
        return 1
    print("leaf_test_scope self-test: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
