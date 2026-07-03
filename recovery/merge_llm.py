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
    "You adapt an intended code change to a file whose surrounding code changed "
    "upstream, so the original patch no longer applies. You are given the PATCH "
    "(a unified diff of the intended change) and the CURRENT file. Emit the change "
    "as one or more SEARCH/REPLACE edits and NOTHING else, in exactly this format:\n"
    "<<<<<<< SEARCH\n"
    "<exact consecutive lines that currently exist in the file>\n"
    "=======\n"
    "<the replacement lines>\n"
    ">>>>>>> REPLACE\n"
    "Rules: each SEARCH block must match the CURRENT file EXACTLY (byte-for-byte, "
    "including indentation) and be UNIQUE. Make the smallest edits that realize the "
    "patch's intent, preserving unrelated upstream changes. Never output the whole "
    "file. No prose, no markdown fences, no commentary."
)

_SR_RE = re.compile(r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE", re.DOTALL)


def _parse_sr(text):
    return [(m.group(1), m.group(2)) for m in _SR_RE.finditer(text)]


def _apply_sr(content, blocks):
    """Apply SEARCH/REPLACE edits by exact, unique string match. Any block that
    does not match exactly once aborts the whole merge (safety)."""
    for search, replace in blocks:
        n = content.count(search)
        if n == 0:
            return None, f"SEARCH not found: {search[:70]!r}"
        if n > 1:
            return None, f"SEARCH not unique ({n}x): {search[:70]!r}"
        content = content.replace(search, replace, 1)
    return content, None


def make_merger(cfg: dict):
    def merger(comp, mod, repo):
        return merge_source_patch(cfg, comp, mod, repo)
    return merger


# --------------------------------------------------------------------------- backend health probe
_PROBE_CACHE = {"t": 0.0, "result": None}


def _do_probe(cfg):
    """Cheap availability check: load + (refresh-if-expired) Antigravity creds.
    Confirms the LLM merge backend is usable NOW without spending a completion."""
    runner = (
        "import sys;"
        f"sys.path.insert(0, {_base_dir()!r});"
        "from recovery._antigravity.antigravity import AntigravityClient;"
        "from recovery._antigravity.config import Settings;"
        "cr = AntigravityClient(Settings.from_env())._valid_credentials();"
        "print('OK' if getattr(cr, 'access_token', '') else 'NOCRED')"
    )
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if cfg.get("llm_model"):
        env["ANTIGRAVITY_PROXY_MODEL"] = cfg["llm_model"]
    try:
        r = subprocess.run([cfg["venv_python"], "-c", runner],
                           capture_output=True, text=True, timeout=60, env=env)
    except Exception as exc:
        return {"available": False, "detail": f"probe error: {exc}",
                "model": cfg.get("llm_model") or "default"}
    if r.returncode == 0 and "OK" in r.stdout:
        return {"available": True, "detail": "credentials valid/refreshable",
                "model": cfg.get("llm_model") or "default"}
    return {"available": False, "detail": (r.stderr or r.stdout or "no usable credentials").strip()[-200:],
            "model": cfg.get("llm_model") or "default"}


def probe(cfg, ttl=300):
    """Cached backend probe (avoids spawning a subprocess on every health hit)."""
    import time
    now = time.time()
    if _PROBE_CACHE["result"] is not None and (now - _PROBE_CACHE["t"]) < ttl:
        return _PROBE_CACHE["result"]
    res = _do_probe(cfg)
    _PROBE_CACHE.update(t=now, result=res)
    return res


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
        fp = os.path.join(repo, path)
        theirs = open(fp, encoding="utf-8").read() if os.path.isfile(fp) else (_show(repo, "HEAD", path) or "")
        prompt = (
            f"# PATCH (intended change, written against base {base[:8]}):\n{patch_text}\n\n"
            f"# CURRENT FILE `{path}`:\n{theirs}\n\n"
            "Emit SEARCH/REPLACE edits that apply the patch's intent to the CURRENT file."
        )
        out = _call_llm(cfg, MERGE_SYSTEM, prompt)
        blocks = _parse_sr(out)
        if not blocks:
            return {"ok": False, "method": "llm", "detail": f"no SEARCH/REPLACE edits parsed for {path}"}
        content, err = _apply_sr(theirs, blocks)
        if err:
            return {"ok": False, "method": "llm", "detail": f"edit apply failed {path}: {err}"}
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
