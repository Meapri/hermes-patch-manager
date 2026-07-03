# hermes-recovery

An **independent, extensible recovery system for Hermes core modifications.**
`hermes update` does a `git reset` + venv rebuild, which wipes any direct edits
to the `~/.hermes/hermes-agent` tree. This system re-applies them automatically,
without depending on Hermes to be working.

## Model

A **mod** is a declarative, self-verifying unit of change, stored outside Hermes
under `mods.d/<id>/`:

```
mods.d/claude-auth/
  mod.json                       # manifest
  tracked.patch                  # source-patch component (a git diff)
  files/agent/anthropic_billing_bypass.py   # new-file component
```

The engine is **handler-based**, so new component types slot in without touching
orchestration. Today: `source-patch` (a git diff) and `new-file`. Each mod
carries a `policy` (`apply: auto|manual`, `merge: git|llm`) and a `verify` spec.

## How recovery works

`hpm heal` applies every enabled mod idempotently, with escalation:

1. `git apply --check` → clean apply
2. `git apply --3way` → merge against blobs still in the object store
3. **LLM merge** (optional, Phase 2) → adapt the patch to changed upstream code
   via an embedded, adapted Antigravity client, then verify before applying

Already-applied mods are detected (`git apply --reverse --check`) and skipped.
After anything changes, the configured services are restarted so a running
gateway reloads the re-applied source.

## Triggers (independent of Hermes)

`hermes update` uses `git reset`, which fires **no git hook**, so recovery is
driven out-of-band by systemd:

- `hermes-recovery-watch.path` watches `.git/logs/HEAD` (the reflog — appended on
  every ref move, unlike `.git/HEAD`) → runs `hpm heal` after a settle delay.
- `hermes-recovery-guard.timer` re-checks periodically (backstop).
- `hermes-recovery-health.service` serves status on the tailnet.

## Install (Ubuntu)

Auto-detects a `systemctl --user` gateway (common) vs a system service:

```bash
./install.sh
hpm doctor
```

Deploys to `~/.local/share/hermes-recovery` (user) or `/opt/hermes-recovery`
(system), installs the systemd units, enables linger for boot persistence.

## Register a modification

Make your edits to the Hermes core, then capture them as a mod:

```bash
# claude-auth: an edited file + a new module
hpm capture claude-auth \
  --files agent/anthropic_adapter.py \
  --new-files agent/anthropic_billing_bypass.py

# your other edits as a separate mod
hpm capture web-tools --files tools/web_tools.py,tests/tools/test_web_tools_config.py
```

## Operate

```bash
hpm list            # mods + applied state
hpm check [--json]  # verify every mod is applied at runtime; exit!=0 on drift
hpm heal            # re-apply now (what the watcher/timer run)
hpm doctor          # human summary
```

Health from any tailnet node:

```bash
curl http://<tailscale-ip>:8577/health     # full JSON
curl -f http://<tailscale-ip>:8577/healthz # 200 / 503
```

## Honest limitations

- **Upstream conflicts**: if `origin/main` changes the lines a patch touches,
  `git apply` + `--3way` fail. Without the LLM tier this is *detected* (health
  goes red) but not auto-fixed; with it, the patch is adapted and **verified**
  before applying — never blindly.
- Prefer capturing each logical change as its own mod so conflicts stay isolated.
