"""Tests for #7481 review — single-flight dedup for active ≡ named overlap.

When the active model.base_url matches a named custom_providers entry, the
catalog rebuild must probe that endpoint exactly once and populate both the
named group AND auto_detected_models_by_provider from that single result.

Without this fix, both the named loop and the active-endpoint block probe
the same URL, doubling latency and potentially exceeding the rebuild budget
on cold loads (see #7481 review gate result).
"""

import json
import pathlib
import sys
import urllib.request

import pytest

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO.parent / ".hermes" / "hermes-agent"))

import api.config as config
from api.config import CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    """Clear the TTL model cache before and after every test."""
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


def _setup_config(tmp_path, yaml_content, monkeypatch):
    """Write a config.yaml and reload."""
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(yaml_content, encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    config.reload_config()
    # Patch list_available_providers to avoid real network calls
    try:
        import hermes_cli.models as hm
        monkeypatch.setattr(hm, "list_available_providers", lambda: [])
    except Exception:
        pass


class TestSingleFlightOverlap:
    """Active endpoint ≡ named custom provider must probe exactly once."""

    def test_single_probe_when_active_matches_named(self, tmp_path, monkeypatch):
        """When model.base_url equals a named custom provider's base_url,
        only one HTTP probe should be made (not two)."""
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"data": [{"id": "live-model-1"}]}).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            requests.append(req.full_url)
            return Response()

        _setup_config(
            tmp_path,
            (
                "model:\n"
                "  provider: custom:ollama-local\n"
                "  base_url: http://localhost:11434/v1\n"
                "  api_key: local-key\n"
                "custom_providers:\n"
                "  - name: ollama-local\n"
                "    base_url: http://localhost:11434/v1\n"
                "    api_key: local-key\n"
            ),
            monkeypatch,
        )
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = config.get_available_models()

        # Exactly one probe — not two
        assert len(requests) == 1, (
            f"Expected exactly 1 probe for active=named overlap, got {len(requests)}: {requests}"
        )
        assert "http://localhost:11434/v1/models" in requests[0]

        # The named group should contain the live model
        groups = result.get("groups", [])
        named_group = next((g for g in groups if g.get("provider_id") == "custom:ollama-local"), None)
        assert named_group is not None, "custom:ollama-local group must exist"
        model_ids = [m["id"] for m in named_group.get("models", [])]
        assert "live-model-1" in model_ids, f"live-model-1 must appear in named group, got {model_ids}"

    def test_single_flight_when_named_has_configured_models_and_matches_active(self, tmp_path, monkeypatch):
        """When a named provider has configured models AND matches active,
        the named probe is skipped (single-flight), but the active block
        still probes once. Total: 1 probe, not 2."""
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"data": [{"id": "live-model"}]}).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            requests.append(req.full_url)
            return Response()

        _setup_config(
            tmp_path,
            (
                "model:\n"
                "  provider: custom:my-local\n"
                "  base_url: http://localhost:8080/v1\n"
                "custom_providers:\n"
                "  - name: my-local\n"
                "    base_url: http://localhost:8080/v1\n"
                "    models:\n"
                "      - configured-model-a\n"
                "      - configured-model-b\n"
            ),
            monkeypatch,
        )
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = config.get_available_models()

        # Active block probes once; named block is skipped (single-flight).
        # Total: 1 probe (active block only).
        assert len(requests) == 1, f"Expected 1 probe, got {len(requests)}: {requests}"

        groups = result.get("groups", [])
        named_group = next((g for g in groups if g.get("provider_id") == "custom:my-local"), None)
        assert named_group is not None
        model_ids = [m["id"] for m in named_group.get("models", [])]
        # Configured models take priority
        assert "configured-model-a" in model_ids
        assert "configured-model-b" in model_ids


class TestOriginalStarvationFix:
    """Original #7481 case: unreachable active, reachable named."""

    def test_reachable_named_not_starved_by_unreachable_active(self, tmp_path, monkeypatch):
        """When the active endpoint is unreachable but a named provider is
        reachable, the named provider's models must appear in the foreground
        response (not just after background completion)."""
        call_count = [0]

        class SuccessResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"data": [{"id": "named-live-model"}]}).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            url = req.full_url
            # Named provider succeeds
            if "named-server" in url:
                return SuccessResponse()
            # Active endpoint times out (simulated by raising)
            raise TimeoutError("active endpoint unreachable")

        _setup_config(
            tmp_path,
            (
                "model:\n"
                "  provider: custom:named-server\n"
                "  base_url: http://unreachable-host:9999/v1\n"
                "custom_providers:\n"
                "  - name: named-server\n"
                "    base_url: http://named-server:8080/v1\n"
                "    api_key: named-key\n"
            ),
            monkeypatch,
        )
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = config.get_available_models()

        # Named provider should have probed (1 probe for the named endpoint)
        # The active endpoint probe failure is expected but shouldn't block
        groups = result.get("groups", [])
        named_group = next((g for g in groups if g.get("provider_id") == "custom:named-server"), None)
        assert named_group is not None, "custom:named-server group must exist"
        model_ids = [m["id"] for m in named_group.get("models", [])]
        assert "named-live-model" in model_ids, (
            f"named-live-model must appear in foreground response, got {model_ids}"
        )
