"""Runtime decision: may a backend-only Pull+Build skip the frontend build?

A sync that changes nothing under ``website/`` pays the frontend build's whole
cost to reproduce a bundle already staged. This module answers whether that one
step -- ``npm run build`` plus the dist stage -- may be skipped.

It answers with a PROVENANCE RECORD, not with a comparison of artifacts. That
distinction is the whole design, and it is what the module exists to hold:

* **The property a safe skip needs is "the served bundle was produced by a
  completed frontend build from exactly the source that is on disk now".** That
  is a statement about HISTORY. No comparison of two artifacts can establish
  it, because the interesting failures leave the artifacts equal: ``npm run
  build`` is ``tsc -b && vite build`` and ``emptyOutDir`` lives inside vite, so
  a TypeScript error in freshly merged code means vite never runs and
  ``website/dist`` is left fully intact -- byte-identical to the staged copy it
  was staged from. Any artifact-to-artifact check reads that as "provenance
  holds" and skips, and the sync reports success having never built the merged
  frontend. So the record is produced by the sync itself, once, and only after
  the build step has actually SUCCEEDED: see :func:`build_record`.

* **This module cannot READ a record, only produce one and be handed one.**
  :func:`build_record` derives the pair; :func:`may_skip_frontend` takes it as
  two VALUES. There is deliberately no parameter that could carry a path,
  because a record that lives anywhere a sync step can write is a record a sync
  step can forge -- and the steps that run before the gated build execute code
  from the revision being landed (``pip install -e .`` runs its build backend,
  ``npm ci`` runs every dependency's lifecycle scripts) at the same uid as the
  process that reads it. Both fields are computable by that code, so a
  step-writable record lets it stage a bundle of its own choosing, vouch for it
  as this revision's, and have the build that would have overwritten it
  skipped. The record therefore lives in the deciding processes' own memory --
  the Dev Fleet backend's, and the runner's own script text -- and the type
  signature here is what keeps it there.

  READ THAT AS SCOPING THE RECORD, NOT THE BUNDLE. ``static/dist`` is same-uid
  writable, so a step that wants its own bundle served can simply write one and
  the gateway serves what is there, skip or no skip. What is bounded here is
  narrower: such a step cannot get that bundle VOUCHED for, so it cannot make a
  later sync skip the build that would replace it. The mechanism's durable value
  is the ACCIDENT case below -- artifacts that are equal after a failed build --
  rather than trustworthiness of what is on disk.

* **Identity, not a delta.** The record names the ``website/`` TREE OID
  (``git rev-parse HEAD:website``), which positively identifies the source the
  build read, rather than "nothing changed since some base commit". A tree OID
  cannot be vacuously satisfied the way a diff can, it needs no pre-merge base
  OID plumbed in from the caller, and because ``package-lock.json`` lives inside
  ``website/`` the same OID pins the dependency set too.

* **The build reads the WORKING TREE, and a tree OID only describes commits.**
  So both producing and honouring the record require ``website/`` to be clean
  including untracked files. Without that, an uncommitted ``website/src``
  edit -- or an untracked new component -- sits in a checkout whose committed
  tree still matches the record, and the skip serves a bundle that predates the
  edit while reporting success.

* **``npm ci`` is NOT skipped.** It stays unconditional. Whether the on-disk
  ``node_modules`` is the tree ``npm ci`` would produce cannot be verified below
  the cost of running it: npm's ``node_modules/.package-lock.json`` is metadata
  it writes once and nothing reconciles with the files it describes, so it stays
  byte-identical while a package is deleted from the tree, a file inside one is
  removed, or a file is truncated. npm's ``integrity`` hashes are over the
  published TARBALL, not the extracted tree, so nothing on disk lets them be
  re-derived either. ``npm ci`` is also the step that REPAIRS such a tree, and
  it is the cheap half of the frontend work, so leaving it unconditional costs
  little and keeps the build's dependency input correct by construction.

* **Skipping is CONSERVATIVE.** Every missing, unreadable, or unobtainable
  input answers "do not skip", and the sync builds exactly as it does without
  this module. A wrong skip serves a stale SPA behind a new backend and reports
  success, which is far more expensive than the build it saves.

Four residuals are deliberately accepted rather than papered over. Two are
impurities the record tolerates because it captures provenance instead of
asserting byte-equality: ``website/vite.config.ts`` stamps ``git rev-parse
--short HEAD`` into ``dist/sw.js``, so on a backend-only sync a rebuild would
emit a different service-worker version string than the staged one (harmless --
the service worker is network-first and caches only the offline shell); and a
Node or npm upgrade between the recorded build and the skip can change the
emitted bundle with an unchanged ``website/`` tree. Neither can serve stale
application code. The third is a race: :func:`build_record` fingerprints the
staged bundle after the build+stage step has exited, hence after
``frontend.build_and_stage`` released the staging lock, so a peer
``_stage_dist`` caller restaging inside that sub-second window is recorded as if
this sync had produced it.

The fourth is the one worth stating precisely, because it is where this design
stops and the reason it may. **Both git-derived halves of the verdict are
forgeable by an actor with write access to ``.git`` at our uid**, and no set of
config overrides closes that: an ``assume-unchanged`` bit in ``.git/index``
makes ``status`` report clean over arbitrary working-tree content, a line in
``.git/info/exclude`` hides a file from ``--untracked-files=all``, and a
``filter.<d>.clean`` driver runs while ``status`` hashes a stat-dirty file --
none of them a config key that :data:`_GIT_HARDENING` could name. So a checkout
that has been tampered with can be made to answer "the recorded tree, and
clean", and the build is skipped.

THAT BUYS THE ACTOR NOTHING, and that is what makes it a residual rather than
the hole it looks like. The reasoning is a capability comparison, not a hope:

* The skip cannot serve the actor's OWN bundle. ``staged_dist_digest`` is a live
  filesystem read compared against the recorded digest, so ``static/dist`` must
  be byte-identical to what the recorded build staged; substituting a bundle is a
  sha256 preimage, and staging one merely withholds the skip. The most a forged
  verdict achieves is that the last recorded build's own bundle stays served.
* The same actor already has strictly more WITHOUT the skip. Reaching ``.git``
  means code execution at our uid inside the sync -- an ``npm ci`` lifecycle
  script, or ``pip install -e .``'s build backend -- and the build step it would
  be suppressing is ``npm run build``, whose ``build`` script lives in the
  same-uid-writable ``website/package.json``, followed by a stage that copies
  the same-uid-writable ``website/dist``. So on the unconditional path that actor
  rewrites the build script to exit zero and hands its own bundle straight to
  ``static/dist``, under a sync that reports success. Unconditional building is
  not a defence against it; it is a wider version of the same exposure.

So the skip adds no capability, and the two are not comparable in the other
direction either. What separates this from the on-disk record this module
refuses (see above) is REACH: forging a live git read steers only the run the
actor is already inside, where it owns the build anyway, while a durable record
on disk would let it steer a LATER sync it is not part of. Reach beyond the
compromised run is the property worth engineering against, and the record's
absence from disk is what removes it.

This module imports ONLY the standard library. The sync runner is a stdlib-only
``python -c`` program that must not import ``kiro_crew`` (that would drag in the
package ``__init__`` chain, which imports croniter and the rest of the runtime),
so this helper is snapshotted at import and executed BY PATH the same way
``dep_sync`` and ``npm_preflight`` are -- see ``server._sync_start_locked``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess  # nosec B404 - reading git is this module's purpose
from pathlib import Path

#: The checkout subdirectory holding the frontend half, matching
#: :mod:`npm_preflight`'s ``_FRONTEND_SUBDIR``.
_FRONTEND_SUBDIR = "website"

#: The SERVED frontend bundle, relative to the repo root: the build+stage step's
#: whole job is to populate this directory (``<repo>/src/kiro_crew/static/dist``)
#: so the gateway can serve the SPA. On a packaged install it is a real directory
#: shipped in the wheel; on a source-tree run it is a symlink to
#: ``website/dist``. Either way ``frontend.py`` resolves the runtime bundle from
#: here and treats ``index.html`` as the marker of a usable dist -- see
#: ``frontend.ensure_dev_dist_symlink`` / ``_resolve_website_dist``. Because
#: ``Path`` reads follow symlinks, one probe on this path covers BOTH layouts.
_STATIC_DIST = os.path.join("src", "kiro_crew", "static", "dist")

#: The resolution marker ``frontend.py`` requires before it will serve a bundle;
#: an absent ``index.html`` is exactly what makes it fall back to the "not built"
#: guidance page, so its presence is what makes a staged tree usable at all.
_DIST_INDEX = "index.html"

#: Read size for fingerprinting the staged tree, so a single large asset (a font,
#: a source map) is hashed in bounded memory rather than read whole.
_HASH_CHUNK = 1 << 20

#: Environment forced onto every git spawn in this module, so the checkout's own
#: ``.git`` cannot turn one of these reads into an arbitrary program or point it
#: at a different object graph. ``.git`` is not trusted input: it is same-uid
#: writable, and the sync steps that ran before this reads anything executed code
#: from the revision being landed.
#:
#: What it closes, and each entry earns its place:
#:
#: * the config keys that name a COMMAND git runs. ``core.fsmonitor`` is the one
#:   that reaches THIS module: git invokes it while refreshing the index, so the
#:   cleanliness check below would become "run this program", INSIDE the record
#:   derivation and before the bundle is fingerprinted -- deterministic
#:   interposition rather than a race, and unobservable, because a non-zero exit
#:   from an ``fsmonitor`` program still leaves ``status`` reporting success.
#: * ``GIT_NO_REPLACE_OBJECTS``, which is not about code at all. A
#:   ``refs/replace/<oid>`` ref substitutes one object for another in every read,
#:   so ``rev-parse HEAD:website`` reports the SUBSTITUTE's tree -- a tree no
#:   checked-out commit names and the working tree does not hold. That breaks the
#:   identity :func:`website_tree_oid` exists to state, for a grafted checkout
#:   with nobody attacking anything, so it is pinned as correctness first.
#:
#: WHAT IT DOES NOT CLOSE, deliberately, because no set of config overrides can:
#: an actor who can write ``.git/config`` can also write ``.git/index`` (an
#: ``assume-unchanged`` bit makes ``status`` report clean over arbitrary
#: working-tree content) and ``.git/info/exclude`` (which hides a file from
#: ``--untracked-files=all``), and can define a ``filter.<d>.clean`` driver that
#: ``status`` runs when it must hash a stat-dirty file. None of those is a key
#: that could be added here, so enumerating more keys would buy nothing and imply
#: a completeness this set cannot have. Why that residual is acceptable -- the
#: actor it needs already owns ``website/package.json``'s ``build`` script, hence
#: already has the strictly stronger outcome -- is the fourth residual in the
#: module docstring. Read this as hardening the reads, NOT as making ``.git``
#: trusted.
#:
#: Spelled here rather than imported because this module must stay stdlib-only for
#: the sync runner (see the module docstring). It is the same set
#: ``runtime._GIT_ENV_NEUTRALIZERS`` applies to every other Dev Fleet git call,
#: and a test pins the two equal so the copy cannot drift from it.
_GIT_HARDENING: dict[str, str] = {
    "GIT_ALLOW_PROTOCOL": "https:ssh",
    "GIT_PROTOCOL_FROM_USER": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_CONFIG_COUNT": "4",
    "GIT_CONFIG_KEY_0": "core.fsmonitor",
    "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "core.hooksPath",
    "GIT_CONFIG_VALUE_1": "/dev/null",
    "GIT_CONFIG_KEY_2": "credential.helper",
    "GIT_CONFIG_VALUE_2": "",
    "GIT_CONFIG_KEY_3": "core.sshCommand",
    "GIT_CONFIG_VALUE_3": "ssh",
}

#: Width of every length and count field the staged-dist digest frames a value
#: with. Fixed-width and big-endian, so the field can never be mistaken for the
#: value that follows it, and eight bytes is past any reachable path length,
#: file size or entry count.
_FRAME_WIDTH = 8

#: The kind byte each staged-dist entry carries, distinct per entry SHAPE so the
#: byte stream stays uniquely decodable: a file entry is followed by its size and
#: body digest, a directory entry by nothing, and the kind is what says which
#: follows. Distinguishing a symlink from what it resolves to is what keeps
#: "the build's own file" and "a link to identical bytes" two different trees.
_ENTRY_FILE = b"F"
_ENTRY_FILE_LINK = b"L"
_ENTRY_DIR = b"D"
_ENTRY_DIR_LINK = b"S"


def _frame(digest: hashlib._Hash, value: bytes) -> None:
    """Feed one variable-length field to *digest*, its length first.

    A LENGTH PREFIX, never a separator. A separator only delimits values that
    cannot contain it, and the values here can: a file body is arbitrary bytes,
    so ``path SEP body SEP`` lets one file whose bytes spell out the framing of
    two collide with the pair -- ``{a: X, b: Y}`` and ``{a: X SEP b SEP Y}``
    serialize identically. A collision is the one error this digest must not
    make: it reads as "the recorded build's bundle is still staged" while an
    asset is gone, so the build is skipped and the missing asset 404s under a
    sync that reported success. With every variable-length field length-framed
    and every fixed-shape field fixed-width, the stream is uniquely decodable
    and the tree-to-digest mapping is injective.
    """
    digest.update(len(value).to_bytes(_FRAME_WIDTH, "big"))
    digest.update(value)


def _git_stdout(git: str, repo: str, args: list[str]) -> str | None:
    """Run ``<git> -C <repo> <args>`` and return stdout, or ``None`` on failure.

    The single spawn point of this module, so every git question it asks shares
    one timeout, one failure policy and one ENVIRONMENT: any failure at all (git
    missing, ref absent, path not in the revision, timeout, non-zero exit)
    collapses to ``None``, which every caller treats as "evidence unobtainable ->
    do not skip", and every spawn carries :data:`_GIT_HARDENING`, which keeps the
    checkout's own config from turning one of these reads into an arbitrary
    program or pointing it at a substituted object graph -- see that constant for
    what it does NOT close. Applied here rather than left to the caller's environment
    because the two callers do not share one: the sync runner is spawned with
    ``runtime._build_env()`` and inherits the same neutralizers, but the Dev Fleet
    backend derives the record IN PROCESS, where nothing has hardened
    ``os.environ``.
    """
    try:
        proc = subprocess.run(  # nosec B603 - argv list, no shell
            [git, "-C", repo, *args],
            capture_output=True,
            timeout=60,
            check=False,
            env={**os.environ, **_GIT_HARDENING},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def website_tree_oid(git: str, repo: str, rev: str) -> str | None:
    """The OID of the ``website/`` tree at *rev*, or ``None`` if unobtainable.

    This is the positive IDENTITY of the frontend source at a revision: two
    revisions share it exactly when their ``website/`` trees are byte-identical
    all the way down, which is a stronger and simpler statement than "the diff
    between them is empty". It also needs no second revision to compare
    against, which is what lets both the record and the decision name the one
    revision that matters -- ``HEAD``, the source a build reads -- instead of
    relating a base to a tip.

    ``package-lock.json`` lives inside ``website/``, so this OID pins the
    declared dependency set as well as the sources.

    Answered from the REAL object graph: :data:`_GIT_HARDENING` carries
    ``GIT_NO_REPLACE_OBJECTS``, without which a ``refs/replace/<oid>`` ref makes
    this report the substitute's tree -- an OID no checked-out commit names and
    the working tree does not hold, which is the one thing this function must
    never return.
    """
    out = _git_stdout(git, repo, ["rev-parse", f"{rev}:{_FRONTEND_SUBDIR}"])
    if out is None:
        return None
    oid = out.strip()
    # A tree OID is a hex digest. Anything else is not an answer to this
    # question -- refuse it rather than let it match another non-answer.
    if not oid or not all(c in "0123456789abcdef" for c in oid):
        return None
    return oid


def website_worktree_is_clean(git: str, repo: str) -> bool:
    """Is ``website/`` free of uncommitted AND untracked changes?

    Required both to write the stamp and to honour it, because the two things
    the stamp relates are of different kinds: a tree OID describes a COMMIT,
    while ``npm run build`` reads the WORKING TREE. A ``git merge --ff-only``
    succeeds over a dirty ``website/`` it does not touch, so without this the
    committed tree can match the stamp while the source on disk does not --
    and an uncommitted ``website/src`` edit, or an untracked new component, is
    then never built while the sync reports success.

    ``--untracked-files=all`` is what covers the untracked case; a new file is
    exactly as invisible to a tree OID as an edited one. This does not
    suppress the skip on a normally-built checkout: ``website/.gitignore``
    already covers ``node_modules`` and ``dist``, so a fully built tree still
    reports clean.

    ``False`` on any dirt AND on any failure to ask -- unobtainable evidence is
    weak evidence, and both mean "build".

    This answer is only as honest as the checkout's own ``.git``, which an actor
    at our uid can make lie (an ``assume-unchanged`` bit, ``.git/info/exclude``, a
    ``clean`` filter) and which no environment hardening can prevent. That is the
    fourth residual in the module docstring, along with why an actor who can do it
    already has strictly more than the skip could give it.
    """
    out = _git_stdout(
        git, repo, ["status", "--porcelain", "--untracked-files=all", "--", _FRONTEND_SUBDIR]
    )
    if out is None:
        return False
    return not out.strip()


def staged_dist_digest(repo: str) -> str | None:
    """Fingerprint EVERY entry of the served bundle, or ``None`` if none is usable.

    Two questions, one walk:

      * PRESENCE -- the build+stage step's job is to populate ``static/dist``,
        and running it is what repairs an absent one. ``None`` here (no
        directory, no ``index.html``, anything in the tree that cannot be read)
        means there is nothing trustworthy to serve, so the build must run.
      * IDENTITY -- the digest covers each entry's path, KIND and bytes, so it
        changes on any restage, any missing or truncated chunk, and any added
        file. Recording it and re-checking it at skip time is what makes an
        out-of-band restage, or a bundle damaged since the recorded build,
        withhold the skip instead of being silently inherited by it.

    The serialization is INJECTIVE, not merely separated: no two distinct trees
    can produce one digest, because every variable-length field carries its own
    fixed-width length and every entry its kind -- see :func:`_frame` for the
    collision a separator admits and what it would cost here. What is framed is
    what a static file server serves: each entry's path within the bundle,
    whether it is a file, a directory or a symlink, and every byte of every
    file, plus the entry count. Metadata that cannot change a served byte (mode,
    mtime, owner) is deliberately out, so a permission bit does not force a
    rebuild; directories are in even though they serve nothing themselves, which
    is what makes an added, removed or newly-symlinked directory visible.

    Hashing the whole tree rather than ``index.html`` alone is what covers the
    divergences the marker file cannot see, and they are reachable rather than
    theoretical. Vite's ``emptyOutDir`` deletes the previous bundle in directory
    order before writing the new one, so a build+stage step killed mid-empty (the
    run watchdog's timeout kill, or gateway shutdown reaping the tree) can leave
    ``index.html`` still byte-identical to the recorded one while the chunks it
    references are already gone. That step exits non-zero, so it never records a
    new stamp -- the OLD stamp stands, and on a backend-only retry the tree OID
    and the cleanliness gate both still pass. An index-only digest would skip
    there and serve a shell whose every asset 404s. The same walk covers the
    public assets vite copies verbatim under stable unhashed names
    (``/vendor/*.mjs``, icons, ``sw.js``), which no digest of ``index.html``
    describes.

    ``os.walk`` follows the top path, so this covers both the packaged real
    directory and the source-tree symlink to ``website/dist``, and it is ordered
    (directories and files sorted) so the digest is a function of the tree rather
    than of readdir order. It does NOT follow links below that path, so a
    symlinked subdirectory is framed as one link entry and never descended into;
    a dangling link is not a directory, so it lands among the files and fails to
    open. ``onerror`` re-raises rather than letting the walk silently skip an
    unreadable subtree, which would fingerprint a partial tree and could match.

    The digest is a value of this function alone, held only in memory (see the
    module docstring), and both ends of every comparison come from one capture of
    this file: the backend derives the record with the module it imported, and the
    runner checks it against a snapshot of those same bytes. So changing what this
    frames needs no version tag to be safe -- there is no stored digest anywhere,
    and no way for two framings to meet. It simply makes each record the backend
    holds stop matching, which costs that checkout one frontend build.
    """
    root = Path(repo) / _STATIC_DIST
    if not (root / _DIST_INDEX).is_file():
        return None

    def _reraise(exc: OSError) -> None:
        raise exc

    digest = hashlib.sha256()
    entries = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, onerror=_reraise):
            dirnames.sort()
            for name in dirnames:
                path = Path(dirpath) / name
                _frame(digest, path.relative_to(root).as_posix().encode("utf-8"))
                digest.update(_ENTRY_DIR_LINK if path.is_symlink() else _ENTRY_DIR)
                entries += 1
            for name in sorted(filenames):
                path = Path(dirpath) / name
                _frame(digest, path.relative_to(root).as_posix().encode("utf-8"))
                digest.update(_ENTRY_FILE_LINK if path.is_symlink() else _ENTRY_FILE)
                # The body goes in as its OWN digest, a fixed 32 bytes, and its
                # length is the bytes actually read rather than a stat's answer:
                # a fixed-width field needs no delimiter at all, and counting
                # what was hashed keeps the length honest about a file truncated
                # under the read.
                body = hashlib.sha256()
                size = 0
                with open(path, "rb") as fh:
                    while chunk := fh.read(_HASH_CHUNK):
                        body.update(chunk)
                        size += len(chunk)
                digest.update(size.to_bytes(_FRAME_WIDTH, "big"))
                digest.update(body.digest())
                entries += 1
    except OSError:
        return None
    # The count closes the sequence the way each length closes a field: it is
    # what a truncated walk cannot fake.
    digest.update(entries.to_bytes(_FRAME_WIDTH, "big"))
    return digest.hexdigest()


def build_record(git: str, repo: str) -> tuple[str, str] | None:
    """The ``(website tree OID, staged bundle digest)`` describing what is on disk.

    Derived by the Dev Fleet backend only after a sync run has exited zero. That
    ordering is the entire safety argument: a build that failed -- including the
    dominant ``tsc -b`` failure, which leaves ``website/dist`` fully intact and
    therefore indistinguishable from a good build by any artifact comparison --
    never reaches this, so the backend keeps holding the older pair (or none) and
    the next sync rebuilds.

    ``None`` -- meaning "no record, so the next sync builds" -- whenever the pair
    cannot honestly be formed: a dirty ``website/`` (the build read a working tree
    no commit describes, so no tree OID identifies what it produced), an
    unresolvable tree OID, or a bundle that cannot be read. The caller REPLACES
    whatever it held with this answer, so a build this function declines to vouch
    for cannot leave an earlier pair standing as if it described the bundle now on
    disk.
    """
    if not website_worktree_is_clean(git, repo):
        return None
    tree = website_tree_oid(git, repo, "HEAD")
    if tree is None:
        return None
    dist = staged_dist_digest(repo)
    if dist is None:
        return None
    return tree, dist


def may_skip_frontend(git: str, repo: str, recorded_tree: str, recorded_dist: str) -> bool:
    """The one decision the runner consults: skip the frontend build+stage step?

    ``recorded_tree`` and ``recorded_dist`` are a :func:`build_record` pair, passed
    as VALUES. There is no parameter that could name a file, and that is the
    invariant this signature exists to enforce rather than merely document: every
    other input to the verdict is recomputed live from git and the filesystem
    here, so the pair is the only historical CLAIM involved -- and a claim on disk
    is a claim any same-uid sync step can rewrite. See the module docstring.

    ``True`` only when all four hold, which together say "the bundle on disk was
    produced by a completed build from exactly the source this sync is landing":

      * ``HEAD``'s ``website/`` tree OID is obtainable, AND
      * the record from a COMPLETED earlier build names that same tree OID, AND
      * every file of the bundle currently staged is still the one that build
        staged, AND
      * ``website/`` on disk is clean, so the committed tree the OID names is
        also the source a build would read now.

    The revision asked about is ``HEAD`` -- the same one :func:`build_record`
    describes, and never the ref the sync is merging. The runner reaches this only
    after the merge step, so HEAD IS the source the build would read, while the
    ref equals it only when the merge actually fast-forwarded. ``git merge
    --ff-only <ref>`` ALSO exits zero, reporting "Already up to date", whenever
    the ref is an ancestor of HEAD -- which is what a checkout carrying local
    commits looks like. Asking about the ref there answers about a source no build
    will read, and answers ``True`` for a committed ``website/`` change that then
    never gets built. Asking about HEAD cannot fail that way: after a successful
    ``merge --ff-only`` HEAD is either the ref (the same answer) or ahead of it
    (correctly refused).

    Everything else -- a ``website/`` change (a different tree OID), an empty
    record, a failed or interrupted earlier build (which produced none), an
    out-of-band restage, a staged tree damaged since the record, a dirty or
    untracked-carrying ``website/``, an absent ``static/dist``, or git being
    unavailable to answer any of it -- returns ``False``, and the sync builds as
    it does without this module.

    ``npm ci`` is not part of this verdict: it is unconditional. See the module
    docstring for why its skip has no safe formulation.
    """
    if not recorded_tree or not recorded_dist:
        return False
    incoming = website_tree_oid(git, repo, "HEAD")
    if incoming is None or incoming != recorded_tree:
        return False
    if staged_dist_digest(repo) != recorded_dist:
        return False
    return website_worktree_is_clean(git, repo)
