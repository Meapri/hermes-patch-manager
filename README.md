# hermes-patch-manager

A small subsystem that keeps **multiple runtime patches** attached to Hermes and
**self-verifying**, on Ubuntu + systemd, reportable over a Tailscale network.

## The core idea

`hermes update` rebuilds the venv, which wipes anything installed into the venv's
`site-packages` (the classic `sitecustomize.py` hook). So this manager **never
puts the hook in the venv**. Instead:

- The loader (`loader/sitecustomize.py`) lives in `/opt/hermes-patch-manager`.
- The gateway systemd unit gets a drop-in that sets
  `Environment=PYTHONPATH=/opt/hermes-patch-manager/loader`.
- Python auto-imports `sitecustomize` from `PYTHONPATH` at interpreter startup.

Because the loader is outside the venv, **a venv rebuild cannot wipe it** — the
patches simply reattach on the next gateway start. There is nothing to "recover".

The loader reads every manifest in `registry.d/*.json` and installs one import
hook per patch, so any number of patches coexist under a single loader.

## Recovery layers (belt & suspenders)

| Layer | Handles |
|-------|---------|
| PYTHONPATH-injected loader (outside venv) | `hermes update` / venv rebuild — the main threat, fully immunized |
| gateway `ExecStartPre=-hpm heal` | logs state before every start; never blocks startup |
| `hermes-patch-guard.timer` → `hpm guard` | a *running* gateway whose patch drifted (e.g. update didn't restart it): restarts it to reattach (if `auto_restart_on_drift`) |
| `hpm check` / health endpoint | **interface drift** — a Hermes update that changed a hooked API. This can't be auto-fixed (the patch code must be updated), but it is **detected** and surfaced over the tailnet |

## Install (on the Ubuntu box)

`install.sh` auto-detects whether the Hermes gateway is a **`systemctl --user`**
service or a **system** service and installs the manager in the matching scope,
so the drift-guard can actually restart the gateway.

```bash
# per-user gateway (systemctl --user hermes-gateway) — the common desktop/build case:
PATCH_SRC=~/hermes-claude-auth/anthropic_billing_bypass.py ./install.sh
#   -> deploys to ~/.local/share/hermes-patch-manager, units under
#      ~/.config/systemd/user, enables linger for boot persistence.

# system gateway (root):
sudo PATCH_SRC=~/hermes-claude-auth/anthropic_billing_bypass.py ./install.sh
#   -> deploys to /opt/hermes-patch-manager, units under /etc/systemd/system.
```

Force a scope with `MODE=user` / `MODE=system`. It writes `config.json`, installs
the gateway drop-in + three units (`hermes-patch-health.service`,
`hermes-patch-guard.service/.timer`), restarts the gateway, and prints
`hpm doctor`. Add the bin dir (`~/.local/bin` in user mode) to your `PATH`.

## Usage

```bash
hpm doctor            # human summary: loader / drop-in / per-patch runtime state
hpm check             # verify every enabled patch is APPLIED at runtime; exit!=0 on drift
hpm status            # full JSON (what the health endpoint serves)
hpm list              # registered patches
```

`check`/`status` don't just check files exist — they launch the venv python
under the loader, import each hooked module, and assert the patch's marker. That
proves the patch is **actually applied at runtime**, which is the only thing that
matters for billing/behavior.

## Adding another patch

Drop your patch module anywhere, then:

```bash
sudo hpm add /path/my_patch.py \
  --name my-patch \
  --hook agent.some_module:apply_my_patch \
  --hook agent.other_module:install_hook \
  --verify agent.some_module:_MY_PATCH_APPLIED \
  --source https://github.com/you/my-patch
sudo systemctl restart hermes-gateway.service
hpm check
```

- `--hook target:func` — when `target` is imported, call `my_patch.func` (it is
  passed the imported module; a zero-arg function also works).
- `--verify import:MARKER` — `check`/health assert `getattr(import, MARKER)` is
  truthy after the target loads.

Disable/remove: `hpm remove my-patch` (`--purge` to delete the manifest).

## Tailnet health endpoint

`hermes-patch-health.service` runs `hpm serve`, binding to your Tailscale IP
(`tailscale ip -4`, override with `health_bind`) on port `8577`:

```bash
# from any node on the tailnet:
curl http://<tailscale-ip>:8577/health     # full JSON status
curl -f http://<tailscale-ip>:8577/healthz # 200 healthy / 503 drift  (for probes/monitors)
```

It binds to the tailnet interface only, so it is not exposed to the public
internet.

## What survives `hermes update`

| Component | Location | Survives update? |
|-----------|----------|:---:|
| loader + patches + registry | `/opt/hermes-patch-manager` | ✅ outside venv |
| gateway drop-in (PYTHONPATH) | `/etc/systemd/system/<svc>.d/` | ✅ outside venv |
| the venv | rebuilt by `hermes update` | patches reattach on next start |

## Honest limitations

- **Interface drift is detected, not auto-fixed.** If a Hermes update renames or
  re-signatures a hooked API, the patch stops applying. `hpm check` and the
  health endpoint go red so you know immediately — but the patch code itself must
  be updated. No system can paper over that.
- The guard's `auto_restart_on_drift` restarts the gateway to heal stale-process
  drift; set it to `false` in `config.json` if you never want automatic restarts.
- `install.sh`/systemd/tailscale are Linux-only. The loader and `hpm` logic are
  plain Python and are smoke-tested cross-platform.
