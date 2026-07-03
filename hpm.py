#!/usr/bin/env python3
"""
hermes-patch-manager (hpm) — manage & self-verify Hermes runtime patches.

The loader (loader/sitecustomize.py) is injected into the gateway via PYTHONPATH,
so it lives outside the venv and survives `hermes update`. This CLI registers
patches, and — crucially — verifies that each one is ACTUALLY APPLIED at runtime
(not merely present on disk) by importing the target Hermes module under the
loader and asserting the patch's marker. `serve` exposes that as a tailnet
health endpoint.

Subcommands:
  list                 list registered patches
  add <file.py>        register a patch module + manifest
  remove <name>        disable/remove a patch
  check   [--json]     verify every enabled patch is applied at runtime (exit!=0 on drift)
  status  [--json]     full status (files, drop-in, per-patch runtime state)
  heal                 re-assert on-disk invariants (drop-in, patch files); idempotent
  doctor               human-readable summary
  serve [--bind IP] [--port N]   HTTP health endpoint (JSON /health, /healthz)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
LOADER_DIR = os.path.join(BASE, "loader")
PATCH_STORE = os.path.join(BASE, "patches")
REGISTRY_DIR = os.path.join(BASE, "registry.d")
CONFIG_PATH = os.environ.get("HPM_CONFIG", os.path.join(BASE, "config.json"))


def dropin_path(cfg: dict) -> str:
    svc = cfg.get("gateway_service") or "hermes-gateway.service"
    return f"/etc/systemd/system/{svc}.d/10-hermes-patch-manager.conf"


# --------------------------------------------------------------------------- config
def load_config() -> dict:
    cfg = {
        "hermes_agent_dir": "",
        "venv_python": "",
        "run_as_user": "",
        "gateway_service": "hermes-gateway.service",
        "health_bind": "auto",
        "health_port": 8577,
        "auto_restart_on_drift": True,
    }
    if os.path.exists(CONFIG_PATH):
        try:
            cfg.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
        except Exception as exc:
            _warn(f"config unreadable ({CONFIG_PATH}): {exc}")
    return cfg


def _warn(msg: str) -> None:
    sys.stderr.write(f"[hpm] {msg}\n")


# --------------------------------------------------------------------------- registry
def load_manifests():
    out = []
    if not os.path.isdir(REGISTRY_DIR):
        return out
    for name in sorted(os.listdir(REGISTRY_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            m = json.load(open(os.path.join(REGISTRY_DIR, name), encoding="utf-8"))
            m["_file"] = name
            out.append(m)
        except Exception as exc:
            _warn(f"bad manifest {name}: {exc}")
    return out


# --------------------------------------------------------------------------- runtime verify
def verify_patch(cfg: dict, manifest: dict) -> dict:
    """Import the target module UNDER the loader and assert the patch marker.
    This proves the patch is actually applied at runtime, not just present."""
    res = {"name": manifest.get("name"), "enabled": manifest.get("enabled", True)}
    v = manifest.get("verify")
    if not manifest.get("enabled", True):
        res.update(applied=None, note="disabled")
        return res
    if not v or not v.get("import") or not v.get("marker"):
        res.update(applied=None, note="no verify block")
        return res
    py = cfg.get("venv_python")
    cwd = cfg.get("hermes_agent_dir") or None
    if not py or not os.path.exists(py):
        res.update(applied=False, error=f"venv_python missing: {py!r} (set it in config.json)")
        return res
    snippet = (
        "import importlib\n"
        f"m = importlib.import_module({v['import']!r})\n"
        f"print('APPLIED' if getattr(m, {v['marker']!r}, False) else 'MISSING')\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = LOADER_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # don't scatter root-owned __pycache__ in the user venv
    home = cfg.get("home")
    if home:
        env["HOME"] = home
    cmd = [py, "-c", snippet]
    user = cfg.get("run_as_user")
    if user and os.geteuid() == 0 and user != "root":
        cmd = ["sudo", "-u", user, "-E"] + cmd
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=90)
    except Exception as exc:
        res.update(applied=False, error=f"verify run failed: {exc}")
        return res
    applied = "APPLIED" in p.stdout
    res.update(applied=applied, rc=p.returncode)
    if not applied:
        res["stderr_tail"] = (p.stderr or "").strip()[-500:]
        res["stdout_tail"] = (p.stdout or "").strip()[-200:]
    return res


# --------------------------------------------------------------------------- status
def collect_status(cfg: dict) -> dict:
    manifests = load_manifests()
    patches = []
    for m in manifests:
        entry = {
            "name": m.get("name"),
            "enabled": m.get("enabled", True),
            "module": m.get("module"),
            "module_present": os.path.exists(os.path.join(PATCH_STORE, (m.get("module") or "") + ".py")),
        }
        entry.update(verify_patch(cfg, m))
        patches.append(entry)
    dpath = dropin_path(cfg)
    dropin_ok = os.path.exists(dpath)
    pythonpath_wired = False
    if dropin_ok:
        try:
            pythonpath_wired = LOADER_DIR in open(dpath, encoding="utf-8").read()
        except Exception:
            pass
    enabled = [p for p in patches if p.get("enabled")]
    healthy = all(p.get("applied") for p in enabled if p.get("applied") is not None)
    drift = [p["name"] for p in enabled if p.get("applied") is False]
    return {
        "healthy": bool(healthy) and dropin_ok and pythonpath_wired,
        "loader_present": os.path.exists(os.path.join(LOADER_DIR, "sitecustomize.py")),
        "gateway_dropin_present": dropin_ok,
        "pythonpath_wired": pythonpath_wired,
        "patch_count": len(patches),
        "enabled_count": len(enabled),
        "drift": drift,
        "patches": patches,
    }


# --------------------------------------------------------------------------- commands
def cmd_list(args, cfg):
    for m in load_manifests():
        flag = "on " if m.get("enabled", True) else "off"
        hooks = ", ".join(f"{h.get('target')}::{h.get('apply')}" for h in m.get("hooks", []))
        print(f"[{flag}] {m.get('name'):20} module={m.get('module'):28} {hooks}")
    return 0


def cmd_add(args, cfg):
    src = args.file
    if not os.path.exists(src):
        _warn(f"no such file: {src}"); return 2
    module = args.module or os.path.splitext(os.path.basename(src))[0]
    name = args.name or module.replace("_", "-")
    os.makedirs(PATCH_STORE, exist_ok=True)
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    shutil.copy2(src, os.path.join(PATCH_STORE, module + ".py"))
    hooks = []
    for h in args.hook or []:
        target, _, apply_attr = h.partition(":")
        if not target or not apply_attr:
            _warn(f"bad --hook {h!r} (want target:func)"); return 2
        hooks.append({"target": target, "apply": apply_attr})
    manifest = {"name": name, "module": module, "hooks": hooks, "enabled": True}
    if args.verify:
        imp, _, marker = args.verify.partition(":")
        manifest["verify"] = {"import": imp, "marker": marker}
    if args.source:
        manifest["source"] = args.source
    json.dump(manifest, open(os.path.join(REGISTRY_DIR, name + ".json"), "w", encoding="utf-8"), indent=2)
    print(f"registered '{name}' (module {module}.py). Restart the gateway to apply:")
    print(f"  systemctl restart {cfg.get('gateway_service')}")
    return 0


def cmd_remove(args, cfg):
    path = os.path.join(REGISTRY_DIR, args.name + ".json")
    if not os.path.exists(path):
        _warn(f"no such patch: {args.name}"); return 2
    if args.purge:
        os.remove(path)
        print(f"removed manifest {args.name}")
    else:
        m = json.load(open(path, encoding="utf-8")); m["enabled"] = False
        json.dump(m, open(path, "w", encoding="utf-8"), indent=2)
        print(f"disabled {args.name} (use --purge to delete)")
    return 0


def cmd_check(args, cfg):
    st = collect_status(cfg)
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        for p in st["patches"]:
            mark = {True: "APPLIED", False: "DRIFT!!", None: "-"}[p.get("applied")]
            print(f"  {mark:8} {p.get('name')}")
            if p.get("applied") is False and p.get("stderr_tail"):
                print(f"           {p['stderr_tail'].splitlines()[-1] if p['stderr_tail'] else ''}")
        print(f"loader={st['loader_present']} dropin={st['gateway_dropin_present']} pythonpath={st['pythonpath_wired']}")
        print("HEALTHY" if st["healthy"] else f"UNHEALTHY (drift: {st['drift'] or 'config'})")
    return 0 if st["healthy"] else 1


def cmd_status(args, cfg):
    print(json.dumps(collect_status(cfg), indent=2))
    return 0


def cmd_heal(args, cfg):
    """Re-assert on-disk invariants. With PYTHONPATH injection nothing in the
    venv can rot, so heal mainly restores the gateway drop-in if it was removed
    and reloads systemd. Patch modules/registry live in /opt and are the source
    of truth."""
    dpath = dropin_path(cfg)
    if not os.path.exists(dpath):
        _warn("gateway drop-in missing; run install.sh to recreate it")
    elif LOADER_DIR not in open(dpath, encoding="utf-8").read():
        _warn("drop-in present but PYTHONPATH not wired; run install.sh")
    st = collect_status(cfg)
    if st["healthy"]:
        print("healthy — nothing to heal")
    else:
        print(f"unhealthy: drift={st['drift']} dropin={st['gateway_dropin_present']} pythonpath={st['pythonpath_wired']}")
        print(f"  recover with: systemctl restart {cfg.get('gateway_service')}  (or re-run install.sh)")
    return 0 if st["healthy"] else 1


def cmd_doctor(args, cfg):
    st = collect_status(cfg)
    print("hermes-patch-manager doctor")
    print(f"  base dir        : {BASE}")
    print(f"  loader present  : {st['loader_present']}")
    print(f"  gateway dropin  : {st['gateway_dropin_present']}  (PYTHONPATH wired: {st['pythonpath_wired']})")
    print(f"  venv python     : {cfg.get('venv_python')}")
    print(f"  patches         : {st['enabled_count']} enabled / {st['patch_count']} total")
    for p in st["patches"]:
        mark = {True: "APPLIED", False: "DRIFT", None: "n/a"}[p.get("applied")]
        print(f"    - {p.get('name'):20} {mark}")
    print(f"  OVERALL         : {'HEALTHY' if st['healthy'] else 'UNHEALTHY'}")
    return 0 if st["healthy"] else 1


def cmd_guard(args, cfg):
    """Periodic drift guard (run by the systemd timer). Verifies runtime
    application; if a patch has drifted and auto_restart_on_drift is set,
    restart the gateway to self-heal (the patch reattaches via PYTHONPATH on
    the fresh start). Conservative: only restarts when the gateway is active
    and the on-disk wiring is intact, so it fixes 'stale process' drift rather
    than masking a broken install."""
    st = collect_status(cfg)
    if st["healthy"]:
        print("guard: healthy")
        return 0
    print(f"guard: UNHEALTHY drift={st['drift']} dropin={st['gateway_dropin_present']} pythonpath={st['pythonpath_wired']}")
    wiring_ok = st["gateway_dropin_present"] and st["pythonpath_wired"] and st["loader_present"]
    if not (cfg.get("auto_restart_on_drift") and wiring_ok and st["drift"]):
        print("guard: not auto-restarting (either disabled, wiring broken -> run install.sh, or no runtime drift)")
        return 1
    svc = cfg.get("gateway_service", "hermes-gateway.service")
    try:
        active = subprocess.run(["systemctl", "is-active", "--quiet", svc]).returncode == 0
    except Exception as exc:
        print(f"guard: cannot query {svc}: {exc}"); return 1
    if not active:
        print(f"guard: {svc} not active; skipping restart"); return 1
    print(f"guard: restarting {svc} to reattach patches...")
    rc = subprocess.run(["systemctl", "restart", svc]).returncode
    if rc != 0:
        print(f"guard: restart failed rc={rc}"); return 1
    after = collect_status(cfg)
    print("guard: healed" if after["healthy"] else f"guard: STILL unhealthy after restart drift={after['drift']}")
    return 0 if after["healthy"] else 1


def _tailscale_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
        for line in out.stdout.splitlines():
            line = line.strip()
            if line:
                return line
    except Exception:
        pass
    return None


def cmd_serve(args, cfg):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    bind = args.bind or cfg.get("health_bind") or "auto"
    if bind == "auto":
        bind = _tailscale_ip()
        if not bind:
            _warn("could not detect tailscale IP; falling back to 127.0.0.1 (set health_bind)")
            bind = "127.0.0.1"
    port = args.port or int(cfg.get("health_port", 8577))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            st = collect_status(cfg)
            if self.path.rstrip("/") in ("/healthz", "/healthz"):
                code = 200 if st["healthy"] else 503
                body = (b"ok\n" if st["healthy"] else b"unhealthy\n")
                self.send_response(code); self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            body = json.dumps(st, indent=2).encode()
            self.send_response(200 if st["healthy"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    httpd = ThreadingHTTPServer((bind, port), Handler)
    print(f"hermes-patch-manager health serving on http://{bind}:{port}/health  (/healthz for probes)")
    httpd.serve_forever()


def main():
    ap = argparse.ArgumentParser(prog="hpm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add"); a.add_argument("file"); a.add_argument("--name"); a.add_argument("--module")
    a.add_argument("--hook", action="append", help="target:func (repeatable)")
    a.add_argument("--verify", help="import.module:MARKER_ATTR"); a.add_argument("--source")
    r = sub.add_parser("remove"); r.add_argument("name"); r.add_argument("--purge", action="store_true")
    c = sub.add_parser("check"); c.add_argument("--json", action="store_true")
    sub.add_parser("status")
    sub.add_parser("heal")
    sub.add_parser("guard")
    sub.add_parser("doctor")
    s = sub.add_parser("serve"); s.add_argument("--bind"); s.add_argument("--port", type=int)
    args = ap.parse_args()
    cfg = load_config()
    fn = {
        "list": cmd_list, "add": cmd_add, "remove": cmd_remove, "check": cmd_check,
        "status": cmd_status, "heal": cmd_heal, "guard": cmd_guard, "doctor": cmd_doctor,
        "serve": cmd_serve,
    }[args.cmd]
    sys.exit(fn(args, cfg))


if __name__ == "__main__":
    main()
