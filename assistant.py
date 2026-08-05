"""AI assistant: turn a natural-language request into structured changes to the estimate.

Uses Azure OpenAI (GPT-4o) when configured via env vars:
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT (default gpt-4o),
    AZURE_OPENAI_API_VERSION (default 2024-10-21)
Falls back to a deterministic rule-based parser when no keys are present, so the chat
still works offline / before the model is wired up.

interpret(user_msg, context) -> actions dict:
{
  "reply": str,                       # human-friendly summary of the change
  "params": {region, term, ahb, resiliency},   # only keys that change
  "row_edits": [{"match": str, "set": {vcpu, memory_gb, quantity, os, target,
                                        disposition, hours, unit_price}}],
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
                   "set": { "vcpu": <n>, "memory_gb": <n>, "quantity": <n>,
                            "os": "linux"|"windows", "target": <one of TARGETS>,
                            "disposition": <7R word>, "hours": <n>, "unit_price": <n> } } ]
- "pricing": { "global_multiplier": <number>,        // 10% discount => 0.9, 15% uplift => 1.15
               "by_model": { "<model label substring>": <mult> },
               "by_name": { "<workload name substring>": <mult> },
               "set_monthly": { "<workload name substring>": <absolute $/month> } }

Valid targets: __TARGETS__. Valid terms: __TERMS__.
Rules: a discount lowers the multiplier below 1; a buffer/uplift/contingency raises it above 1.
If the user only chats without asking for a change, return just a "reply".
Current estimate context (for reference): __CONTEXT__
"""


def is_configured():
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT"))


def engine_name():
    return "Azure OpenAI" if is_configured() else "rule-based (no AI endpoint configured)"


def _client():
    if not is_configured():
        return None, None
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-chat")
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


def interpret(user_msg, context=None):
    context = context or {}
    client, deployment = _client()
    if client:
        try:
            sys = (SYSTEM_PROMPT
                   .replace("__TARGETS__", ", ".join(VALID_TARGETS))
                   .replace("__TERMS__", ", ".join(VALID_TERMS))
                   .replace("__CONTEXT__", json.dumps(context)[:4000]))
            resp = _complete(client, deployment,
                             [{"role": "system", "content": sys},
                              {"role": "user", "content": user_msg}])
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
    out = {"params": {}, "row_edits": [], "pricing": {}, "_engine": "rules"}
    changes = []

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
                 if k in ("vcpu", "memory_gb", "quantity", "os", "target",
                          "disposition", "hours", "unit_price")}
            if s.get("target") and s["target"].lower() not in VALID_TARGETS:
                s.pop("target", None)
            if s:
                edits.append({"match": str(e["match"]), "set": s})
    data["row_edits"] = edits
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
