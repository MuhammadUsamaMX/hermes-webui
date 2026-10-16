"""Regression coverage for #7587 — a repeated user turn must never be lost.

Investigation correction (see card comment): the visible/content keys are
occurrence-free **on purpose** so that a provider-*restamped* twin still dedupes
against its sidecar original (``test_large_merge_dedupes_workspace_prefixed_middle_replay``
requires a state row at ts 2000 to dedupe against its twin at ts 499.5). Adding
occurrence metadata to those keys therefore reverses a deliberate invariant.

The actual defect is replay-cursor desynchronization in
``merge_session_messages_append_only``. ``state_replay_idx`` tracks how far the
state transcript has been matched against the sidecar IN ORDER. Several
suppression paths consume a state row that genuinely corresponds to a sidecar
row WITHOUT advancing that cursor. Once desynced, a later state row is compared
against an already-consumed sidecar slot and — because two turns with identical
content project the same visible key — is suppressed. When the state row being
mis-consumed is a user turn the user actually sent twice, that is silent data
loss on the restore path.

The fix must be cursor bookkeeping: every suppression path that consumes a
sidecar row has to advance the replay cursor past it.
"""

from api.models import (
    _session_message_merge_key,
    _session_message_visible_key,
    merge_session_messages_append_only,
)


SAME_TEXT = "restart the deploy and tell me when it is green"


def _user(content, ts, **kw):
    row = {"role": "user", "content": content, "timestamp": ts}
    row.update(kw)
    return row


def _assistant(content, ts, **kw):
    row = {"role": "assistant", "content": content, "timestamp": ts}
    row.update(kw)
    return row


def _user_rows(merged):
    return [row for row in merged if row.get("role") == "user"]


# ── the defect: desynced cursor eats a repeated user turn ─────────────────


def test_repeated_turn_survives_when_state_lacks_the_sidecar_leading_row():
    """#7587 core: the state transcript is missing the sidecar's first row.

    The assistant state row cannot claim sidecar slot 0 (a user turn), so it is
    consumed by the full-precision dedup path. The cursor stays at 0 and the
    genuine repeat at ts 2000 is then matched against slot 0 (the ts 1000 turn)
    and dropped — losing a user turn the user actually sent.
    """
    sidecar = [_user("hi", 1000.0), _assistant("a", 1100.0)]
    state = [
        _assistant("a", 1100.0, _state_db_row_id=2),
        _user("hi", 2000.0, _state_db_row_id=3),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert len(_user_rows(merged)) == 2, [r.get("content") for r in _user_rows(merged)]


def test_repeated_turn_survives_after_a_desyncing_skipped_row():
    """Same defect with a longer sidecar, so the desync happens mid-transcript."""
    sidecar = [
        _user("first", 1000.0),
        _assistant("ack", 1100.0),
        _user(SAME_TEXT, 2000.0),
        _assistant("done", 2100.0),
    ]
    # state transcript has no twin for the leading "first" turn
    state = [
        _assistant("ack", 1100.0, _state_db_row_id=2),
        _user(SAME_TEXT, 2000.0, _state_db_row_id=3),
        _assistant("done", 2100.0, _state_db_row_id=4),
        _user(SAME_TEXT, 3000.0, _state_db_row_id=5),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert len(_user_rows(merged)) == 3, [r.get("content") for r in _user_rows(merged)]


def test_multimodal_repeat_survives_cursor_desync():
    """The same loss with an image-bearing repeat, not just plain text."""
    image = [
        {"type": "text", "text": "why did this fail?"},
        {"type": "image_url", "image_url": {"url": "file:///tmp/shot.png"}},
    ]
    sidecar = [_user("hi", 1000.0), _assistant("a", 1100.0)]
    state = [
        _assistant("a", 1100.0, _state_db_row_id=2),
        _user(image, 2000.0, _state_db_row_id=3),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert len(_user_rows(merged)) == 2


# ── controls: the shapes that already worked must keep working ───────────


def test_ordered_replay_keeps_the_repeat():
    """Control: when state replays the sidecar in order, the repeat survives."""
    sidecar = [_user("hi", 1000.0), _assistant("a", 1100.0)]
    state = [
        _user("hi", 1000.0, _state_db_row_id=1),
        _assistant("a", 1100.0, _state_db_row_id=2),
        _user("hi", 2000.0, _state_db_row_id=3),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert len(_user_rows(merged)) == 2


def test_restamped_twin_still_dedupes():
    """Control for the invariant #7587's suggested fix would have broken.

    A provider restamped this row far into the future; it is the SAME logical
    turn and must NOT render twice.
    """
    sidecar = [_user(SAME_TEXT, 1000.0), _assistant("a", 1100.0)]
    state = [
        _user(SAME_TEXT, 1000.0, _state_db_row_id=1),
        _assistant("a", 1100.0, _state_db_row_id=2),
        _user(SAME_TEXT, 9000.0, _state_db_row_id=3),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert len(_user_rows(merged)) == 2


def test_restamped_twin_of_the_prompt_itself_still_dedupes():
    """Control: the prompt restamped forward, with no reply, must stay one row."""
    sidecar = [_user(SAME_TEXT, 1000.0)]
    state = [
        _user("[Workspace::v1: /tmp/p]\n" + SAME_TEXT, 2000.0, _state_db_row_id=1),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert len(_user_rows(merged)) == 1


def test_single_occurrence_replay_is_still_one_row():
    """Control: one logical turn present in both stores must not double."""
    sidecar = [_user(SAME_TEXT, 1000.0), _assistant("a", 1100.0)]
    state = [
        _user(SAME_TEXT, 1000.0, _state_db_row_id=1),
        _assistant("a", 1100.0, _state_db_row_id=2),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert len(merged) == 2


def test_distinct_new_text_after_desync_is_unaffected():
    """Control: the non-repeat shape in the same position already worked."""
    sidecar = [_user("hi", 1000.0), _assistant("a", 1100.0)]
    state = [
        _assistant("a", 1100.0, _state_db_row_id=2),
        _user("brand new question", 2000.0, _state_db_row_id=3),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert len(_user_rows(merged)) == 2


def test_merge_key_still_separates_repeated_turns():
    """Control: the pre-existing occurrence discriminator must not regress."""
    assert _session_message_merge_key(_user(SAME_TEXT, 1000.0)) != (
        _session_message_merge_key(_user(SAME_TEXT, 2000.0))
    )


def test_visible_key_remains_occurrence_free():
    """Control: the deliberate invariant #7587's premise wanted to break.

    Kept explicit so a future change cannot silently re-break restamped-replay
    dedup while claiming to fix #7587.
    """
    assert _session_message_visible_key(
        _user(SAME_TEXT, 1000.0)
    ) == _session_message_visible_key(_user(SAME_TEXT, 2000.0))