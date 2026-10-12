"""Per-session reasoning effort (#2697): session-scoped override + global default.

The WebUI must be able to set reasoning effort for ONE conversation without
touching ``agent.reasoning_effort`` in the profile's config.yaml, and a new
session must keep falling back to that configured default.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from api.config import get_reasoning_status
from api.models import get_session, new_session
from api.routes import handle_get, handle_post

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
GATEWAY_PY = (REPO / "api" / "gateway_chat.py").read_text(encoding="utf-8")


class _Handler:
    """Minimal BaseHTTPRequestHandler stand-in supporting GET and POST."""

    def __init__(self, body: dict | None = None, command: str = "POST"):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.command = command
        self.status = None
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = self
        self.body = bytearray()
        self.sent_headers = []
        self.request = None

    def send_response(self, code: int):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def payload(self) -> dict:
        return json.loads(bytes(self.body).decode("utf-8"))


def _new_saved_session(**kwargs) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        session = new_session(workspace=tmp, **kwargs)
    session.save()
    return session.session_id


def _post(body: dict) -> tuple[int, dict]:
    handler = _Handler(body, command="POST")
    handle_post(handler, urlparse("/api/reasoning"))
    return handler.status, handler.payload()


def _get(query: str) -> tuple[int, dict]:
    handler = _Handler(None, command="GET")
    handle_get(handler, urlparse("/api/reasoning" + query))
    return handler.status, handler.payload()


# ── Backend: per-session override ───────────────────────────────────────────

def test_post_with_session_id_sets_session_override_not_config():
    sid = _new_saved_session()

    status, payload = _post({"session_id": sid, "effort": "high"})

    assert status == 200, payload
    assert payload["reasoning_effort"] == "high"
    assert payload["effort_scope"] == "session"
    assert get_session(sid).reasoning_effort == "high"
    # The profile default must be untouched — that is the whole point (#2697).
    default = get_reasoning_status()
    assert default["effort_scope"] == "default"
    assert default["reasoning_effort"] != "high"


def test_sessions_are_independent_and_new_sessions_use_the_default():
    a = _new_saved_session()
    b = _new_saved_session()

    _post({"session_id": a, "effort": "high"})
    _post({"session_id": b, "effort": "low"})

    assert get_session(a).reasoning_effort == "high"
    assert get_session(b).reasoning_effort == "low"
    # A brand-new session inherits the configured default, not a sibling's pick.
    c = _new_saved_session()
    assert get_session(c).reasoning_effort is None
    assert get_reasoning_status()["effort_scope"] == "default"


def test_clearing_session_override_falls_back_to_default():
    sid = _new_saved_session()
    _post({"session_id": sid, "effort": "low"})

    status, payload = _post({"session_id": sid, "effort": ""})

    assert status == 200, payload
    assert get_session(sid).reasoning_effort is None
    assert payload["effort_scope"] == "default"


def test_session_override_is_persisted_across_reload():
    sid = _new_saved_session(reasoning_effort="minimal")

    from api.models import Session

    assert Session.load_metadata_only(sid).reasoning_effort == "minimal"


def test_post_with_session_id_rejects_unknown_effort():
    sid = _new_saved_session()

    status, _payload = _post({"session_id": sid, "effort": "ultra"})

    assert status == 400
    assert get_session(sid).reasoning_effort is None


def test_post_without_session_id_still_writes_profile_default():
    handler = _Handler({"effort": "low"}, command="POST")
    handle_post(handler, urlparse("/api/reasoning"))
    payload = handler.payload()

    assert handler.status == 200, payload
    assert payload["effort_scope"] == "default"
    # Restore the untouched default for other tests in this session.
    _post({"effort": ""})


def test_get_with_session_id_reports_session_scope():
    sid = _new_saved_session()
    _post({"session_id": sid, "effort": "high"})

    scoped = _get(f"?session_id={sid}")[1]
    unscoped = _get("")[1]

    assert scoped["reasoning_effort"] == "high"
    assert scoped["effort_scope"] == "session"
    assert unscoped["effort_scope"] == "default"
    assert unscoped["reasoning_effort"] != "high"


def test_unknown_session_id_on_get_falls_back_to_default():
    status, payload = _get("?session_id=doesnotexist")

    assert status == 200
    assert payload["effort_scope"] == "default"


# ── Streaming / gateway: the session override is what the turn actually uses ─

def test_gateway_effort_resolver_honours_session_override():
    from api.gateway_chat import _gateway_reasoning_effort_for_request

    cfg = {"agent": {"reasoning_effort": "low"}}

    assert _gateway_reasoning_effort_for_request(cfg, model="gpt-5") == "low"
    assert (
        _gateway_reasoning_effort_for_request(
            cfg, model="gpt-5", session_effort="high"
        )
        == "high"
    )
    # An empty/absent session override keeps the configured default.
    assert (
        _gateway_reasoning_effort_for_request(
            cfg, model="gpt-5", session_effort=None
        )
        == "low"
    )


def test_streaming_reads_session_effort_before_config_default():
    """The agent-construction path must prefer the session's own effort."""
    match = re.search(
        r"CLI-parity reasoning effort:.*?_reasoning_config = parse_reasoning_effort\(_effort\)",
        STREAMING_PY,
        re.DOTALL,
    )
    assert match, "streaming reasoning-effort block not found"
    block = match.group(0)
    assert "getattr(_session_meta, 'reasoning_effort'" in block, (
        "the streaming path must read the per-session reasoning_effort "
        "override before falling back to agent.reasoning_effort"
    )
    assert block.index("_session_effort") < block.index("_effort = coerce_reasoning_effort_for_model"), (
        "the session override must be applied BEFORE the profile default is coerced"
    )


# ── Frontend: the composer chip and /reasoning command are session-scoped ───

def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function body not found: {signature}")


def test_reasoning_effort_context_carries_the_active_session_id():
    body = _function_body(UI_JS, "function _reasoningEffortContext")
    compact = body.replace(" ", "")

    assert "session_id" in compact, (
        "the reasoning chip must send the active session id so a pick is "
        "scoped to that conversation (#2697)"
    )


def test_session_switch_refetches_the_reasoning_chip():
    query = _function_body(UI_JS, "function _reasoningEffortQuery")
    ctx = _function_body(UI_JS, "function _reasoningEffortContext")
    assert "URLSearchParams(_reasoningEffortContext())" in query
    assert "session_id" in ctx.replace(" ", ""), (
        "the fetch cache key must include the session id, otherwise switching "
        "sessions reuses another conversation's chip state"
    )


def test_reasoning_command_sets_effort_for_the_active_session():
    body = _function_body(COMMANDS_JS, "function cmdReasoning")
    effort_branch = body[body.index("EFFORTS.includes(arg)") :]

    assert "/api/reasoning" in effort_branch
    assert "_reasoningEffortContext" in effort_branch, (
        "/reasoning <level> must scope its POST to the active session so it "
        "does not rewrite the profile default for every other conversation"
    )
