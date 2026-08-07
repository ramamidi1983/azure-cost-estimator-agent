"""AI assistant: turn a natural-language request into structured changes to the estimate.

Uses Azure OpenAI when configured via env vars:
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY (optional; omit to use Entra ID /
    managed identity), AZURE_OPENAI_DEPLOYMENT (your deployment name, default gpt-4o),
    AZURE_OPENAI_API_VERSION (default 2024-10-21)
Falls back to a deterministic rule-based parser when no keys are present, so the chat
still works offline / before the model is wired up.

interpret(user_msg, context, history) -> actions dict. `history` is the prior chat turns
([{role, content}]) so the model can resolve references to earlier messages and edit cumulatively.
{
  "reply": str,                       # human-friendly summary of the change
  "params": {region, term, ahb, resiliency},   # only keys that change
  "row_edits": [{"match": str, "set": {name, environment, role, vcpu, memory_gb, storage_gb,
                                        quantity, os, target, disposition, hours, unit_price}}],
  "row_ops": [{"op": "delete", "match": str} |
              {"op": "dedupe", "subset": [str]} |
              {"op": "add", "set": {name, environment, role, vcpu, memory_gb, storage_gb,
                                    quantity, os, target, disposition, hours, unit_price}}],
  "pricing": {"global_multiplier": float, "by_model": {label: mult},
              "by_name": {name: mult}, "set_monthly": {name: amount}},
  "_engine": "Azure OpenAI" | "rules"
}
"""
import json
import os
import re

VALID_TARGETS = ["vm", "aca", "aks", "appservice", "hyperscale", "sqldb",
                 "postgres", "mysql", "redis", "cosmos", "saas"]
VALID_TERMS = ["payg", "1y", "3y"]

SYSTEM_PROMPT = """You are a pricing assistant embedded in an Azure Migration Cost Estimator.
The user tells you, in plain English, how they want to change the estimate. Respond with ONLY a
single JSON object (no markdown) using these optional keys — include a key ONLY if it changes:

- "reply": a short, friendly one-or-two sentence summary of what you changed.
- "params": { "region": <azure region>, "term": "payg"|"1y"|"3y",
              "ahb": true|false, "resiliency": true|false }
- "row_edits": [ { "match": "<substring of the workload name>",
                   "set": { "name": <new name>, "environment": <Prod|NonProd>, "role": <text>,
                            "vcpu": <n>, "memory_gb": <n>, "storage_gb": <n>, "quantity": <n>,
                            "os": "linux"|"windows", "target": <one of TARGETS>,
                            "disposition": <7R word>, "hours": <n>, "unit_price": <n> } } ]
- "row_ops": [ { "op": "delete", "match": "<substring of the workload name to remove>" },
               { "op": "dedupe", "subset": ["name"] },      // omit subset to dedupe whole rows
               { "op": "add", "set": { "name": ..., "vcpu": ..., ... } } ]
  Use row_ops to DELETE rows ("remove the old-reporting server", "drop all retired VMs"),
  DEDUPE ("delete duplicate rows", "remove duplicates by name"), or ADD a new workload.
  Use row_edits to change fields on existing rows. You may return both in one turn.
- "pricing": { "global_multiplier": <number>,        // 10% discount => 0.9, 15% uplift => 1.15
               "by_model": { "<model label substring>": <mult> },
               "by_name": { "<workload name substring>": <mult> },
               "set_monthly": { "<workload name substring>": <absolute $/month> } }

Valid targets: __TARGETS__. Valid terms: __TERMS__.
Rules: a discount lowers the multiplier below 1; a buffer/uplift/contingency raises it above 1.
If the user only chats without asking for a change, return just a "reply".
You are in a multi-turn conversation: the messages before this one are the running history of
this same estimate. USE that history — resolve references like "that server", "the same discount",
"make it 3 years instead", "undo that", or "also do the DB" against what was said earlier, and
build on prior edits cumulatively rather than starting over each turn.

You can SEE the user's uploaded inventory sheet and the current priced line items in the context
below. Keys: "inventory" = a SAMPLE (first 100 rows) of the uploaded rows with their real columns
(name, vcpu, memory_gb, os, storage_gb, disposition, target, environment, quantity, ...);
"inventory_row_count" = the TRUE total number of rows; "priced_lines" = the current per-line
monthly costs (compute vs storage components); "total_monthly" = the grand total.
"inventory_stats" is AUTHORITATIVE, computed from the ENTIRE sheet (not the 100-row sample):
it has row_count, unique_names, name_duplicate_rows (how many rows would be removed if you
dedupe by hostname keeping the first), duplicate_name_groups, full_row_duplicate_rows, and
duplicate_names ({hostname: occurrences}). For ANY question about totals, counts, or duplicates
you MUST use inventory_stats — never infer counts from the 100-row sample and never guess. When
asked to list duplicates, list them from inventory_stats.duplicate_names. Note a "duplicate" here
usually means the same Host Name repeated (a shared host running multiple apps), so dedupe by
hostname: use row_ops {"op":"dedupe","subset":["name"]} unless the user explicitly wants exact
whole-row duplicates.
Ground every answer and edit in this actual data: when the user asks analytical questions
("which server is most expensive?", "how many Windows VMs?", "what's driving the DB cost?"),
answer directly from the context in "reply". When they ask for a change, target it precisely
using the real workload names you can see (match row_edits/pricing on those exact names).
Current estimate context (JSON): __CONTEXT__
"""


def _history_messages(history, limit=12):
    """Map prior chat turns [{role, content}] to OpenAI messages, keeping the last `limit`."""
    msgs = []
    for m in (history or [])[-limit:]:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": str(content)})
    return msgs


def is_configured():
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT"))


def engine_name():
    return "Azure OpenAI" if is_configured() else "rule-based (no AI endpoint configured)"


def _client():
    if not is_configured():
        return None, None
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    from openai import AzureOpenAI
    if api_key:  # key auth (if the account allows local auth)
        client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
    else:  # Entra ID (managed identity / az login) — used when local auth is disabled
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        client = AzureOpenAI(azure_endpoint=endpoint, azure_ad_token_provider=token_provider,
                             api_version=api_version)
    return client, deployment


def _extract_json(text):
    """Pull the first JSON object out of a model response (tolerates code fences/prose)."""
    if not text:
        raise ValueError("empty response")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text).rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        depth = 0
        for i in range(start, len(text)) if start >= 0 else []:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise ValueError("no JSON object found in model response")


def _complete(client, deployment, messages):
    """Call chat completions, degrading unsupported params (temperature/response_format)."""
    attempts = [
        {"response_format": {"type": "json_object"}, "temperature": 0},
        {"temperature": 0},
        {},
    ]
    last = None
    for kw in attempts:
        try:
            return client.chat.completions.create(model=deployment, messages=messages, **kw)
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e).lower()
            if not any(w in msg for w in ("temperature", "response_format", "unsupported",
                                          "not support", "invalid", "400")):
                raise
    raise last


def interpret(user_msg, context=None, history=None):
    context = context or {}
    client, deployment = _client()
    if client:
        try:
            sys = (SYSTEM_PROMPT
                   .replace("__TARGETS__", ", ".join(VALID_TARGETS))
                   .replace("__TERMS__", ", ".join(VALID_TERMS))
                   .replace("__CONTEXT__", json.dumps(context, default=str)[:16000]))
            messages = [{"role": "system", "content": sys}]
            messages += _history_messages(history)
            messages.append({"role": "user", "content": user_msg})
            resp = _complete(client, deployment, messages)
            data = _extract_json(resp.choices[0].message.content)
            data.setdefault("reply", "Done.")
            out = _sanitize(data)
            out["_engine"] = "Azure OpenAI"
            return out
        except Exception as e:  # graceful fallback
            data = _fallback(user_msg)
            data["reply"] = f"(Azure OpenAI error — used rules) {data.get('reply', '')} [{e}]"
            return data
    return _fallback(user_msg)


# ---------------------------------------------------------------- rule fallback
def _fallback(msg):
    t = msg.lower()
    out = {"params": {}, "row_edits": [], "row_ops": [], "pricing": {}, "_engine": "rules"}
    changes = []

    if re.search(r"\b(dedupe|de-?duplicate|duplicate rows?|remove duplicates?)\b", t):
        out["row_ops"].append({"op": "dedupe"}); changes.append("removed duplicate rows")

    if re.search(r"\b(3[\s-]?y(ea)?r|three[\s-]?year|3yr)\b", t):
        out["params"]["term"] = "3y"; changes.append("term → 3-year")
    elif re.search(r"\b(1[\s-]?y(ea)?r|one[\s-]?year|1yr)\b", t):
        out["params"]["term"] = "1y"; changes.append("term → 1-year")
    elif re.search(r"\b(payg|pay[\s-]?as[\s-]?you[\s-]?go|on[\s-]?demand)\b", t):
        out["params"]["term"] = "payg"; changes.append("term → PAYG")

    if re.search(r"\b(no|without|remove|disable)\b.*\b(hybrid|ahb)\b", t):
        out["params"]["ahb"] = False; changes.append("Azure Hybrid Benefit off")
    elif re.search(r"\b(hybrid benefit|ahb)\b", t):
        out["params"]["ahb"] = True; changes.append("Azure Hybrid Benefit on")

    if re.search(r"\b(no|without|remove|disable)\b.*\b(resilien|ha|high availab|dr|disaster)\b", t):
        out["params"]["resiliency"] = False; changes.append("resiliency off")
    elif re.search(r"\b(resilien|high availab|\bha\b|disaster recovery|\bdr\b)\b", t):
        out["params"]["resiliency"] = True; changes.append("resiliency on")

    for reg in ["eastus2", "eastus", "westus2", "westeurope", "northeurope",
                "centralus", "southcentralus"]:
        if reg in t.replace(" ", ""):
            out["params"]["region"] = reg; changes.append(f"region → {reg}"); break

    m = re.search(r"(\d+(?:\.\d+)?)\s*%?\s*(discount|off)\b", t)
    if m:
        pct = float(m.group(1)); out["pricing"]["global_multiplier"] = round(1 - pct / 100, 4)
        changes.append(f"{pct:g}% discount")
    m = re.search(r"(\d+(?:\.\d+)?)\s*%?\s*(buffer|uplift|contingency|markup|increase|margin)", t)
    if m:
        pct = float(m.group(1)); out["pricing"]["global_multiplier"] = round(1 + pct / 100, 4)
        changes.append(f"{pct:g}% {m.group(2)}")

    out = _sanitize(out)
    if changes:
        out["reply"] = "Applied: " + "; ".join(changes) + "."
    else:
        out["reply"] = ("I couldn't map that to a change without an AI model configured. "
                        "Try phrases like '3 year term', 'add 10% buffer', 'apply 15% discount', "
                        "'turn on hybrid benefit', or 'use region eastus2'. "
                        "Wire up Azure OpenAI for full natural-language edits.")
    return out


# ---------------------------------------------------------------- validation
def _sanitize(data):
    data.setdefault("params", {})
    data.setdefault("row_edits", [])
    data.setdefault("row_ops", [])
    data.setdefault("pricing", {})
    p = data["params"]
    if p.get("term") not in VALID_TERMS:
        p.pop("term", None)
    for k in list(p.keys()):
        if k not in ("region", "term", "ahb", "resiliency"):
            p.pop(k, None)
    edits = []
    for e in data.get("row_edits") or []:
        if isinstance(e, dict) and e.get("match") and isinstance(e.get("set"), dict):
            s = {k: v for k, v in e["set"].items()
                 if k in ("name", "environment", "role", "vcpu", "memory_gb", "storage_gb",
                          "quantity", "os", "target", "disposition", "hours", "unit_price")}
            if s.get("target") and str(s["target"]).lower() not in VALID_TARGETS:
                s.pop("target", None)
            if s:
                edits.append({"match": str(e["match"]), "set": s})
    data["row_edits"] = edits
    ops = []
    add_keys = ("name", "environment", "role", "disposition", "target", "vcpu", "memory_gb",
                "os", "storage_gb", "quantity", "hours", "unit_price")
    for o in data.get("row_ops") or []:
        if not isinstance(o, dict):
            continue
        kind = str(o.get("op", "")).strip().lower()
        if kind == "delete" and str(o.get("match", "")).strip():
            ops.append({"op": "delete", "match": str(o["match"]).strip()})
        elif kind == "dedupe":
            clean = {"op": "dedupe"}
            if isinstance(o.get("subset"), list) and o["subset"]:
                clean["subset"] = [str(s) for s in o["subset"]]
            ops.append(clean)
        elif kind == "add" and isinstance(o.get("set"), dict):
            s = {k: v for k, v in o["set"].items() if k in add_keys}
            if s.get("target") and str(s["target"]).lower() not in VALID_TARGETS:
                s.pop("target", None)
            if s:
                ops.append({"op": "add", "set": s})
    data["row_ops"] = ops
    pr = data["pricing"] or {}
    clean = {}
    if isinstance(pr.get("global_multiplier"), (int, float)):
        clean["global_multiplier"] = float(pr["global_multiplier"])
    for key in ("by_model", "by_name", "set_monthly"):
        if isinstance(pr.get(key), dict):
            clean[key] = {str(k): float(v) for k, v in pr[key].items()
                          if isinstance(v, (int, float))}
    data["pricing"] = clean
    data.setdefault("_engine", "rules")
    return data
