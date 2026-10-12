import os
import time

from api.run_journal import (
    RunJournalWriter,
    _run_path,
    run_journal_retention_enabled,
    sweep_run_journals,
)


def _mk_run(tmp_path, sid, rid, terminal=True):
    w = RunJournalWriter(sid, rid, session_dir=tmp_path)
    w.append_sse_event("token", {"text": "x"})
    if terminal:
        w.append_sse_event("done", {"session": {"session_id": sid}})
    return _run_path(sid, rid, session_dir=tmp_path)


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_sweep_retires_old_terminal_run(tmp_path):
    path = _mk_run(tmp_path, "s1", "r1", terminal=True)
    _age(path, 20)

    stats = sweep_run_journals(tmp_path, max_age_days=14, max_size_mb=0)

    assert stats["removed"] == 1
    assert not path.exists()


def test_sweep_keeps_recent_terminal_run(tmp_path):
    path = _mk_run(tmp_path, "s1", "r1", terminal=True)

    stats = sweep_run_journals(tmp_path, max_age_days=14, max_size_mb=0)

    assert stats["removed"] == 0
    assert path.exists()


def test_sweep_never_touches_non_terminal_run(tmp_path):
    path = _mk_run(tmp_path, "s1", "r1", terminal=False)
    _age(path, 999)

    stats = sweep_run_journals(tmp_path, max_age_days=1, max_size_mb=0)

    assert stats["removed"] == 0
    assert path.exists()


def test_size_cap_retires_oldest_terminal_runs(tmp_path):
    old = _mk_run(tmp_path, "s1", "r_old", terminal=True)
    _age(old, 1)
    new = _mk_run(tmp_path, "s1", "r_new", terminal=True)
    live = _mk_run(tmp_path, "s1", "r_live", terminal=False)
    for p in (old, new, live):
        os.utime(p, None)  # reset: size trigger must not depend on age

    # Tiny cap forces retirement of terminal files (oldest first); live run stays.
    stats = sweep_run_journals(tmp_path, max_age_days=10_000, max_size_mb=0.000001)

    assert stats["removed"] >= 1
    assert not old.exists()          # oldest terminal goes first
    assert live.exists()             # non-terminal never removed


def test_retention_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_RETENTION", "off")
    assert run_journal_retention_enabled() is False
    path = _mk_run(tmp_path, "s1", "r1", terminal=True)
    _age(path, 999)

    stats = sweep_run_journals(tmp_path, max_age_days=1, max_size_mb=0)

    assert stats["removed"] == 0
    assert path.exists()
