"""Azure Migration Cost Estimator - dashboard.
Run:  streamlit run app.py
"""
import datetime as dt
import io
import json
import os
import tempfile
import pandas as pd
import streamlit as st
import estimator as E
import workbook as W
import assistant as A
from cli import TERM_LABEL

REGIONS = ["eastus", "eastus2", "westus2", "westeurope", "northeurope",
           "centralus", "southcentralus"]

st.set_page_config(page_title="Azure Migration Cost Estimator", layout="wide")


# ---------------------------------------------------------------- session state
def _init_state():
    ss = st.session_state
    ss.setdefault("p_region", "eastus")
    ss.setdefault("p_term", "1y")
    ss.setdefault("p_ahb", False)
    ss.setdefault("p_resiliency", True)
    ss.setdefault("inventory", None)     # raw inventory DataFrame (source of truth)
    ss.setdefault("overrides", {})       # accumulated pricing overrides
    ss.setdefault("results", None)       # {"lines", "summ", "modern"}
    ss.setdefault("sig", None)           # signature of last computed inputs
    ss.setdefault("chat", [])            # [{"role","content"}]


# Apply any deferred parameter changes (from the AI assistant) BEFORE the sidebar
# widgets are instantiated, to avoid Streamlit's "set after widget" error.
if "pending_params" in st.session_state:
    _pend = st.session_state.pop("pending_params")
    for _k, _v in (_pend or {}).items():
        st.session_state[f"p_{_k}"] = _v

_init_state()


def merge_overrides(base, new):
    """Accumulate pricing overrides: compound multipliers, replace absolute prices."""
    base = dict(base or {})
    if isinstance(new.get("global_multiplier"), (int, float)):
        base["global_multiplier"] = base.get("global_multiplier", 1.0) * float(new["global_multiplier"])
    for key in ("by_model", "by_name"):
        if new.get(key):
            d = dict(base.get(key, {}))
            for k, v in new[key].items():
                if isinstance(v, (int, float)):
                    d[k] = d.get(k, 1.0) * float(v)
            base[key] = d
    if new.get("set_monthly"):
        d = dict(base.get("set_monthly", {}))
        d.update({k: float(v) for k, v in new["set_monthly"].items() if isinstance(v, (int, float))})
        base["set_monthly"] = d
    return base


def _sig():
    ss = st.session_state
    inv = ss.get("inventory")
    inv_sig = None if inv is None else int(pd.util.hash_pandas_object(inv.astype(str)).sum())
    return json.dumps([inv_sig, ss.p_region, ss.p_term, ss.p_ahb, ss.p_resiliency,
                       ss.get("overrides")], sort_keys=True, default=str)


def recompute():
    ss = st.session_state
    inv = ss.get("inventory")
    if inv is None or getattr(inv, "empty", True):
        ss["results"] = None
        return
    lines, _ = E.estimate(inv, region=ss.p_region, term=ss.p_term,
                          ahb=ss.p_ahb, resiliency=ss.p_resiliency)
    lines = E.apply_overrides(lines, ss.get("overrides") or {})
    summ = E.build_summary(lines, ss.p_resiliency)
    modern = E.modernization(inv, region=ss.p_region, term=ss.p_term, ahb=ss.p_ahb)
    ss["results"] = {"lines": lines, "summ": summ, "modern": modern}


def ensure_results():
    """Recompute only when the inputs (inventory, params, overrides) have changed."""
    ss = st.session_state
    if ss.get("inventory") is None:
        return
    sig = _sig()
    if ss.get("results") is None or ss.get("sig") != sig:
        with st.spinner("Fetching live Azure pricing and computing..."):
            recompute()
        ss["sig"] = sig


def build_context():
    ss = st.session_state
    ctx = {"params": {"region": ss.p_region, "term": ss.p_term,
                      "ahb": ss.p_ahb, "resiliency": ss.p_resiliency},
           "active_overrides": ss.get("overrides") or {}}
    r = ss.get("results")
    if r and r.get("lines") is not None and not r["lines"].empty:
        lines = r["lines"]
        keep = [c for c in ["name", "model", "monthly"] if c in lines.columns]
        ctx["workloads"] = lines[keep].round(0).to_dict("records")[:60]
        tot = r["summ"][r["summ"]["area"] == "TOTAL"]
        if not tot.empty:
            ctx["total_monthly"] = round(float(tot.iloc[0]["monthly"]), 0)
    return ctx


# ---------------------------------------------------------------- header
st.title("Azure Migration Cost Estimator")
st.caption("Upload a workload inventory - get a client-ready Azure cost estimate priced with live Azure Retail rates. "
           "Chat with the built-in AI assistant to tweak assumptions and pricing in plain English. "
           "Supports IaaS, PaaS, containers, SaaS and modernization comparisons.")

with st.sidebar:
    st.header("Parameters")
    st.selectbox("Region", REGIONS, key="p_region")
    st.radio("Pricing term", ["1y", "3y", "payg"],
             format_func=lambda t: TERM_LABEL[t], key="p_term")
    st.checkbox("Azure Hybrid Benefit (Windows/SQL)", key="p_ahb")
    st.checkbox("Resiliency add-in (HA replica + ASR)", key="p_resiliency")
    st.divider()
    ov = st.session_state.get("overrides") or {}
    if ov:
        st.markdown("**Active pricing overrides**")
        st.json(ov)
        if st.button("Reset overrides"):
            st.session_state["overrides"] = {}
            st.rerun()
    st.divider()
    st.markdown("**Inventory columns**")
    st.code("name, environment, role, disposition,\ntarget, vcpu, memory_gb, os,\n"
            "storage_gb, quantity, hours, azure_sku,\nunit_price", language="text")
    st.markdown("Leave `disposition` and `target` blank to default to **standard IaaS (VM)**.")

# ---------------------------------------------------------------- Instructions
with st.expander("How pricing works - IaaS / PaaS / SaaS / combination (read me)", expanded=False):
    st.markdown("""
This agent selects an Azure target for each workload from its **migration disposition** (the "7 R's"),
then prices it with **live Azure Retail Prices**. If a row has no disposition and no explicit target,
it is priced as **standard IaaS (a VM)**.

**How the disposition drives the target**

| Disposition | Bucket | Typical Azure target |
|---|---|---|
| Rehost | IaaS | Virtual Machine (lift-and-shift) |
| Replatform | PaaS / container | App Service, Azure SQL DB, PostgreSQL/MySQL Flex, Redis |
| Refactor | Container | AKS |
| Rearchitect / Modernize | Modernize | Container Apps, SQL Hyperscale, Cosmos DB |
| Repurchase | SaaS | Per-user license (no compute meter) |
| Retire / Retain | Skip | Excluded from Azure cost |

The concrete target is refined by the workload `role` (e.g. a *Replatform* + `sql database`
becomes **Azure SQL DB**, while *Replatform* + `postgres` becomes **PostgreSQL Flexible Server**).

---
**1. IaaS pricing** (Rehost, or no disposition)
- Size from `vcpu` / `memory_gb` -> nearest VM SKU (or set `azure_sku` to force one, e.g. `Standard_D8s_v5`).
- Rate = live VM retail rate for the chosen **term** (PAYG / 1-yr / 3-yr savings plan).
- Add **Azure Hybrid Benefit** for Windows/SQL to strip the license cost.

**2. PaaS pricing** (Replatform / Modernize of data & app services)
- App Service (web/api apps), Azure SQL DB (GP reserved), SQL Hyperscale (per vCore),
  PostgreSQL / MySQL Flexible Server, Cache for Redis, Cosmos DB (RU/s).
- Priced from the service's own meters; `vcpu`/`memory_gb`/`storage_gb` drive tier & size.

**3. SaaS pricing** (Repurchase)
- No compute meter. Cost = `quantity` (users) x `unit_price` ($/user/month).
- Put the per-user license price in the `unit_price` column and the user count in `quantity`.

**4. Combination (mixed estate)**
- One inventory can mix all of the above - just set each row's `disposition`.
- Results group by model (IaaS / PaaS / Container / SaaS) on the Summary sheet.
- **If a row lists no disposition and no target, it is assumed to be standard IaaS (VM).**

Use the **Modernization** tab to compare Rehost vs Replatform vs Containerize vs Modernize
for the same app workloads before you commit to a target.
""")

# ---------------------------------------------------------------- load inventory
up = st.file_uploader("Upload inventory (CSV or XLSX)", type=["csv", "xlsx", "xls"])
c_load1, c_load2 = st.columns([1, 4])
use_sample = c_load1.button("Use sample inventory")

if up is not None:
    new_df = pd.read_excel(up) if up.name.lower().endswith(("xlsx", "xls")) else pd.read_csv(up)
    st.session_state["inventory"] = new_df
    st.session_state["results"] = None
elif use_sample:
    st.session_state["inventory"] = pd.read_csv("samples/sample_inventory.csv")
    st.session_state["results"] = None

if st.session_state.get("inventory") is None:
    st.info("Upload an inventory file or click **Use sample inventory** to begin.")
    st.stop()

st.subheader("Inventory")
edited = st.data_editor(st.session_state["inventory"], num_rows="dynamic",
                        use_container_width=True, key="inv_editor")
if st.button("Apply inventory changes", type="primary"):
    st.session_state["inventory"] = edited
    st.rerun()

# recompute if anything changed
ensure_results()
res = st.session_state.get("results")
if not res:
    st.stop()

lines, summ, modern = res["lines"], res["summ"], res["modern"]
total = summ[summ["area"] == "TOTAL"].iloc[0]

c1, c2, c3 = st.columns(3)
c1.metric("Monthly", f"${total['monthly']:,.0f}")
c2.metric("Annual", f"${total['annual']:,.0f}")
c3.metric("3-Year", f"${total['annual']*3:,.0f}")

tab_sum, tab_lines, tab_mod, tab_ai, tab_price = st.tabs(
    ["Summary", "Line items", "Modernization", "AI assistant", "Custom pricing"])

with tab_sum:
    st.subheader("Summary by area")
    show = summ[summ["area"] != "TOTAL"].set_index("area")
    st.bar_chart(show["monthly"])
    st.dataframe(summ.style.format({"monthly": "${:,.0f}", "annual": "${:,.0f}"}),
                 use_container_width=True)

with tab_lines:
    st.subheader("Line items (disposition -> target -> model)")
    cols = [c for c in ["name", "environment", "role", "disposition", "target", "model",
                        "sku", "rate_basis", "quantity", "hours", "storage_gb", "monthly"]
            if c in lines.columns]
    st.dataframe(lines[cols].style.format({"monthly": "${:,.0f}"}),
                 use_container_width=True)

with tab_mod:
    st.subheader("Modernization path comparison")
    st.caption("Per app workload, $/month. Compare Rehost vs Replatform vs Containerize vs Modernize.")
    if modern is not None and not modern.empty:
        mchart = modern[modern[modern.columns[0]] != "TOTAL"].set_index(modern.columns[0])
        st.bar_chart(mchart)
        fmt = {c: "${:,.0f}" for c in modern.columns if c != modern.columns[0]}
        st.dataframe(modern.style.format(fmt), use_container_width=True)
    else:
        st.info("No app workloads to compare (modernization applies to app/web/api roles).")

# ---------------------------------------------------------------- AI assistant
with tab_ai:
    st.subheader("Chat: customize the estimate in plain English")
    st.caption("Engine: " + A.engine_name())
    st.markdown(
        "Ask for things like: *\"switch to a 3-year term and turn on hybrid benefit\"*, "
        "*\"apply a 15% partner discount\"*, *\"add a 10% contingency buffer\"*, "
        "*\"set the web-frontend rows to 5 instances\"*, *\"bump every SQL line by 20%\"*, "
        "or *\"price prod-sql01 at $1,200/month\"*. Changes update the estimate live.")

    for m in st.session_state["chat"]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    with st.form("ai_form", clear_on_submit=True):
        prompt = st.text_area("Your request", height=80,
                              placeholder="e.g. apply a 15% discount and use a 3 year term")
        col_a, col_b = st.columns([1, 6])
        sent = col_a.form_submit_button("Send", type="primary")
        clear = col_b.form_submit_button("Clear chat")

    if clear:
        st.session_state["chat"] = []
        st.rerun()

    if sent and prompt and prompt.strip():
        st.session_state["chat"].append({"role": "user", "content": prompt.strip()})
        with st.spinner("Thinking..."):
            result = A.interpret(prompt.strip(), build_context())
        # apply structured changes
        if result.get("row_edits"):
            st.session_state["inventory"] = E.apply_row_edits(
                st.session_state["inventory"], result["row_edits"])
        if result.get("pricing"):
            st.session_state["overrides"] = merge_overrides(
                st.session_state["overrides"], result["pricing"])
        if result.get("params"):
            st.session_state["pending_params"] = result["params"]
        engine = result.get("_engine", "")
        reply = result.get("reply", "Done.")
        st.session_state["chat"].append(
            {"role": "assistant", "content": f"{reply}\n\n*(engine: {engine})*"})
        st.rerun()

# ---------------------------------------------------------------- custom pricing
with tab_price:
    st.subheader("Manually customize pricing")
    ov = st.session_state.get("overrides") or {}

    st.markdown("**Global adjustment** - shift every line up or down (discount or uplift).")
    cur_pct = int(round((ov.get("global_multiplier", 1.0) - 1.0) * 100))
    pct = st.slider("Adjustment %", -60, 60, cur_pct, step=1,
                    help="-15 = 15% discount, +10 = 10% uplift/buffer")
    cga, cgb = st.columns([1, 6])
    if cga.button("Apply global adjustment"):
        new_ov = dict(ov)
        new_ov["global_multiplier"] = round(1 + pct / 100.0, 4)
        st.session_state["overrides"] = new_ov
        st.rerun()
    if cgb.button("Clear all overrides"):
        st.session_state["overrides"] = {}
        st.rerun()

    st.divider()
    st.markdown("**Per-line prices** - edit the monthly column to pin an absolute price, "
                "then click *Apply manual prices*.")
    price_df = lines[["name", "model", "monthly"]].copy()
    price_df["monthly"] = price_df["monthly"].round(2)
    edited_prices = st.data_editor(
        price_df, use_container_width=True, key="price_editor",
        disabled=["name", "model"], num_rows="fixed")
    if st.button("Apply manual prices"):
        set_monthly = {}
        base = price_df.set_index("name")["monthly"]
        for _, row in edited_prices.iterrows():
            nm = row["name"]
            try:
                new_val = float(row["monthly"])
            except (TypeError, ValueError):
                continue
            if nm in base.index and abs(float(base.loc[nm]) - new_val) > 0.01:
                set_monthly[nm] = new_val
        if set_monthly:
            st.session_state["overrides"] = merge_overrides(
                st.session_state["overrides"], {"set_monthly": set_monthly})
            st.rerun()
        else:
            st.info("No price changes detected.")

# ---------------------------------------------------------------- export
params = {"region": st.session_state.p_region, "term": st.session_state.p_term,
          "term_label": TERM_LABEL[st.session_state.p_term],
          "ahb": st.session_state.p_ahb, "resiliency": st.session_state.p_resiliency,
          "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
          "meta": E.meta(st.session_state.p_region)}
buf = io.BytesIO()
tmp = os.path.join(tempfile.gettempdir(), "azure_estimate.xlsx")
W.build(lines, summ, params, tmp, modern=modern)
with open(tmp, "rb") as f:
    buf.write(f.read())
st.download_button("Download Excel workbook", buf.getvalue(),
                   file_name="Azure_Cost_Estimate.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
