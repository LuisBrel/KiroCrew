"""Tests for the record-time ``invisible_only`` marker on assistant messages.

Covers ``chat_runner._mark_invisible_only``: the helper that records, once at
the point a turn is persisted, whether the finalized assistant reply renders as
nothing. Without it every transcript consumer re-derives the Cf rule from the
content, and the rows accrete in stores with nothing on them to key off (#7599).

Two properties matter beyond the stamping itself and are asserted here:

* the verdict is computed by the ONE existing implementation of the Cf drop,
  not a fresh copy of the rule;
* the stored content is never rewritten — the marker is additive, so a
  transcript cannot be damaged by it and dropping such rows stays a later,
  separate decision.
"""

import inspect

from kiro_crew import preview_text
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _mark_invisible_only
from kiro_crew.dashboard.state import _ChatSlot


def _slot_with_reply(content: str, **append_kwargs) -> _ChatSlot:
    slot = _ChatSlot("test-invisible-only")
    slot.append("assistant", content, "msg msg-a", broadcast=False, **append_kwargs)
    # append flags the slot dirty; clear it so the marker's own flag is visible.
    slot._dirty = False
    return slot


def test_verdict_comes_from_the_one_cf_implementation():
    """The helper must delegate, not restate the rule.

    ``drop_format_chars`` documents itself as the single implementation of the
    Cf drop, and the frontend predicate mirrors it. A private copy inside
    chat_runner would drift from both — and drift is the defect this marker
    exists to remove, not a cosmetic concern.
    """
    assert chat_runner.drop_format_chars is preview_text.drop_format_chars


class TestMarkInvisibleOnly:
    def test_stamps_a_zwsp_only_reply(self):
        # The quiet monitor-cycle say-nothing reply.
        slot = _slot_with_reply("\u200b")
        _mark_invisible_only(slot)
        assert slot.messages[-1]["meta"]["invisible_only"] is True

    def test_covers_the_whole_cf_class_plus_padding(self):
        slot = _slot_with_reply("\u200b\u200c \u200d\u2060\ufeff\u00ad\n")
        _mark_invisible_only(slot)
        assert slot.messages[-1]["meta"]["invisible_only"] is True

    def test_ordinary_reply_is_not_stamped_at_all(self):
        # Absent, not False: absence is what every pre-marker row on disk looks
        # like, so a reader can treat it as "derive it yourself".
        slot = _slot_with_reply("done café")
        _mark_invisible_only(slot)
        assert "invisible_only" not in slot.messages[-1].get("meta", {})

    def test_embedded_format_chars_are_not_invisible_only(self):
        slot = _slot_with_reply("a\u200bb")
        _mark_invisible_only(slot)
        assert "invisible_only" not in slot.messages[-1].get("meta", {})

    def test_content_is_left_intact(self):
        # The marker is additive by design. Normalizing the content to empty
        # would be a lossy one-way write over persisted transcript text.
        slot = _slot_with_reply("\u200b")
        _mark_invisible_only(slot)
        assert slot.messages[-1]["content"] == "\u200b"

    def test_stamps_a_row_that_carries_file_change_chips(self):
        # The marker states a fact about the TEXT. The chips exception is the
        # reader's, because it composes with facts the write side cannot know
        # (a regenerate can add a visible variant turns later).
        slot = _slot_with_reply("\u200b", meta={"file_changes": [{"path": "a.ts"}]})
        _mark_invisible_only(slot)
        meta = slot.messages[-1]["meta"]
        assert meta["invisible_only"] is True
        assert meta["file_changes"] == [{"path": "a.ts"}]

    def test_flags_the_slot_dirty(self):
        # Mirrors _flush_file_changes: meta is mutated in place without an
        # append, so nothing else marks the slot for the periodic flush.
        slot = _slot_with_reply("\u200b")
        _mark_invisible_only(slot)
        assert slot._dirty is True

    def test_only_the_last_assistant_row_is_considered(self):
        slot = _ChatSlot("test-invisible-only-multi")
        slot.append("assistant", "\u200b", "msg msg-a", broadcast=False)
        slot.append("assistant", "the visible answer", "msg msg-a", broadcast=False)
        _mark_invisible_only(slot)
        assert "invisible_only" not in slot.messages[0].get("meta", {})
        assert "invisible_only" not in slot.messages[1].get("meta", {})

    def test_turn_boundary_protects_the_previous_turn(self):
        # An error-only turn appends no assistant message; without the boundary
        # the walk-back would reach into the turn before it.
        slot = _ChatSlot("test-invisible-only-boundary")
        slot.append("assistant", "\u200b", "msg msg-a", broadcast=False)
        boundary = len(slot.messages)
        slot.append("error", "boom", "msg msg-err", broadcast=False)
        _mark_invisible_only(slot, turn_boundary=boundary)
        assert "invisible_only" not in slot.messages[0].get("meta", {})

    def test_no_assistant_message_is_noop(self):
        slot = _ChatSlot("test-invisible-only-none")
        slot.append("error", "boom", "msg msg-err", broadcast=False)
        _mark_invisible_only(slot)
        assert len(slot.messages) == 1
        assert "invisible_only" not in slot.messages[0].get("meta", {})

    def test_stamping_twice_is_idempotent(self):
        # The success path stamps, then the finally block stamps again on the
        # same row. The second pass must be a no-op, not a second write.
        slot = _slot_with_reply("\u200b")
        _mark_invisible_only(slot)
        first = dict(slot.messages[-1]["meta"])
        _mark_invisible_only(slot)
        assert slot.messages[-1]["meta"] == first
        assert first["invisible_only"] is True


class TestPersistCallSite:
    """The stamp is worthless unless the persist paths actually run it.

    ``run_chat_turn`` has no mountable unit seam, so the wiring is pinned by
    source the way the frontend pins ChatPage's render chain.
    """

    src = inspect.getsource(chat_runner)
    CALL = "_mark_invisible_only(slot, turn_boundary=_turn_msg_boundary)"

    def test_marker_runs_before_the_history_save(self):
        mark = self.src.index(self.CALL)
        save = self.src.index("await save_slot_off_loop(state, slot)")
        assert mark < save

    def test_marker_runs_after_the_file_changes_flush(self):
        # A row that just gained diff chips must already carry them, so the
        # reader sees the text verdict and the chips together.
        flush = self.src.index("_flush_file_changes(slot)")
        mark = self.src.index(self.CALL)
        assert flush < mark

    def test_both_flush_sites_stamp(self):
        # A cancelled or errored turn can still have appended a finalized
        # invisible-only reply. Stamping at only one site would leave rows
        # persisted forever without a marker, so absence could never mean
        # "predates the marker" — which is the contract the reader relies on.
        assert self.src.count(self.CALL) == 2

    def test_turn_boundary_is_bound_before_the_enclosing_try(self):
        # The finally block reads _turn_msg_boundary, so it must be bound even
        # when the turn raises before the authoritative capture.
        lines = self.src.splitlines()
        first_bind = next(i for i, ln in enumerate(lines) if "_turn_msg_boundary = " in ln)
        first_use = next(i for i, ln in enumerate(lines) if self.CALL in ln)
        assert first_bind < first_use
        assert lines[first_bind].startswith("    _turn_msg_boundary = len(slot.messages)")
