"""Regression tests for Issue #7622: a fresh install must not default to English.

Before this fix the UI locale resolved as server-setting -> localStorage ->
hardcoded English, and the server's *default* setting was the literal string
"en". That made browser detection unreachable: a brand-new install always came
up in English no matter what the visitor's browser asked for.

These tests pin the three parts of the fix:

1. `static/i18n.js` resolves an unset locale from `navigator.languages`
   (falling back to English only when nothing matches).
2. `api/config.py` ships `language` as "" (unset), not "en", so "no explicit
   choice" is distinguishable from an explicit "I want English".
3. `api/routes.py` renders the server-side `/login` page from the
   `Accept-Language` header while the setting is still unset.
"""

import json
import pathlib
import subprocess
import sys
import textwrap
import urllib.request

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

sys.path.insert(0, str(REPO_ROOT))


# ── 1. Client-side browser detection (static/i18n.js) ────────────────────────


def _run_i18n_case(script_expr: str, languages=None, language=None) -> dict:
    """Evaluate an i18n.js expression in a sandbox with a faked `navigator`."""
    wrapped_expr = f"(() => ({script_expr}))()"
    nav = "undefined" if languages is None and language is None else (
        "{ languages: %s, language: %s }" % (
            json.dumps(languages) if languages is not None else "undefined",
            json.dumps(language) if language is not None else "undefined",
        )
    )
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const src = fs.readFileSync({json.dumps(str(REPO_ROOT / "static" / "i18n.js"))}, 'utf8');
        const storage = {{}};
        const ctx = {{
          navigator: {nav},
          localStorage: {{
            getItem: (k) => Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null,
            setItem: (k, v) => {{ storage[k] = String(v); }},
          }},
          document: {{
            documentElement: {{ lang: '' }},
            querySelectorAll: () => [],
          }},
        }};
        vm.createContext(ctx);
        vm.runInContext(src, ctx);
        const out = vm.runInContext({json.dumps(wrapped_expr)}, ctx);
        process.stdout.write(JSON.stringify(out));
        """
    )
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def test_detect_browser_locale_resolves_bcp47_variants():
    result = _run_i18n_case(
        "({zh: detectBrowserLocale()})", languages=["zh-CN", "en-US"]
    )
    assert result["zh"] == "zh"


def test_detect_browser_locale_walks_the_ranked_list():
    # First entry unknown, second one known -> the browser's own order wins.
    result = _run_i18n_case(
        "({ja: detectBrowserLocale()})", languages=["xx-YY", "ja-JP"]
    )
    assert result["ja"] == "ja"


def test_detect_browser_locale_returns_null_when_nothing_matches():
    result = _run_i18n_case(
        "({none: detectBrowserLocale()})", languages=["xx-YY", "zz"]
    )
    assert result["none"] is None


def test_detect_browser_locale_tolerates_missing_navigator():
    # Non-browser contexts (tests, SSW pre-render) must not throw.
    result = _run_i18n_case("({none: detectBrowserLocale()})")
    assert result["none"] is None


def test_unset_locale_falls_back_to_browser_language_not_english():
    result = _run_i18n_case(
        "({lang: resolvePreferredLocale('', null)})",
        languages=["de-DE", "en"],
    )
    assert result["lang"] == "de"


def test_browser_detection_only_applies_when_nothing_else_is_set():
    result = _run_i18n_case(
        """
({
  fromServer: resolvePreferredLocale('ru', null),   // explicit server setting
  fromStored:  resolvePreferredLocale('', 'ja'),    // explicit user choice
  fromBrowser: resolvePreferredLocale('', ''),      // nothing set at all
  englishLast: resolvePreferredLocale(null, null),  // browser says en-US
})
        """,
        languages=["en-US"],
    )
    assert result["fromServer"] == "ru"
    assert result["fromStored"] == "ja"
    assert result["fromBrowser"] == "en"
    assert result["englishLast"] == "en"


def test_unresolvable_browser_language_still_falls_back_to_english():
    result = _run_i18n_case(
        "({lang: resolvePreferredLocale('', '')})", languages=["xx-YY"]
    )
    assert result["lang"] == "en"


def test_set_locale_still_persists_an_explicit_choice():
    result = _run_i18n_case(
        """
({
  ...(setLocale('zh-CN'), {}),
  saved: localStorage.getItem('hermes-lang'),
  htmlLang: document.documentElement.lang,
})
        """,
        languages=["en-US"],
    )
    assert result["saved"] == "zh"
    assert result["htmlLang"] == "zh-CN"


def test_resolution_precedence_order_is_server_then_stored_then_browser():
    # Cheap structural guard so a future edit cannot silently reorder the chain.
    fn = I18N_JS[
        I18N_JS.index("function resolvePreferredLocale(") :
    ]
    fn = fn[: fn.index("\n}")]
    assert fn.index("resolveLocale(primary)") < fn.index("resolveLocale(fallback)")
    assert fn.index("resolveLocale(fallback)") < fn.index("detectBrowserLocale()")
    assert fn.index("detectBrowserLocale()") < fn.index("'en'")


# ── 2. Server default must be "unset", not "en" ──────────────────────────────


def test_default_language_setting_is_unset_not_english():
    from api.config import _SETTINGS_DEFAULTS

    assert _SETTINGS_DEFAULTS["language"] == "", (
        "a fresh install must ship language as '' (unset) so browser detection "
        "is reachable; shipping the literal 'en' makes #7622 unfixable"
    )


def test_language_setting_accepts_empty_string_to_reset_to_auto():
    from api.config import _SETTINGS_LANG_RE

    # "" is the documented reset-to-auto value and is accepted verbatim; every
    # real tag must still be validated by the BCP-47-ish pattern.
    assert _SETTINGS_LANG_RE.match("zh-CN")
    assert _SETTINGS_LANG_RE.match("en")
    assert not _SETTINGS_LANG_RE.match("")
    assert not _SETTINGS_LANG_RE.match("english!")


# ── 3. Server-rendered login page honours Accept-Language (#7622) ────────────


def test_accept_language_parser_picks_the_highest_quality_tag():
    from api.routes import _login_locale_from_accept_language

    assert _login_locale_from_accept_language("fr-CA,fr;q=0.9,en;q=0.8") == "fr"
    assert _login_locale_from_accept_language("ja;q=0.4, de;q=0.9") == "de"


def test_accept_language_parser_handles_aliases_and_quotes():
    from api.routes import _login_locale_from_accept_language

    assert _login_locale_from_accept_language("zh-TW") == "zh-Hant"
    assert _login_locale_from_accept_language("zh-Hans-CN") == "zh"
    assert _login_locale_from_accept_language('"pt-BR"') == "pt"
    assert _login_locale_from_accept_language("en-GB") == "en"


def test_accept_language_parser_returns_none_for_absent_or_unknown():
    from api.routes import _login_locale_from_accept_language

    assert _login_locale_from_accept_language(None) is None
    assert _login_locale_from_accept_language("") is None
    assert _login_locale_from_accept_language("*") is None
    # An unknown tag must not be laundered into "en" by the resolver fallback.
    assert _login_locale_from_accept_language("xx-YY") is None
    # A q=0 entry is an explicit "not acceptable".
    assert _login_locale_from_accept_language("fr;q=0, en") == "en"


# ── 4. End-to-end against the live test server ──────────────────────────────

from tests._pytest_port import BASE  # noqa: E402  (port comes from conftest)


def _get_settings():
    with urllib.request.urlopen(BASE + "/api/settings", timeout=10) as r:
        return json.loads(r.read()), r.status


def _post_settings(body):
    req = urllib.request.Request(
        BASE + "/api/settings",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()), r.status


def _get_login(accept_language=None):
    headers = {}
    if accept_language is not None:
        headers["Accept-Language"] = accept_language
    req = urllib.request.Request(BASE + "/login", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode(), r.status


def test_login_page_follows_browser_language_until_a_choice_is_made():
    settings, status = _get_settings()
    assert status == 200
    prev = settings.get("language") or ""
    try:
        # Fresh install state: nothing chosen yet.
        saved, status = _post_settings({"language": ""})
        assert status == 200
        assert saved.get("language") == ""

        html, status = _get_login("fr-CA,fr;q=0.9,en;q=0.8")
        assert status == 200
        assert 'lang="fr-FR"' in html
        assert "Se connecter" in html

        # ...and English is only the last resort, not the default.
        html, status = _get_login("xx-YY")
        assert status == 200
        assert 'lang="en"' in html
    finally:
        _post_settings({"language": prev})


def test_explicit_language_setting_overrides_accept_language():
    settings, status = _get_settings()
    assert status == 200
    prev = settings.get("language") or ""
    try:
        _post_settings({"language": "ja"})
        html, status = _get_login("fr-CA")
        assert status == 200
        assert 'lang="ja-JP"' in html
    finally:
        _post_settings({"language": prev})


def test_settings_round_trip_preserves_unset_language():
    settings, status = _get_settings()
    assert status == 200
    prev = settings.get("language") or ""
    try:
        _post_settings({"language": ""})
        reread, status = _get_settings()
        assert status == 200
        assert reread.get("language") == ""
    finally:
        _post_settings({"language": prev})
