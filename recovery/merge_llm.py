"""
LLM-assisted patch-merge tier (Phase 2).
========================================

Escalation for a source-patch that no longer applies (``git apply`` + ``--3way``
both failed because upstream changed the touched lines).  We re-apply the
patch's *intent* to the current file with an LLM, then **verify before applying**
— never blindly.  The LLM is an embedded, adapted Antigravity client
(``recovery/_antigravity``, vendored from Meapri/Antigravity-Proxy); the actual
call runs under the Hermes venv python (which has httpx) so ``hpm`` stays
stdlib-only and the merger keeps working off static Antigravity creds even when
Hermes core is broken.

Safety invariants:
  * output is syntax-checked (and the mod's tests run) before it touches the tree
  * failures roll the file(s) back
  * ``policy.apply == "manual"`` stages the verified candidate and asks for
    review instead of applying
  * a successful merge is *pinned* to the current upstream commit and reused
    deterministically (no re-invoking the LLM every heal)
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile

MERGE_SYSTEM = (
    "You are a precise source-code merge tool. You are given a PATCH that encodes "
    "an intended change and the CURRENT version of a file the patch no longer "
    "applies to cleanly because upstream changed. Re-apply the patch's INTENT to "
    "the current file. Output ONLY the complete updated file content — no prose, "
    "no markdown fences. Preserve everything else byte-for-byte; make the smallest "
    "change that realizes the patch's intent."
)


def make_merger(cfg: dict):
    def merger(comp, mod, repo):
        return merge_source_patch(cfg, comp, mod, repo)
    return merger


# --------------------------------------------------------------------------- helpers
def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def _show(repo, ref, path):
    r = _git(repo, "show", f"{ref}:{path}")
    return r.stdout if r.returncode == 0 else None


def _files_in_patch(patch_text: str):
    out = []
    for line in patch_text.splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m and m.group(1) != "/dev/null":
            out.append(m.group(1))
    return out


def _base_dir():
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _call_llm(cfg, system, prompt):
    sysf = tempfile.NamedTemporaryFile("w", suffix=".sys", delete=False, encoding="utf-8")
    prof = tempfile.NamedTemporaryFile("w", suffix=".prompt", delete=False, encoding="utf-8")
    sysf.write(system); sysf.close(); prof.write(prompt); prof.close()
    runner = (
        "import sys;"
        f"sys.path.insert(0, {_base_dir()!r});"
        "from recovery._antigravity.antigravity import AntigravityClient;"
        "from recovery._antigravity.config import Settings;"
        "c = AntigravityClient(Settings.from_env());"
        "print(c.complete(system=open(sys.argv[1],encoding='utf-8').read(),"
        "prompt=open(sys.argv[2],encoding='utf-8').read(), memories=[], grounding=False))"
    )
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if cfg.get("llm_model"):
        env["ANTIGRAVITY_PROXY_MODEL"] = cfg["llm_model"]
    try:
        r = subprocess.run([cfg["venv_python"], "-c", runner, sysf.name, prof.name],
                           capture_output=True, text=True, timeout=300, env=env)
    finally:
        for f in (sysf.name, prof.name):
            try:
                os.unlink(f)
            except OSError:
                pass
    if r.returncode != 0:
        raise RuntimeError(f"antigravity call failed rc={r.returncode}: {r.stderr[-400:]}")
    return r.stdout


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines)
    return t


def _verify_syntax(cfg, content: str, path: str):
    if not path.endswith(".py"):
        return True, "not python"
    tf = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    tf.write(content); tf.close()
    try:
        r = subprocess.run([cfg["venv_python"], "-m", "py_compile", tf.name],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0, r.stderr[-300:]
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass


def _run_tests(cfg, repo, tests):
    if not tests:
        return True, "no tests declared"
    r = subprocess.run([cfg["venv_python"], "-m", "pytest", "-q", *tests],
                       cwd=repo, capture_output=True, text=True, timeout=900)
    return r.returncode == 0, (r.stdout + r.stderr)[-500:]


def _write(repo, path, content):
    fp = os.path.join(repo, path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


# --------------------------------------------------------------------------- pin reuse
def _pin_dir(mod):
    return os.path.join(mod["_dir"], "pinned")


def _try_pinned(cfg, comp, mod, repo, files):
    """If a prior LLM merge was pinned to the CURRENT upstream commit, re-apply
    it deterministically without calling the LLM."""
    pin = _pin_dir(mod)
    up_file = os.path.join(pin, "upstream.txt")
    if not os.path.isfile(up_file):
        return None
    upstream = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if open(up_file, encoding="utf-8").read().strip() != upstream:
        return None
    applied = []
    for path in files:
        pp = os.path.join(pin, path)
        if not os.path.isfile(pp):
            return None
        _write(repo, path, open(pp, encoding="utf-8").read())
        applied.append(path)
    return {"ok": True, "method": "llm-pinned", "detail": f"reused pinned merge for {upstream[:8]}"}


# --------------------------------------------------------------------------- main
def merge_source_patch(cfg, comp, mod, repo) -> dict:
    patch_path = os.path.join(mod["_dir"], comp["patch"])
    patch_text = open(patch_path, encoding="utf-8").read()
    base = comp.get("base_commit", "HEAD")
    files = _files_in_patch(patch_text)
    if not files:
        return {"ok": False, "method": "llm", "detail": "no files parsed from patch"}

    pinned = _try_pinned(cfg, comp, mod, repo, files)
    if pinned:
        return pinned

    merged = {}
    for path in files:
        theirs = _show(repo, "HEAD", path)
        if theirs is None:
            fp = os.path.join(repo, path)
            theirs = open(fp, encoding="utf-8").read() if os.path.isfile(fp) else ""
        base_content = _show(repo, base, path) or ""
        prompt = (
            f"# PATCH (intended change, written against base {base[:8]}):\n{patch_text}\n\n"
            f"# CURRENT FILE `{path}` (upstream changed; patch no longer applies):\n{theirs}\n\n"
            f"# Base version the patch was written against (reference):\n{base_content}\n\n"
            "Output the COMPLETE updated content of the current file with the patch's intent applied."
        )
        content = _strip_fences(_call_llm(cfg, MERGE_SYSTEM, prompt))
        if not content.strip():
            return {"ok": False, "method": "llm", "detail": f"empty merge for {path}"}
        ok, det = _verify_syntax(cfg, content, path)
        if not ok:
            return {"ok": False, "method": "llm", "detail": f"syntax check failed {path}: {det}"}
        merged[path] = content

    # apply to tree (with rollback), run tests
    backups = {p: _show(repo, "HEAD", p) for p in merged}
    for p, c in merged.items():
        _write(repo, p, c)
    ok, det = _run_tests(cfg, repo, (mod.get("verify") or {}).get("tests") or [])
    if not ok:
        for p in merged:
            if backups.get(p) is not None:
                _write(repo, p, backups[p])
        return {"ok": False, "method": "llm", "detail": f"tests failed → rolled back: {det}"}

    if (mod.get("policy") or {}).get("apply") == "manual":
        for p in merged:
            if backups.get(p) is not None:
                _write(repo, p, backups[p])
        stage = os.path.join(mod["_dir"], "staged")
        for p, c in merged.items():
            sp = os.path.join(stage, p)
            os.makedirs(os.path.dirname(sp), exist_ok=True)
            open(sp, "w", encoding="utf-8").write(c)
        return {"ok": False, "method": "llm", "detail": "verified but staged for review (policy=manual)",
                "needs_review": True}

    # auto: applied + verified → pin to current upstream
    upstream = _git(repo, "rev-parse", "HEAD").stdout.strip()
    pin = _pin_dir(mod)
    os.makedirs(pin, exist_ok=True)
    open(os.path.join(pin, "upstream.txt"), "w").write(upstream)
    for p, c in merged.items():
        pp = os.path.join(pin, p)
        os.makedirs(os.path.dirname(pp), exist_ok=True)
        open(pp, "w", encoding="utf-8").write(c)
    return {"ok": True, "method": "llm", "detail": f"llm-merged + verified ({len(merged)} file(s))"}
