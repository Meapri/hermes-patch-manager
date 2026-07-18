#!/usr/bin/env python3
"""
hpm — Hermes modification-recovery manager.

Recovers arbitrary Hermes core modifications (source patches + new files) after
`hermes update` (git-reset + venv rebuild) wipes them, independently of Hermes.
Modifications are declarative *mods* under mods.d/; the engine applies them
idempotently (git apply -> --3way -> [LLM merge, Phase 2]), verifies they are
live, and restarts the configured services so a running gateway reloads the
re-applied source.

  list                 list registered mods + applied state
  capture <id>         snapshot current git diff + new files into a mod
  heal                 re-apply all mods idempotently, then restart services if anything changed
  check   [--json]     verify every enabled mod is applied (exit!=0 on drift)
  status               full JSON status
  doctor               human summary
  serve [--bind IP]    tailnet health endpoint (/health, /healthz)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BASE)
from recovery import engine  # noqa: E402
from recovery import integrations  # noqa: E402

CONFIG_PATH = os.environ.get("HPM_CONFIG", os.path.join(BASE, "config.json"))


def load_config() -> dict:
    cfg = {
        "hermes_agent_dir": "",
        "hermes_home": os.path.expanduser("~/.hermes"),
        "venv_python": "",
        "mods_dir": os.path.join(BASE, "mods.d"),
        "mode": "user",                       # user | system  (systemctl scope)
        "restart_services": ["hermes-gateway.service"],
        "health_bind": "auto",
        "health_port": 8577,
        "capture_new_files": [],
        "capture_verify": {},
    }
    if os.path.exists(CONFIG_PATH):
        try:
            cfg.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
        except Exception as exc:
            sys.stderr.write(f"[hpm] config unreadable: {exc}\n")
    cfg["mods_dir"] = cfg.get("mods_dir") or os.path.join(BASE, "mods.d")
    cfg["hermes_home"] = os.path.expanduser(cfg.get("hermes_home") or os.path.join(os.path.expanduser("~"), ".hermes"))
    cfg["hermes_agent_dir"] = cfg.get("hermes_agent_dir") or os.path.join(cfg["hermes_home"], "hermes-agent")
    cfg["venv_python"] = cfg.get("venv_python") or os.path.join(cfg["hermes_agent_dir"], "venv", "bin", "python")
    return cfg


def _sctl(cfg):
    return ["systemctl", "--user"] if cfg.get("mode") == "user" else ["systemctl"]


def _restart_services(cfg):
    out = []
    for svc in cfg.get("restart_services", []):
        rc = subprocess.run(_sctl(cfg) + ["restart", svc]).returncode
        out.append((svc, rc == 0))
    return out


# --------------------------------------------------------------------------- commands
def cmd_list(args, cfg):
    for s in engine.check(cfg)["mods"]:
        mark = {True: "APPLIED", False: "DRIFT ", None: "  ?   "}[s.get("applied")]
        flag = "on " if s.get("enabled") else "off"
        print(f"[{flag}] {mark}  {s['id']:20} {s.get('description','')[:50]}")
    return 0


def cmd_capture(args, cfg):
    files = [f.strip() for f in args.files.split(",") if f.strip()] if args.files else None
    new_files = [f.strip() for f in args.new_files.split(",") if f.strip()] if args.new_files else None
    r = engine.capture(cfg, args.id, files=files, new_files=new_files,
                       description=args.description or "", merge=args.merge)
    print(f"captured mod '{r['id']}' -> {r['dir']}")
    for c in r["components"]:
        print(f"  {c['type']}: {c.get('patch') or c.get('path')}")
    if not r["components"]:
        print("  (no components — nothing to capture; check --files/--new-files)")
    return 0


def cmd_heal(args, cfg):
    merger = _load_merger(cfg)
    before = engine.check(cfg)
    res = engine.heal(cfg, merger=merger)
    integration = integrations.heal(cfg)
    changed = any(
        r.get("method") not in (None, "noop") and r.get("ok")
        for m in res["mods"] for r in m["results"]
    )
    for m in res["mods"]:
        for r in m["results"]:
            if r.get("method") not in ("noop",):
                print(f"  {m['id']}: {r.get('target')} -> {r.get('method')} {'ok' if r['ok'] else 'FAIL: '+r.get('detail','')}")
    for item in integration["results"]:
        print(f"  integration {item['name']}: {'ok' if item.get('ok') else 'FAILED'}")
    changed = changed or any(item.get("ok") for item in integration["results"])
    if changed and not args.no_restart:
        for svc, ok in _restart_services(cfg):
            print(f"  restart {svc}: {'ok' if ok else 'FAILED'}")
    after = engine.check(cfg)
    integration_after = integrations.check(cfg)
    print("HEALTHY" if after["healthy"] and integration_after["healthy"] else f"UNHEALTHY drift={after['drift']}")
    return 0 if after["healthy"] and integration_after["healthy"] else 1


def cmd_check(args, cfg):
    st = _full_status(cfg)
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        for s in st["mods"]:
            mark = {True: "APPLIED", False: "DRIFT!!", None: "  ?  "}[s.get("applied")]
            print(f"  {mark}  {s['id']}")
            for c in s.get("components", []):
                if not c["applied"]:
                    print(f"           - {c['type']} {c.get('target')} NOT applied")
        print(_llm_line(st))
        print("HEALTHY" if st["healthy"] else f"UNHEALTHY (drift: {st['drift']})")
    return 0 if st["healthy"] else 1


def cmd_status(args, cfg):
    print(json.dumps(_full_status(cfg), indent=2))
    return 0


def cmd_doctor(args, cfg):
    st = _full_status(cfg)
    print("hermes modification-recovery — doctor")
    print(f"  repo   : {cfg['hermes_agent_dir']}")
    print(f"  mods   : {st['enabled_count']} enabled / {st['mod_count']} total")
    for s in st["mods"]:
        mark = {True: "APPLIED", False: "DRIFT", None: "n/a"}[s.get("applied")]
        print(f"    - {s['id']:20} {mark}")
    print(_llm_line(st))
    print(f"  OVERALL: {'HEALTHY' if st['healthy'] and st.get('integrations', {}).get('healthy', True) else 'UNHEALTHY'}")
    return 0 if st["healthy"] else 1


def _full_status(cfg):
    st = engine.check(cfg)
    st["repo"] = cfg["hermes_agent_dir"]
    st["integrations"] = integrations.check(cfg)
    st["healthy"] = st["healthy"] and st["integrations"]["healthy"]
    if cfg.get("llm_merge"):
        try:
            from recovery.merge_llm import probe
            st["llm_merge"] = probe(cfg)
        except Exception as exc:
            st["llm_merge"] = {"available": None, "detail": f"probe unavailable: {exc}"}
    else:
        st["llm_merge"] = {"available": None, "detail": "disabled"}
    return st


def _llm_line(st):
    llm = st.get("llm_merge") or {}
    label = {True: "available", False: "EXPIRED / UNAVAILABLE — re-auth Antigravity",
             None: "disabled"}[llm.get("available")]
    return f"  llm_merge: {label}" + (f"  (model={llm.get('model')})" if llm.get("model") else "")


def _load_merger(cfg):
    """Phase 2: return an LLM merge callable, or None if not configured."""
    if not cfg.get("llm_merge"):
        return None
    try:
        from recovery.merge_llm import make_merger
        return make_merger(cfg)
    except Exception as exc:
        sys.stderr.write(f"[hpm] LLM merger unavailable: {exc}\n")
        return None


def _tailscale_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
        for line in out.stdout.splitlines():
            if line.strip():
                return line.strip()
    except Exception:
        pass
    return None


def cmd_serve(args, cfg):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    bind = args.bind or cfg.get("health_bind") or "auto"
    if bind == "auto":
        bind = _tailscale_ip() or "127.0.0.1"
    port = args.port or int(cfg.get("health_port", 8577))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            st = _full_status(cfg)
            if self.path.rstrip("/") == "/healthz":
                body = b"ok\n" if st["healthy"] else b"unhealthy\n"
                self.send_response(200 if st["healthy"] else 503)
                self.send_header("Content-Type", "text/plain")
            else:
                body = json.dumps(st, indent=2).encode()
                self.send_response(200 if st["healthy"] else 503)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"recovery health on http://{bind}:{port}/health")
    ThreadingHTTPServer((bind, port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser(prog="hpm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    c = sub.add_parser("capture"); c.add_argument("id"); c.add_argument("--description")
    c.add_argument("--files", help="comma-separated tracked paths (default: all changes)")
    c.add_argument("--new-files", dest="new_files", help="comma-separated untracked paths to store")
    c.add_argument("--merge", default="git", choices=["git", "llm"], help="conflict policy")
    h = sub.add_parser("heal"); h.add_argument("--no-restart", action="store_true")
    ck = sub.add_parser("check"); ck.add_argument("--json", action="store_true")
    sub.add_parser("status"); sub.add_parser("doctor")
    s = sub.add_parser("serve"); s.add_argument("--bind"); s.add_argument("--port", type=int)
    args = ap.parse_args()
    cfg = load_config()
    fn = {"list": cmd_list, "capture": cmd_capture, "heal": cmd_heal, "check": cmd_check,
          "status": cmd_status, "doctor": cmd_doctor, "serve": cmd_serve}[args.cmd]
    sys.exit(fn(args, cfg))


if __name__ == "__main__":
    main()
