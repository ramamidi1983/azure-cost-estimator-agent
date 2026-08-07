"""Persistent 'learning' layer for the cost estimator.

Stores only GENERIC, reusable pricing preferences distilled from AI interactions -
never file-specific data (no per-server row edits, no by-name/absolute prices). These
generic preferences (default term, region, hybrid benefit, resiliency, default OS, and
global / by-model pricing multipliers) are persisted to disk and can be auto-applied as
defaults to future sessions, so the tool "learns" how this user likes to price solutions.

The store lives under the same on-disk cache dir used for inventory reload. It survives
reruns and browser refreshes for the life of the container instance.
"""
import datetime as dt
import json
import os
import tempfile

_DIR = os.path.join(tempfile.gettempdir(), "cost_estimator_cache")
_FILE = os.path.join(_DIR, "learned_prefs.json")

# Only these params are generic enough to carry across different inventories.
GENERIC_PARAMS = ("region", "term", "ahb", "resiliency", "default_os")


def _empty():
    return {"params": {}, "pricing": {}, "log": []}


def load():
    try:
        with open(_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("params", {})
    d.setdefault("pricing", {})
    d.setdefault("log", [])
    return d


def _save(d):
    try:
        os.makedirs(_DIR, exist_ok=True)
        with open(_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
    except OSError:
        pass


def learn(result, user_msg=""):
    """Distil the GENERIC parts of an AI result into the persistent store.
    Returns a list of human-readable strings describing what was learned (may be empty)."""
    d = load()
    learned = []
    params = result.get("params") or {}
    for k in GENERIC_PARAMS:
        if k in params and params[k] is not None:
            d["params"][k] = params[k]
            learned.append(f"{k} = {params[k]}")
    pr = result.get("pricing") or {}
    if isinstance(pr.get("global_multiplier"), (int, float)):
        d["pricing"]["global_multiplier"] = float(pr["global_multiplier"])
        learned.append(f"global pricing x{float(pr['global_multiplier']):g}")
    if isinstance(pr.get("by_model"), dict) and pr["by_model"]:
        bm = d["pricing"].setdefault("by_model", {})
        for k, v in pr["by_model"].items():
            if isinstance(v, (int, float)):
                bm[str(k)] = float(v)
                learned.append(f"{k} pricing x{float(v):g}")
    if learned:
        d["log"].append({"ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                         "msg": (user_msg or "")[:200], "learned": learned})
        d["log"] = d["log"][-100:]
        _save(d)
    return learned


def clear():
    """Forget everything the tool has learned."""
    _save(_empty())


def learned_params():
    return dict(load().get("params", {}))


def learned_pricing():
    return dict(load().get("pricing", {}))


def has_learned():
    d = load()
    return bool(d.get("params")) or bool(d.get("pricing"))


def recent_log(n=10):
    return list(reversed(load().get("log", [])))[:n]
