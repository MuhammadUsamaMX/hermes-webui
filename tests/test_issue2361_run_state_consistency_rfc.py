"""Issue #2361 — the run state consistency contract must stay an explicit,
rebased authority matrix, not prose that drifts away from the source.

These tests pin the RFC artifact the umbrella issue asks for:

- a per-layer authority matrix naming authority, persistence lifetime,
  allowed divergence, and replay/recovery rules for every state layer,
- symbol-based source anchors that still exist in the code they name
  (never hardcoded line numbers, per #5513 / #5542),
- invariant numbering that is append-only, because shipped code cites
  invariants by number (`static/boot.js`, `tests/test_cancel_stream_owner_guard.py`),
- a review checklist that covers derived caches and recovery provenance.
"""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
RFC = ROOT / "docs" / "rfcs" / "webui-run-state-consistency-contract.md"
RFC_INDEX = ROOT / "docs" / "rfcs" / "README.md"
CONTRACTS = ROOT / "docs" / "CONTRACTS.md"


def _rfc() -> str:
    assert RFC.exists(), "run state consistency RFC must exist"
    return RFC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_rfc_is_indexed_for_the_umbrella_issue():
    index = RFC_INDEX.read_text(encoding="utf-8")
    contracts = CONTRACTS.read_text(encoding="utf-8")

    assert "webui-run-state-consistency-contract.md" in index
    assert "#2361" in index
    assert "docs/rfcs/webui-run-state-consistency-contract.md" in contracts


def test_rfc_has_an_authority_matrix_with_the_four_required_axes():
    """The maintainer asked for authority, persistence lifetime, allowed
    divergence, and replay/recovery rules per layer (#2361)."""
    text = _rfc()

    header = next(
        (
            line
            for line in text.splitlines()
            if line.startswith("| Layer | Authority")
        ),
        None,
    )
    assert header is not None, "RFC must contain a '| Layer | Authority' matrix"

    for column in (
        "Persistence lifetime",
        "Allowed divergence",
        "Replay / recovery rule",
    ):
        assert column in header, f"authority matrix must have a {column!r} column"


def test_rfc_authority_matrix_covers_every_state_layer():
    text = _rfc()
    matrix = _section(text, "## State Layers")

    layers = [
        "Visible transcript",
        "Model context",
        "Pending turn metadata",
        "Live stream",
        "Worker lifecycle registry",
        "Run journal",
        "Compression summary",
        "Live UI scene",
        "Sidebar/session metadata",
        "Derived model/context metadata",
    ]
    missing = [layer for layer in layers if layer not in matrix]
    assert missing == [], f"authority matrix must cover layers: {missing}"


# (file, symbol) pairs the RFC cites as stable source anchors. Symbol names
# only — line numbers rot on any source-layout shift (#5513, #5542).
SOURCE_ANCHORS = [
    ("api/config.py", "ACTIVE_RUNS"),
    ("api/config.py", "STREAMS"),
    ("api/config.py", "SESSION_INDEX_FILE"),
    ("api/config.py", "_get_models_cache_path"),
    ("api/models.py", "pending_user_message"),
    ("api/models.py", "context_messages"),
    ("api/run_journal.py", "RUN_JOURNAL_DIR_NAME"),
    ("api/turn_journal.py", "TURN_JOURNAL_DIR_NAME"),
    ("api/session_recovery.py", "recover_session"),
    ("api/compression_anchor.py", "is_context_compression_marker"),
]


def test_rfc_source_anchors_land_on_real_symbols():
    text = _rfc()
    for rel, symbol in SOURCE_ANCHORS:
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert symbol in source, f"{rel} must still define {symbol!r}"
        assert symbol in text, (
            f"RFC must name {symbol!r} as a symbol anchor (not a line number)"
        )


def test_rfc_cites_no_hardcoded_source_line_numbers():
    text = _rfc()
    stale = re.findall(r"\b\w+\.py:\d+(?:-\d+)?", text)
    assert not stale, f"RFC must not cite source line numbers; found {stale!r}"


def test_rfc_invariants_are_append_only():
    """Shipped code cites invariants by number, so existing numbers and titles
    must never shift when a new invariant is appended."""
    text = _rfc()
    invariants = _section(text, "## Core Invariants")

    numbers = [
        int(match.group(1))
        for match in re.finditer(r"^(\d+)\. \*\*", invariants, re.MULTILINE)
    ]
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"invariants must be numbered sequentially with no gaps: {numbers}"
    )
    assert numbers[-1] >= 11, "RFC must carry the derived-state and recovery invariants"

    shipped = {
        1: "Visible current turns enter model context.",
        2: "Active turn UI keeps its owner.",
        3: "Reattach preserves order or degrades clearly.",
        4: "Maintenance is not activity.",
        5: "Replay is idempotent.",
        6: "Compression is not current intent.",
        7: "Observation has a degraded path.",
        8: "Every mutation names its layer.",
        9: "Lifecycle-busy is not client-attachable.",
    }
    for number, title in shipped.items():
        assert f"{number}. **{title}**" in invariants, (
            f"invariant #{number} {title!r} is cited elsewhere and must not move"
        )

    assert "10. **" in invariants and "derived" in invariants.lower()
    assert "11. **" in invariants and "provenance" in invariants.lower()


def test_rfc_review_checklist_covers_caches_and_recovery_provenance():
    checklist = _section(_rfc(), "## Review Checklist").lower()

    assert "cache" in checklist, "checklist must ask about derived caches"
    assert "provenance" in checklist or "recovered" in checklist, (
        "checklist must ask about recovery provenance metadata"
    )


def test_rfc_issue_map_names_layer_and_invariant_for_new_slices():
    issue_map = _section(_rfc(), "## Existing Issue Map")

    header = next(
        (line for line in issue_map.splitlines() if line.startswith("| Example |")),
        None,
    )
    assert header is not None, "issue map table must exist"
    assert "| Layer |" in header, "issue map must name the touched state layer"

    for issue in ("#2442", "#2443", "#4208", "#4216"):
        assert issue in issue_map, f"issue map must include {issue}"
