"""Provider-neutral plumbing shared by Kiro Crew's *direct* ACP adapters.

A "direct" adapter is the exception to Kiro Crew's usual ACP shape. Every
shipped harness (Claude Code, Codex, Goose, ...) is a third-party binary the
gateway spawns and then speaks ACP *to*. A direct adapter instead *is* the
agent: it speaks ACP over stdio and drives an OpenAI-compatible HTTP model
itself, so a locally served model behaves like any other backend.

Several such adapters can exist, and they differ only in transport details —
endpoint, credentials, model catalogue, output ceilings. Everything below is
the part that does NOT depend on which provider is on the other end of the
wire:

* :func:`iter_tool` — the local tool runner (``bash`` / ``read_file`` /
  ``write_file``) the model's tool calls execute through. It yields
  ``progress`` frames while a tool runs, and that is not cosmetic: the
  gateway's read loop kills any backend that goes silent for its stall
  timeout, so a silent 10-minute build looked like a dead harness and was
  killed mid-work. Streaming the tail keeps the watchdog fed.
* :func:`estimate_message_tokens`, :func:`usage_tokens` and
  :func:`compact_history` — the token accounting and history trimming that
  keeps a request inside the model's window. Trimming works on whole
  tool-call/result blocks so it can never leave an unmatched tool call on the
  provider wire.
* :func:`bounded_model_id` / :func:`bounded_model_text` — the caps every
  adapter applies before a provider-supplied identifier or label is retained
  in memory or echoed into a session row.

Keeping this here rather than in each adapter is deliberate. The tool runner
is security-relevant — it is what decides whether a local model may execute a
shell command — and N copies of a security-relevant runner drift apart. One
implementation, one behaviour, one place to review.

The module is stdlib-only: it runs from the installed package under the same
interpreter as Kiro Crew, so it must not add a dependency the local path did
not already have.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any, Iterator

from kiro_crew.acp.tool_fs import read_text_pinned, resolve_tool_path, write_text_pinned

# Re-exported for the direct adapters and their tests, which import these
# protocol constants from here rather than from ``kiro_crew.acp.types``.
from kiro_crew.acp.types import (  # noqa: F401
    STOP_REASON_LOCAL_LIMIT,
    UPDATE_AGENT_STATUS,
    UPDATE_CONTEXT_WINDOW,
)
from kiro_crew.platform_compat import SIGKILL, kill_process_tree
from kiro_crew.sandbox import popen_limited, sandboxed_spawn_argv


def env_int(name: str, default: int) -> int:
    """An integer operator knob, falling back to *default* when unset or malformed.

    The knobs are read at import, before the adapter can answer ``initialize``,
    so a raise here would kill the process with a handshake failure that names
    no variable.
    """
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    """The float twin of :func:`env_int`."""
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


#: Cap on the tool result handed back to the model in one piece. A runaway
#: command can print megabytes; the model only needs the tail, and the full
#: text would blow the next request's budget.
MAX_TOOL_OUTPUT = 16000

#: Largest piece one read of a running command's output may return.
_READ_CHUNK_CHARS = 8192

#: A bash tool may legitimately run a long build or test suite, so the ceiling
#: is generous and operator-tunable: a cap of a few minutes would end every real
#: build with a timeout.
BASH_TOOL_TIMEOUT_SECS = env_float("DIRECT_BASH_TIMEOUT_SECS", 3600.0)

#: Credentials the child-tool environment must never inherit. A local adapter
#: may itself be configured with a provider key; a shell command it runs has
#: no business reading it.
_PROVIDER_SECRET_ENV = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "LMSTUDIO_API_KEY",
        "LM_STUDIO_API_KEY",
        "OPENROUTER_API_KEY",
    }
)

# Substrings that make a bash command refused without execution. A screen, not
# a sandbox: the real confinement is the spawn path. It exists so an obviously
# destructive command never runs even when the model was talked into asking.
_DANGER_PATTERNS = (
    "rm -rf /",
    "rm -rf ~",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "> /dev/sda",
    "chmod -R 777 /",
    "shutdown",
    "reboot",
    "diskutil eraseDisk",
)

# Tool name → the ACP "kind" the dashboard renders it under.
TOOL_KIND = {"bash": "execute", "read_file": "read", "write_file": "edit"}


def tool_env() -> dict[str, str]:
    """Child-tool environment with direct-provider credentials removed."""
    return {key: value for key, value in os.environ.items() if key not in _PROVIDER_SECRET_ENV}


def shell_argv(command: str) -> list[str]:
    """The argv that runs *command* in the platform's shell."""
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return ["/bin/sh", "-lc", command]


def dangerous_command_reason(command: str) -> str | None:
    """The matched danger pattern in *command*, or None when it looks safe."""
    lowered = command.lower()
    for pattern in _DANGER_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def iter_tool(
    name: str,
    args_json: str,
    cwd: str,
    should_cancel: Any = None,
) -> Iterator[tuple[str, str]]:
    """Run a tool, yielding ``("progress", tail)`` while it runs and ending
    with ``("final", result)``.

    The progress frames are load-bearing, not cosmetic: the ACP client's read
    loop kills any backend that goes silent for its stall timeout, so a long
    build that streams nothing would trip the watchdog and kill the harness
    mid-work.

    ``should_cancel`` (when given) is polled on a bounded timer independent of
    command output, so a silent command can still be stopped promptly.
    """
    try:
        args = json.loads(args_json or "{}")
    except ValueError:
        yield "final", "ERROR: malformed args"
        return
    if not isinstance(args, dict):
        yield "final", "ERROR: malformed args"
        return

    if name == "bash":
        if should_cancel is not None and should_cancel():
            yield "final", "ERROR: cancelled by user; tool not executed."
            return
        cmd = str(args.get("command", ""))
        if not cmd:
            yield "final", "ERROR: empty command"
            return
        pattern = dangerous_command_reason(cmd)
        if pattern:
            yield "final", (
                f"ERROR: command refused by safety screen (matched dangerous "
                f"pattern {pattern!r}). The command was NOT executed."
            )
            return
        cleanup: str | None = None
        try:
            wrapped, child_env, cleanup = sandboxed_spawn_argv(
                shell_argv(cmd), mode="standard", env=tool_env()
            )
            proc = popen_limited(
                wrapped,
                cwd=cwd,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                start_new_session=True,
            )
        except OSError as e:
            # The spawn failed after the sandbox profile was written; nothing
            # below runs, so remove it here.
            if cleanup:
                try:
                    os.unlink(cleanup)
                except OSError:
                    pass
            yield "final", f"ERROR: {e}"
            return
        timed_out = {"flag": False}
        cancelled = {"flag": False}

        def _kill(*, timeout: bool = True) -> None:
            # Runs on the timer thread and on the cancel path, so it must never
            # raise: an escaping error would leave the command running and, on
            # the cancel path, abort the turn instead of reporting a stop.
            # ``kill_process_tree`` is the platform-neutral group kill (POSIX
            # process group, ``taskkill /T`` on Windows, where ``os.killpg``
            # and ``signal.SIGKILL`` do not exist).
            if timeout:
                timed_out["flag"] = True
            try:
                kill_process_tree(proc.pid, SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        timer = threading.Timer(BASH_TOOL_TIMEOUT_SECS, _kill)
        timer.daemon = True
        timer.start()
        # Bounded while the command runs, not only at return: the adapter holds
        # what it reads, so a command printing hundreds of megabytes must not be
        # held whole. The tail is kept (where a build's error is), and the
        # amount dropped is reported to the model.
        chunks: list[str] = []
        held = 0
        dropped = 0
        last_emit = time.monotonic()
        try:
            stdout = proc.stdout
            if stdout is None:
                yield "final", "ERROR: command produced no output stream"
                return
            output_queue: queue.Queue[object] = queue.Queue()
            output_done = object()

            def _read_output() -> None:
                try:
                    # A size-limited readline, so one line with no newline is
                    # still read in bounded pieces.
                    for line in iter(lambda: stdout.readline(_READ_CHUNK_CHARS), ""):
                        output_queue.put(line)
                finally:
                    output_queue.put(output_done)

            reader = threading.Thread(target=_read_output, daemon=True)
            reader.start()
            while True:
                if should_cancel is not None and should_cancel():
                    cancelled["flag"] = True
                    _kill(timeout=False)
                    break
                try:
                    item = output_queue.get(timeout=0.2)
                except queue.Empty:
                    if proc.poll() is not None and not reader.is_alive():
                        break
                    continue
                if item is output_done:
                    if proc.poll() is not None:
                        break
                    continue
                text = str(item)
                chunks.append(text)
                held += len(text)
                if held > 2 * MAX_TOOL_OUTPUT:
                    joined = "".join(chunks)
                    dropped += len(joined) - MAX_TOOL_OUTPUT
                    chunks = [joined[-MAX_TOOL_OUTPUT:]]
                    held = MAX_TOOL_OUTPUT
                if time.monotonic() - last_emit >= 2.0:
                    yield "progress", "".join(chunks)[-1200:]
                    last_emit = time.monotonic()
            rc = proc.wait()
        finally:
            timer.cancel()
            if cleanup:
                try:
                    os.unlink(cleanup)
                except OSError:
                    pass
        out = "".join(chunks)
        if len(out) > MAX_TOOL_OUTPUT:
            dropped += len(out) - MAX_TOOL_OUTPUT
            out = out[-MAX_TOOL_OUTPUT:]
        if dropped:
            out = f"[{dropped} earlier characters of output omitted]\n" + out
        if cancelled["flag"]:
            out += "\n[tool cancelled by user]"
        elif timed_out["flag"]:
            out += f"\n[killed: exceeded the {BASH_TOOL_TIMEOUT_SECS:.0f}s tool budget]"
        yield "final", (out or "(no output)") + f"\n[exit {rc if not cancelled['flag'] else -2}]"
        return

    if name == "read_file":
        if should_cancel is not None and should_cancel():
            yield "final", "ERROR: cancelled by user; tool not executed."
            return
        try:
            text = read_text_pinned(str(args.get("path", "")), cwd=cwd)
            yield "final", text or "(empty)"
        except OSError as e:
            yield "final", f"ERROR: {e}"
        return

    if name == "write_file":
        if should_cancel is not None and should_cancel():
            yield "final", "ERROR: cancelled by user; tool not executed."
            return
        try:
            raw_path = str(args.get("path", ""))
            path = str(resolve_tool_path(raw_path, cwd))
            if should_cancel is not None and should_cancel():
                yield "final", "ERROR: cancelled by user; tool not executed."
                return
            write_text_pinned(raw_path, str(args.get("content", "")), cwd=cwd)
            yield "final", f"OK: wrote to {path}"
        except OSError as e:
            yield "final", f"ERROR: {e}"
        return

    yield "final", f"ERROR: unknown tool {name}"


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Conservative dependency-free token estimate for one wire message."""
    try:
        chars = len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        chars = len(str(message))
    return max(1, (chars + 2) // 3) + 12


def usage_tokens(value: object) -> int:
    """Coerce one provider ``usage`` count to a non-negative int, never raising.

    The counts ride back to Crew in a ``usage_update`` frame AFTER the turn's
    text has already streamed, so a malformed count (a dict, a garbage string,
    a null) must not abort a turn that already produced output. The meter is
    best-effort accounting, not a correctness gate: anything unreadable is 0
    ("not measured").
    """
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return 0


def history_blocks(messages: list[dict[str, Any]], start: int) -> list[list[dict[str, Any]]]:
    """Group assistant tool calls with their results so trimming stays valid."""
    blocks: list[list[dict[str, Any]]] = []
    index = start
    while index < len(messages):
        message = messages[index]
        block = [message]
        index += 1
        if message.get("role") == "assistant" and message.get("tool_calls"):
            while index < len(messages) and messages[index].get("role") == "tool":
                block.append(messages[index])
                index += 1
        elif message.get("role") == "tool":
            while index < len(messages) and messages[index].get("role") == "tool":
                block.append(messages[index])
                index += 1
        blocks.append(block)
    return blocks


def compact_history(
    messages: list[dict[str, Any]],
    context_window: int,
    *,
    reserve_tokens: int = 8192,
    preserve_from: dict[str, Any] | None = None,
    budget_tokens: int | None = None,
) -> int:
    """Drop oldest complete blocks while reserving output capacity.

    The system prompt and every block from ``preserve_from`` onward are
    retained. Assistant tool-call messages and their tool results are atomic,
    so compaction never leaves an unmatched tool call on the provider wire.
    A positive ``budget_tokens`` optionally tightens the message budget below
    the model's physical window; the caller accounts for tool schemas before
    passing it. Returns the number of messages removed.
    """
    if len(messages) <= 2 or context_window <= 0:
        return 0
    budget = max(1024, context_window - reserve_tokens - 2048)
    budget_override = (
        budget_tokens
        if isinstance(budget_tokens, int)
        and not isinstance(budget_tokens, bool)
        and budget_tokens > 0
        else None
    )
    if budget_override is not None:
        # The override is the caller's remaining message allowance after its
        # tool schemas. It can legitimately fall below 1,024 tokens; flooring
        # it here would retain history that cannot fit on the wire and make an
        # otherwise compactable local request fail unnecessarily.
        budget = min(budget, budget_override)
    if sum(estimate_message_tokens(message) for message in messages) <= budget:
        return 0

    system_end = 0
    while system_end < len(messages) and messages[system_end].get("role") == "system":
        system_end += 1
    prefix = [
        message
        for message in messages[:system_end]
        if not str(message.get("content") or "").startswith("[Kiro Crew compacted ")
    ]
    blocks = history_blocks(messages, system_end)
    compacted_for = (
        "the configured request budget"
        if budget_override is not None
        else "the model context window"
    )
    note = {
        "role": "system",
        "content": (
            f"[Kiro Crew compacted older messages to keep this session inside {compacted_for}. "
            "Continue from the retained recent context.]"
        ),
    }
    protected_index = len(blocks) - 1
    if preserve_from is not None:
        for index, block in enumerate(blocks):
            if any(message is preserve_from for message in block):
                protected_index = index
                break
    removed = 0
    while protected_index > 0:
        candidate = prefix + [note] + [message for block in blocks for message in block]
        if sum(estimate_message_tokens(message) for message in candidate) <= budget:
            break
        removed += len(blocks.pop(0))
        protected_index -= 1

    if not removed:
        return 0
    note["content"] = (
        f"[Kiro Crew compacted {removed} older message(s) to keep this session inside "
        f"{compacted_for}. Continue from the retained recent context.]"
    )
    messages[:] = prefix + [note] + [message for block in blocks for message in block]
    return removed


# ── Provider-supplied identifier/label bounds ──────────────────────────────
#
# A provider is free to return an absurd or hostile row. These caps are what
# the adapters apply before such a value is retained in session state or
# echoed into a dashboard row, so one bad catalogue response cannot balloon
# memory or a rendered table.

MAX_MODEL_ID_LENGTH = 255
MAX_MODEL_NAME_LENGTH = 512
MAX_MODEL_DESCRIPTION_LENGTH = 2048
MAX_MODEL_CONTEXT_WINDOW = 10_000_000


def bounded_model_id(value: object) -> str | None:
    """Return a normalized model ID only when it fits the retained-state bound."""
    if not isinstance(value, str) or len(value) > MAX_MODEL_ID_LENGTH:
        return None
    model_id = value.strip()
    return model_id if model_id and len(model_id) <= MAX_MODEL_ID_LENGTH else None


def bounded_model_text(value: object, *, max_length: int) -> tuple[str | None, bool]:
    """Bound a model row label and report whether the source field was truncated."""
    if not isinstance(value, str):
        return None, False
    if len(value) <= max_length:
        return value, False
    return value[:max_length], True
