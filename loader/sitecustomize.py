"""
Hermes Patch Manager — generalized multi-patch loader.
======================================================

This file is a ``sitecustomize.py`` that Python auto-imports at interpreter
startup for ANY module on ``sys.path``.  It is injected via ``PYTHONPATH``
(set in the hermes-gateway systemd drop-in) so it lives ENTIRELY OUTSIDE the
Hermes venv.  Consequence: ``hermes update`` rebuilding the venv cannot wipe it,
so patches auto-reattach on the next gateway start with zero recovery needed.

It reads every enabled manifest in ``../registry.d/*.json`` and, for each hook,
installs a MetaPathFinder that applies the patch the moment its target Hermes
module is imported.  One loader, N patches — this is the coexistence mechanism.

Never raises: any failure is logged to stderr and skipped so a broken patch can
never take down the interpreter (and therefore never bricks Hermes).
"""
from __future__ import annotations

import importlib
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATCH_STORE = os.environ.get("HPM_PATCH_STORE", os.path.join(_BASE, "patches"))
_REGISTRY = os.environ.get("HPM_REGISTRY", os.path.join(_BASE, "registry.d"))


def _log(msg: str) -> None:
    sys.stderr.write(f"[hermes-patch-manager] {msg}\n")


if os.path.isdir(_PATCH_STORE) and _PATCH_STORE not in sys.path:
    sys.path.insert(0, _PATCH_STORE)


def _load_registry():
    manifests = []
    if not os.path.isdir(_REGISTRY):
        return manifests
    for name in sorted(os.listdir(_REGISTRY)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(_REGISTRY, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as exc:
            _log(f"skipping bad manifest {name}: {exc}")
            continue
        if not manifest.get("enabled", True):
            continue
        if not manifest.get("name") or not manifest.get("module"):
            _log(f"skipping manifest {name}: missing name/module")
            continue
        manifests.append(manifest)
    return manifests


def _make_meta_finder(target_module, patch_module_name, apply_attr, label):
    try:
        from importlib.abc import MetaPathFinder
        from importlib.util import find_spec
    except Exception:
        return

    class _PatchFinder(MetaPathFinder):
        _done = False

        def find_spec(self, fullname, path=None, target=None):
            if fullname != target_module or self._done:
                return None
            if self in sys.meta_path:
                sys.meta_path.remove(self)
            try:
                spec = find_spec(fullname)
            finally:
                if self not in sys.meta_path:
                    sys.meta_path.insert(0, self)
            if spec is None or spec.loader is None:
                return None
            original_exec = getattr(spec.loader, "exec_module", None)
            if not callable(original_exec):
                return None
            finder = self

            def patched_exec(module):
                original_exec(module)
                finder._done = True
                try:
                    patch_mod = importlib.import_module(patch_module_name)
                    fn = getattr(patch_mod, apply_attr, None)
                    if not callable(fn):
                        _log(f"{label}: {patch_module_name}.{apply_attr} not callable")
                        return
                    try:
                        fn(module)          # apply fns that take the target module
                    except TypeError:
                        fn()                # apply fns that take no args
                except Exception as exc:
                    import traceback
                    _log(f"{label}: applying {apply_attr} on {target_module} failed: {exc}")
                    traceback.print_exc(file=sys.stderr)

            spec.loader.exec_module = patched_exec
            return spec

    sys.meta_path.insert(0, _PatchFinder())


def _install(manifest):
    module_name = manifest["module"]
    hooks = manifest.get("hooks") or []
    if not hooks:
        _log(f"{manifest['name']}: no hooks declared")
        return
    for hook in hooks:
        target = hook.get("target")
        apply_attr = hook.get("apply")
        if not target or not apply_attr:
            _log(f"{manifest['name']}: bad hook {hook!r}")
            continue
        _make_meta_finder(target, module_name, apply_attr, manifest["name"])


def _main():
    for manifest in _load_registry():
        try:
            _install(manifest)
        except Exception as exc:
            _log(f"hook install failed for {manifest.get('name')}: {exc}")


try:
    _main()
except Exception as _exc:  # never break the interpreter
    _log(f"loader aborted: {_exc}")
