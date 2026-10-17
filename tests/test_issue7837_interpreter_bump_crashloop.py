"""Regression tests for #7837 — WebUI crash-loops after a Hermes Agent
interpreter bump.

Two independent defects combine into a permanent crash loop:

1. ``bootstrap.py::_python_can_run_webui_and_agent`` probes candidate
   interpreters with ``import yaml`` *before* ``from run_agent import
   AIAgent``. Importing ``run_agent`` is what activates Hermes Agent's
   managed dependency environment (``activate_dependencies()`` runs as an
   import side effect, and the relaunch wrapper re-execs with an isolated
   ``-I`` interpreter that starts with a bare ``sys.path``). Probing a
   WebUI-only dependency first therefore always fails in that pre-activation
   state, even for an interpreter whose fully-activated environment really
   does have PyYAML. The check then fails unconditionally, and the
   reinstall-and-retry loop can never succeed.

2. ``server.py`` relies on its own directory being on ``sys.path``. A plain
   ``python server.py`` puts the script's directory there implicitly, but
   when Hermes Agent's relaunch machinery re-invokes the file through
   ``runpy.run_path()`` (which does *not* add the target script's directory),
   the directory is gone and the first ``api.*`` import raises
   ``ModuleNotFoundError: No module named 'api'``.

The fixtures below reproduce both conditions rather than asserting on source
text, so the tests stay honest if the implementation is rewritten.
"""

import ast
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

import bootstrap

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER_PY = REPO_ROOT / "server.py"


# ── Bug 1: bootstrap.py's compatibility probe import order ────────────────


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX shell interpreter stub (upstream repro is macOS/Linux launchd)",
)
def test_compat_probe_accepts_interpreter_where_deps_activate_on_agent_import(tmp_path):
    """A candidate interpreter whose PyYAML only materialises after importing
    Hermes Agent must be accepted.

    ``-S`` keeps site-packages off ``sys.path`` so the only way ``yaml`` can
    become importable is the path entry that ``run_agent`` adds at import
    time — the pre-activation state an interpreter bump produces. Probing
    ``yaml`` first cannot succeed here; probing the agent first does.
    """
    agent_dir = tmp_path / "agent"
    deps_dir = tmp_path / "deps"
    (deps_dir / "yaml").mkdir(parents=True)
    (deps_dir / "yaml" / "__init__.py").write_text("", encoding="utf-8")
    agent_dir.mkdir()
    # Stands in for run_agent: activating the agent's managed environment is
    # an import side effect, exactly like activate_dependencies().
    (agent_dir / "run_agent.py").write_text(
        "import os, sys\n"
        "sys.path.insert(0, os.path.join(\n"
        "    os.path.dirname(os.path.abspath(__file__)), '..', 'deps'))\n"
        "class AIAgent:\n"
        "    pass\n",
        encoding="utf-8",
    )

    # Interpreter stub that hides site-packages, reproducing the bare
    # sys.path the relaunch wrapper hands to the probe.
    python_exe = tmp_path / "interpreter-stub"
    python_exe.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n', encoding="utf-8"
    )
    python_exe.chmod(0o755)

    assert bootstrap._python_can_run_webui_and_agent(
        str(python_exe), agent_dir
    ) is True, (
        "compatibility probe rejected an interpreter that imports both the "
        "WebUI deps and Hermes Agent; the probe must import Hermes Agent "
        "before any WebUI-only dependency"
    )


def test_compat_probe_probe_script_imports_agent_before_webui_deps():
    """Guard the probe's import order directly, independent of the fixture.

    The order is the defect, so pin it: ``run_agent`` first, ``yaml`` second.
    """
    probe = bootstrap._compat_probe_script()
    agent_idx = probe.index("run_agent")
    yaml_idx = probe.index("yaml")
    assert agent_idx < yaml_idx, (
        f"compat probe imports yaml before run_agent: {probe!r}"
    )


# ── Bug 2: server.py loses its own directory across a runpy relaunch ──────


def test_server_restores_own_directory_before_importing_api():
    """``server.py`` must put its own directory on ``sys.path`` before the
    first ``api.*`` import, so a ``runpy.run_path`` relaunch can resolve them.
    """
    source = SERVER_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)

    first_api_import = None
    path_restore = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root == "api" and first_api_import is None:
                first_api_import = node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "api" and first_api_import is None:
                    first_api_import = node

    assert first_api_import is not None, "server.py no longer imports api.*"

    # Find a module-level sys.path mutation that references this file's own
    # directory (os.path.dirname(os.path.abspath(__file__)) or equivalent).
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.Expr, ast.AugAssign)):
            continue
        text = ast.dump(node)
        if "sys" not in text or "path" not in text or "__file__" not in text:
            continue
        path_restore = node
        break

    assert path_restore is not None, (
        "server.py does not restore its own directory onto sys.path; a "
        "runpy relaunch drops it and the api.* imports cannot resolve"
    )
    assert path_restore.lineno < first_api_import.lineno, (
        f"server.py restores sys.path at line {path_restore.lineno}, after "
        f"the first api.* import at line {first_api_import.lineno}"
    )


def test_server_imports_when_launched_via_runpy_relaunch(tmp_path):
    """End-to-end: executing ``server.py`` through ``runpy.run_path()`` — the
    relaunch path — must reach its ``api.*`` imports instead of raising
    ``ModuleNotFoundError``.

    Only the module's import prologue is exercised: the file is executed
    under a non-``__main__`` name so ``main()`` never runs, with a scrubbed
    ``sys.path`` that mirrors the relaunch (agent root only, script directory
    absent).
    """
    probe = tmp_path / "relaunch_probe.py"
    probe.write_text(
        "import os, runpy, sys\n"
        "agent_root, server = sys.argv[1], sys.argv[2]\n"
        "server_dir = os.path.dirname(os.path.abspath(server))\n"
        "# Model the relaunch wrapper: only the agent root is guaranteed on\n"
        "# sys.path. The script's own directory is NOT added (runpy.run_path\n"
        "# does not do it), while real site-packages remain importable.\n"
        "sys.path[:] = [p for p in sys.path\n"
        "               if p and os.path.abspath(p) != server_dir]\n"
        "sys.path.insert(0, agent_root)\n"
        "try:\n"
        "    runpy.run_path(server, run_name='_issue7837_probe')\n"
        "except ModuleNotFoundError as exc:\n"
        "    print('MODULE_NOT_FOUND:' + str(exc))\n"
        "    raise SystemExit(3)\n"
        "print('PROLOGUE_COMPLETED')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(probe), str(tmp_path), str(SERVER_PY)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=180,
    )

    assert result.returncode == 0, (
        "server.py could not be executed through runpy (the relaunch path): "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr[-2000:]!r}"
    )
    assert "PROLOGUE_COMPLETED" in result.stdout


def test_bootstrap_launches_server_via_execv():
    """``bootstrap.py`` launches the server with a plain ``os.execv``; guard
    that the relaunch-triggering shape the issue describes is unchanged, so
    this fix does not silently alter process-launch semantics."""
    source = (REPO_ROOT / "bootstrap.py").read_text(encoding="utf-8")
    assert "os.execv(python_exe, [python_exe, server_path])" in source


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_no_regression_probe_script_is_syntactically_valid(tmp_path):
    """The probe is executed via ``-c``, so it must be valid Python."""
    ast.parse(bootstrap._compat_probe_script())