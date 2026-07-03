"""
Comprehensive Hermes modification-recovery engine.
===================================================

A *mod* is a declarative, self-verifying unit of change to the Hermes core tree.
The engine applies mods idempotently, verifies they are live, and re-applies
them after ``hermes update`` (git-reset) wipes the tree — independently of
Hermes itself.

Mod layout on disk (``mods.d/<id>/``):
    mod.json                 # manifest (below)
    <name>.patch             # source-patch artifacts
    files/<path>             # new-file artifacts (mirrors the target tree)

Manifest schema (mod.json):
    {
      "id": "claude-auth",
      "description": "...",
      "enabled": true,
      "components": [
        {"type": "source-patch", "patch": "anthropic_adapter.patch", "base_commit": "<sha>"},
        {"type": "new-file", "path": "agent/anthropic_billing_bypass.py"}
      ],
      "verify": {"import": "agent.anthropic_adapter", "tests": ["tests/..."], "smoke": null},
      "policy": {"apply": "auto", "merge": "git"}       # merge: git | llm ; apply: auto | manual
    }

The engine is intentionally handler-based so new component types (config edits,
runtime hooks, whole-file replaces, …) can be added without touching the
orchestration.  ``merger`` is an injected callable (Phase 2: Antigravity LLM)
used only when ``git apply`` + ``git apply --3way`` both fail.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional


# --------------------------------------------------------------------------- git helpers
def git(repo: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _sha256(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


# --------------------------------------------------------------------------- manifest load
def load_mods(mods_dir: str) -> List[Dict[str, Any]]:
    out = []
    if not os.path.isdir(mods_dir):
        return out
    for mid in sorted(os.listdir(mods_dir)):
        mpath = os.path.join(mods_dir, mid, "mod.json")
        if not os.path.isfile(mpath):
            continue
        try:
            m = json.load(open(mpath, encoding="utf-8"))
        except Exception as exc:
            out.append({"id": mid, "_error": f"bad manifest: {exc}", "enabled": False})
            continue
        m.setdefault("id", mid)
        m["_dir"] = os.path.join(mods_dir, mid)
        out.append(m)
    return out


# --------------------------------------------------------------------------- component handlers
# Each handler: is_applied(comp, mod, repo) -> bool ; apply(comp, mod, repo, merger) -> result dict
# result: {"ok": bool, "method": "clean|3way|llm|copy|noop|failed", "detail": str}

def _patch_path(mod: Dict[str, Any], comp: Dict[str, Any]) -> str:
    return os.path.join(mod["_dir"], comp["patch"])


def _sp_is_applied(comp, mod, repo) -> bool:
    patch = _patch_path(mod, comp)
    if not os.path.isfile(patch):
        return False
    # already applied  <=>  reverse-apply would succeed
    return git(repo, "apply", "--reverse", "--check", patch).returncode == 0


def _sp_apply(comp, mod, repo, merger) -> Dict[str, Any]:
    patch = _patch_path(mod, comp)
    if not os.path.isfile(patch):
        return {"ok": False, "method": "failed", "detail": f"patch missing: {patch}"}
    if _sp_is_applied(comp, mod, repo):
        return {"ok": True, "method": "noop", "detail": "already applied"}
    # 1. clean apply
    if git(repo, "apply", "--check", patch).returncode == 0:
        r = git(repo, "apply", patch)
        if r.returncode == 0:
            return {"ok": True, "method": "clean", "detail": ""}
    # 2. 3-way merge (uses blobs still in the object store)
    r3 = git(repo, "apply", "--3way", patch)
    if r3.returncode == 0 and _sp_is_applied(comp, mod, repo):
        return {"ok": True, "method": "3way", "detail": ""}
    # 3. LLM-assisted merge (Phase 2) — only if a merger is wired and policy allows
    if merger is not None and mod.get("policy", {}).get("merge") == "llm":
        try:
            res = merger(comp, mod, repo)   # returns {"ok":bool,"method":"llm","detail":..}
            return res
        except Exception as exc:
            return {"ok": False, "method": "llm", "detail": f"merge raised {exc}"}
    return {"ok": False, "method": "failed",
            "detail": f"git apply + --3way failed:\n{(r3.stderr or '')[:300]}"}


def _nf_target(comp: Dict[str, Any], repo: str) -> str:
    return os.path.join(repo, comp["path"])


def _nf_stored(comp: Dict[str, Any], mod: Dict[str, Any]) -> str:
    return os.path.join(mod["_dir"], "files", comp["path"])


def _nf_is_applied(comp, mod, repo) -> bool:
    tgt, src = _nf_target(comp, repo), _nf_stored(comp, mod)
    return os.path.isfile(tgt) and _sha256(tgt) == _sha256(src)


def _nf_apply(comp, mod, repo, merger) -> Dict[str, Any]:
    import shutil
    src, tgt = _nf_stored(comp, mod), _nf_target(comp, repo)
    if not os.path.isfile(src):
        return {"ok": False, "method": "failed", "detail": f"stored file missing: {src}"}
    if _nf_is_applied(comp, mod, repo):
        return {"ok": True, "method": "noop", "detail": "present"}
    os.makedirs(os.path.dirname(tgt), exist_ok=True)
    shutil.copy2(src, tgt)
    return {"ok": True, "method": "copy", "detail": ""}


HANDLERS = {
    "source-patch": (_sp_is_applied, _sp_apply),
    "new-file": (_nf_is_applied, _nf_apply),
}


# --------------------------------------------------------------------------- orchestration
def component_status(comp, mod, repo) -> Dict[str, Any]:
    h = HANDLERS.get(comp.get("type"))
    if not h:
        return {"type": comp.get("type"), "applied": None, "detail": "unknown type"}
    applied = h[0](comp, mod, repo)
    label = comp.get("patch") or comp.get("path") or "?"
    return {"type": comp["type"], "target": label, "applied": bool(applied)}


def mod_status(mod, repo) -> Dict[str, Any]:
    if mod.get("_error"):
        return {"id": mod["id"], "enabled": False, "applied": None, "error": mod["_error"]}
    comps = [component_status(c, mod, repo) for c in mod.get("components", [])]
    applied = all(c["applied"] for c in comps) if comps else None
    return {"id": mod["id"], "enabled": mod.get("enabled", True),
            "applied": applied, "components": comps,
            "description": mod.get("description", "")}


def heal_mod(mod, repo, merger=None) -> Dict[str, Any]:
    results = []
    for comp in mod.get("components", []):
        h = HANDLERS.get(comp.get("type"))
        if not h:
            results.append({"type": comp.get("type"), "ok": False, "method": "failed",
                            "detail": "unknown type"})
            continue
        results.append({**h[1](comp, mod, repo, merger),
                        "target": comp.get("patch") or comp.get("path")})
    return {"id": mod["id"], "ok": all(r["ok"] for r in results) if results else True,
            "results": results}


def heal(cfg: Dict[str, Any], merger=None) -> Dict[str, Any]:
    repo = cfg["hermes_agent_dir"]
    mods = [m for m in load_mods(cfg["mods_dir"]) if m.get("enabled", True)]
    healed = [heal_mod(m, repo, merger) for m in mods]
    return {"ok": all(h["ok"] for h in healed) if healed else True, "mods": healed}


def check(cfg: Dict[str, Any]) -> Dict[str, Any]:
    repo = cfg["hermes_agent_dir"]
    mods = load_mods(cfg["mods_dir"])
    statuses = [mod_status(m, repo) for m in mods]
    enabled = [s for s in statuses if s["enabled"]]
    drift = [s["id"] for s in enabled if s["applied"] is False]
    healthy = all(s["applied"] for s in enabled if s["applied"] is not None) and not any(
        s.get("error") for s in enabled)
    return {"healthy": bool(healthy), "drift": drift,
            "mod_count": len(statuses), "enabled_count": len(enabled), "mods": statuses}


# --------------------------------------------------------------------------- capture
def capture(cfg: Dict[str, Any], mod_id: str, files: Optional[List[str]] = None,
            new_files: Optional[List[str]] = None, description: str = "",
            verify: Optional[Dict[str, Any]] = None, merge: str = "git") -> Dict[str, Any]:
    """Snapshot into a mod. ``files`` restricts the source-patch to those tracked
    paths (None = all current tracked changes); ``new_files`` are untracked paths
    copied in as new-file components. This lets one logical change (e.g.
    claude-auth) be captured as its own mod, separate from unrelated edits."""
    repo = cfg["hermes_agent_dir"]
    mdir = os.path.join(cfg["mods_dir"], mod_id)
    os.makedirs(os.path.join(mdir, "files"), exist_ok=True)

    diff_args = ["diff", "--"] + list(files) if files else ["diff"]
    diff = git(repo, *diff_args).stdout
    components: List[Dict[str, Any]] = []
    if diff.strip():
        with open(os.path.join(mdir, "tracked.patch"), "w", encoding="utf-8", newline="\n") as f:
            f.write(diff)
        base = git(repo, "rev-parse", "HEAD").stdout.strip()
        components.append({"type": "source-patch", "patch": "tracked.patch", "base_commit": base})

    import shutil
    for rel in (new_files or []):
        srcp = os.path.join(repo, rel)
        if os.path.isfile(srcp):
            dstp = os.path.join(mdir, "files", rel)
            os.makedirs(os.path.dirname(dstp), exist_ok=True)
            shutil.copy2(srcp, dstp)
            components.append({"type": "new-file", "path": rel})

    manifest = {
        "id": mod_id,
        "description": description or f"captured mod {mod_id}",
        "enabled": True,
        "components": components,
        "verify": verify or {},
        "policy": {"apply": "auto", "merge": merge},
    }
    with open(os.path.join(mdir, "mod.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2)
    return {"id": mod_id, "components": components, "dir": mdir}
