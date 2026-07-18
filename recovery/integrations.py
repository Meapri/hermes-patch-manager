"""Coordinate optional provider patch installers without owning their code.

The patch manager owns update detection, ordering, and verification. Provider
repositories own their patch artifacts and installers.  Installers are invoked
with ``HERMES_PATCH_MANAGER=1`` so they must not install competing Git hooks.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


def _first_executable(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _installer(cfg: dict[str, Any], name: str) -> Path | None:
    home = Path(os.path.expanduser("~"))
    explicit = cfg.get(f"{name}_installer")
    repo = cfg.get(f"{name}_repo")
    candidates = []
    if explicit:
        candidates.append(Path(os.path.expanduser(str(explicit))))
    if repo:
        candidates.append(Path(os.path.expanduser(str(repo))) / "install.sh")
    candidates.extend(
        {
            "claude": [home / "hermes-claude-auth" / "install.sh"],
            "antigravity": [home / "hermes-google-antigravity-plugin" / "scripts" / "install.sh"],
        }.get(name, [])
    )
    return _first_executable(candidates)


def _run(installer: Path, mode: str, cfg: dict[str, Any]) -> dict[str, Any]:
    env = os.environ.copy()
    env["HERMES_PATCH_MANAGER"] = "1"
    env["HERMES_HOME"] = str(cfg["hermes_home"])
    env["HERMES_AGENT_DIR"] = str(cfg["hermes_agent_dir"])
    result = subprocess.run(
        [str(installer), mode],
        env=env,
        text=True,
        capture_output=True,
        timeout=int(cfg.get("integration_timeout", 300)),
    )
    output = (result.stdout + result.stderr).strip().splitlines()
    return {
        "installer": str(installer),
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "tail": output[-8:],
    }


def _path_exists(cfg: dict[str, Any], rel: str) -> bool:
    return (Path(cfg["hermes_agent_dir"]) / rel).is_file()


def check(cfg: dict[str, Any]) -> dict[str, Any]:
    home = Path(cfg["hermes_home"])
    venv = Path(cfg["venv_python"])
    sitecustomize = None
    if venv.is_file():
        probe = subprocess.run(
            [str(venv), "-c", "import site; print(site.getsitepackages()[0])"],
            capture_output=True, text=True, timeout=20,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            sitecustomize = Path(probe.stdout.strip()) / "sitecustomize.py"
    text = sitecustomize.read_text(encoding="utf-8", errors="replace") if sitecustomize and sitecustomize.is_file() else ""

    integrations: dict[str, Any] = {}
    claude_installer = _installer(cfg, "claude")
    if claude_installer or (home / "patches" / "anthropic_billing_bypass.py").is_file():
        integrations["claude"] = {
            "installed": (home / "patches" / "anthropic_billing_bypass.py").is_file(),
            "installer": str(claude_installer) if claude_installer else None,
            "hook": "hermes-claude-auth" in text,
        }

    ag_installer = _installer(cfg, "antigravity")
    if ag_installer or (home / "patches" / "antigravity_provider_patch.py").is_file():
        required = cfg.get("antigravity_required_files", [])
        integrations["antigravity"] = {
            "installed": (home / "patches" / "antigravity_provider_patch.py").is_file(),
            "installer": str(ag_installer) if ag_installer else None,
            "hook": "hermes-antigravity" in text,
            "missing_files": [rel for rel in required if not _path_exists(cfg, rel)],
        }

    healthy = True
    for state in integrations.values():
        healthy = healthy and bool(state.get("installed")) and bool(state.get("hook", True))
        healthy = healthy and not state.get("missing_files")
    return {"healthy": healthy, "sitecustomize": str(sitecustomize) if sitecustomize else None, "integrations": integrations}


def heal(cfg: dict[str, Any]) -> dict[str, Any]:
    results = []
    # Claude first, Antigravity second. The final installer owns its own hook
    # composition, while HPM remains the only update trigger/coordinator.
    for name, mode in (("claude", "--post-update"), ("antigravity", "--repair")):
        installer = _installer(cfg, name)
        if not installer:
            if name == "claude" and not cfg.get("require_claude", False):
                continue
            if name == "antigravity" and not cfg.get("require_antigravity", False):
                continue
            results.append({"name": name, "ok": False, "detail": "installer not found"})
            continue
        try:
            results.append({"name": name, **_run(installer, mode, cfg)})
        except Exception as exc:
            results.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
    state = check(cfg)
    if not results:
        return {"ok": state["healthy"], "results": results, "state": state}
    ok = all(item.get("ok", False) for item in results) and state["healthy"]
    return {"ok": ok, "results": results, "state": state}
