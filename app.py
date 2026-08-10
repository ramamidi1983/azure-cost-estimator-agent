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
import pricing as P
import workbook as W
import assistant as A
import memory as M
import tco as TCO
from cli import TERM_LABEL

REGIONS = [
    # --- Commercial: US ---
    "eastus", "eastus2", "centralus", "northcentralus", "southcentralus",
    "westus", "westus2", "westus3", "westcentralus",
    # --- Commercial: Americas ---
    "canadacentral", "canadaeast", "brazilsouth",
    # --- Commercial: Europe ---
    "northeurope", "westeurope", "uksouth", "ukwest", "francecentral",
    "germanywestcentral", "switzerlandnorth", "norwayeast", "swedencentral",
    "polandcentral", "italynorth", "spaincentral",
    # --- Commercial: Asia Pacific / Middle East / Africa ---
    "eastasia", "southeastasia", "australiaeast", "australiasoutheast",
    "japaneast", "japanwest", "koreacentral", "centralindia", "southindia",
    "uaenorth", "qatarcentral", "israelcentral", "southafricanorth",
    # --- US Government (Azure Government) ---
    "usgovvirginia", "usgovarizona", "usgovtexas",
]

# Common Azure service names for the Service Pricing Explorer (free-text also allowed).
COMMON_SERVICES = [
    "Virtual Machines", "Azure NetApp Files", "Storage", "SQL Database",
    "Azure Database for PostgreSQL", "Azure Database for MySQL", "Azure Cosmos DB",
    "Azure Kubernetes Service", "Azure Container Apps", "Azure App Service",
    "Redis Cache", "Azure Files", "Bandwidth", "Load Balancer",
    "Application Gateway", "VPN Gateway", "ExpressRoute", "Virtual Network",
    "Azure Firewall", "Azure Monitor", "Log Analytics", "Backup", "Site Recovery",
    "Azure Kubernetes Service", "Functions", "Event Hubs", "Service Bus",
    "API Management", "Azure Cache for Redis", "Azure Synapse Analytics",
    "Azure Data Factory", "Key Vault", "Azure DNS", "Content Delivery Network",
]

st.set_page_config(page_title="Azure Migration Cost Estimator", layout="wide")

# ---------------------------------------------------------------- inventory reload cache
# Persist to a durable dir (Azure Files mount) when PERSIST_DIR is set, so the
# "Reload inventory" history survives container revisions/restarts; else temp.
_PERSIST_DIR = os.environ.get("PERSIST_DIR") or tempfile.gettempdir()
_CACHE_DIR = os.path.join(_PERSIST_DIR, "cost_estimator_cache")
_HISTORY_LIMIT = 2


def _slot_paths(i):
    return (os.path.join(_CACHE_DIR, f"inv_{i}"),
            os.path.join(_CACHE_DIR, f"inv_{i}.name"))


def _get_history():
    hist = st.session_state.get("inv_history")
    if hist is not None:
        return hist
    hist = []
    for i in range(_HISTORY_LIMIT):
        data_p, name_p = _slot_paths(i)
        try:
            with open(data_p, "rb") as fh:
                data = fh.read()
            with open(name_p, "r", encoding="utf-8") as fh:
                name = fh.read().strip()
            hist.append({"name": name, "data": data})
        except OSError:
            continue
    st.session_state["inv_history"] = hist
    return hist


def _persist_history(hist):
    st.session_state["inv_history"] = hist
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        for i in range(_HISTORY_LIMIT):
            data_p, name_p = _slot_paths(i)
            if i < len(hist):
                with open(data_p, "wb") as fh:
                    fh.write(hist[i]["data"])
                with open(name_p, "w", encoding="utf-8") as fh:
                    fh.write(hist[i]["name"])
            else:
                for p in (data_p, name_p):
                    if os.path.exists(p):
                        os.remove(p)
    except OSError:
        pass


def _add_to_history(name, data):
    hist = [h for h in _get_history() if not (h["name"] == name and h["data"] == data)]
    hist.insert(0, {"name": name, "data": data})
    _persist_history(hist[:_HISTORY_LIMIT])


def _df_from_bytes(name, data):
    buf = io.BytesIO(data)
    if str(name).lower().endswith(("xlsx", "xls")):
        return pd.read_excel(buf)
    return pd.read_csv(buf)


def _load_inventory(name, data, merge=False):
    """Read raw bytes -> canonical inventory DataFrame and store as source of truth.
    When merge=True, append the file's rows to the existing inventory (so manually-added
    services and previously-loaded files are combined) instead of replacing it."""
    df = _df_from_bytes(name, data)
    try:
        df = E._normalize_columns(df)
    except Exception:  # noqa: BLE001 - fall back to raw if normalization fails
        pass
    cur = st.session_state.get("inventory")
    if merge and cur is not None and not getattr(cur, "empty", True):
        df = pd.concat([cur, df], ignore_index=True)
    st.session_state["inventory"] = df
    st.session_state["inv_editor_ver"] = st.session_state.get("inv_editor_ver", 0) + 1
    st.session_state["results"] = None
    st.session_state["compute_requested"] = False  # require an explicit Estimate click


def _append_service_row(row: dict):
    """Append a single manually-specified service line to the working inventory and
    request pricing. Creates the inventory if none is loaded yet."""
    cur = st.session_state.get("inventory")
    new_df = pd.DataFrame([row])
    if cur is None or getattr(cur, "empty", True):
        merged = new_df
    else:
        merged = pd.concat([cur, new_df], ignore_index=True)
    st.session_state["inventory"] = merged
    st.session_state["inv_editor_ver"] = st.session_state.get("inv_editor_ver", 0) + 1
    st.session_state["compute_requested"] = True
    st.session_state["results"] = None


# ---------------------------------------------------------------- pricing overrides
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


# ---------------------------------------------------------------- session state
def _init_state():
    ss = st.session_state
    ss.setdefault("p_region", "eastus")
    ss.setdefault("p_term", "1y")
    ss.setdefault("p_ahb", False)
    ss.setdefault("p_default_os", "Windows")
    ss.setdefault("p_resiliency", True)
    ss.setdefault("p_container_strategy", "existing")
    ss.setdefault("p_pool_aks", True)
    ss.setdefault("p_aks_demand_factor", 0.50)
    ss.setdefault("p_aks_target_utilization", 0.70)
    ss.setdefault("p_aks_headroom", 0.20)
    ss.setdefault("p_optimize_aca", True)
    ss.setdefault("p_aca_prod_active_factor", 0.70)
    ss.setdefault("p_aca_nonprod_active_factor", 0.35)
    ss.setdefault("p_anf_autodetect", False)
    ss.setdefault("inventory", None)     # raw/canonical inventory DataFrame (source of truth)
    ss.setdefault("overrides", {})       # accumulated pricing overrides
    ss.setdefault("results", None)       # {"lines", "summ", "modern"}
    ss.setdefault("compute_requested", False)  # pricing only runs after an explicit click
    ss.setdefault("sig", None)           # signature of last computed inputs
    ss.setdefault("chat", [])            # [{"role","content"}]
    ss.setdefault("auto_apply_learned", True)
    ss.setdefault("inv_editor_ver", 0)   # bumped to reset the data_editor on structural edits
    # One-time: seed this session's defaults from what the tool has learned.
    if not ss.get("_seeded"):
        if ss.get("auto_apply_learned", True):
            for k, v in M.learned_params().items():
                if k in ("region", "term", "ahb", "resiliency"):
                    ss[f"p_{k}"] = v
                elif k == "default_os" and str(v).lower() in ("linux", "windows"):
                    ss["p_default_os"] = str(v).capitalize()
            lp = M.learned_pricing()
            if lp:
                ss["overrides"] = merge_overrides(ss.get("overrides", {}), lp)
        ss["_seeded"] = True


# Apply any deferred parameter changes (from the AI assistant) BEFORE the sidebar
# widgets are instantiated, to avoid Streamlit's "set after widget" error.
if "pending_params" in st.session_state:
    _pend = st.session_state.pop("pending_params")
    for _k, _v in (_pend or {}).items():
        if _k == "default_os" and str(_v).lower() in ("linux", "windows"):
            st.session_state["p_default_os"] = str(_v).capitalize()
        else:
            st.session_state[f"p_{_k}"] = _v

_init_state()


def _sig():
    ss = st.session_state
    inv = ss.get("inventory")
    inv_sig = None if inv is None else int(pd.util.hash_pandas_object(inv.astype(str)).sum())
    return json.dumps([inv_sig, ss.p_region, ss.p_term, ss.p_ahb, ss.p_default_os,
                       ss.p_resiliency, container_options(), ss.get("overrides")],
                      sort_keys=True, default=str)


def container_options():
    ss = st.session_state
    return {
        "container_strategy": ss.p_container_strategy,
        "pool_aks": ss.p_pool_aks,
        "aks_demand_factor": ss.p_aks_demand_factor,
        "aks_target_utilization": ss.p_aks_target_utilization,
        "aks_headroom": ss.p_aks_headroom,
        "optimize_aca": ss.p_optimize_aca,
        "aca_prod_active_factor": ss.p_aca_prod_active_factor,
        "aca_nonprod_active_factor": ss.p_aca_nonprod_active_factor,
        "anf_autodetect": ss.p_anf_autodetect,
    }


def recompute():
    ss = st.session_state
    inv = ss.get("inventory")
    if inv is None or getattr(inv, "empty", True):
        ss["results"] = None
        return
    os_default = str(ss.p_default_os).lower()
    lines, _ = E.estimate(inv, region=ss.p_region, term=ss.p_term,
                          ahb=ss.p_ahb, resiliency=ss.p_resiliency, default_os=os_default,
                          container_opts=container_options())
    lines = E.apply_overrides(lines, ss.get("overrides") or {})
    summ = E.build_summary(lines, ss.p_resiliency)
    modern = E.modernization(inv, region=ss.p_region, term=ss.p_term,
                             ahb=ss.p_ahb, default_os=os_default,
                             container_opts=container_options())
    ss["results"] = {"lines": lines, "summ": summ, "modern": modern}


def ensure_results():
    """Recompute only when pricing has been requested and inputs have changed."""
    ss = st.session_state
    if ss.get("inventory") is None or not ss.get("compute_requested"):
        return
    sig = _sig()
    if ss.get("results") is None or ss.get("sig") != sig:
        with st.spinner("Fetching live Azure pricing and computing..."):
            recompute()
        ss["sig"] = sig


def build_context():
    ss = st.session_state
    ctx = {"params": {"region": ss.p_region, "term": ss.p_term, "ahb": ss.p_ahb,
                      "default_os": ss.p_default_os, "resiliency": ss.p_resiliency,
                      "container_options": container_options()},
           "active_overrides": ss.get("overrides") or {},
           "inventory_loaded": ss.get("inventory") is not None,
           "priced": bool(ss.get("compute_requested"))}
    inv = ss.get("inventory")
    if inv is not None and not getattr(inv, "empty", True):
        inv_cols = [c for c in ["name", "environment", "role", "disposition", "target", "vcpu",
                                "memory_gb", "os", "storage_gb", "quantity", "hours",
                                "azure_sku", "unit_price"] if c in inv.columns]
        ctx["inventory_row_count"] = int(len(inv))
        ctx["inventory"] = inv[inv_cols].head(100).to_dict("records")
        # Authoritative full-sheet duplicate/row analytics (NOT limited by the 100-row sample).
        ctx["inventory_stats"] = E.inventory_stats(inv)
    r = ss.get("results")
    if r and r.get("lines") is not None and not r["lines"].empty:
        lines = r["lines"]
        keep = [c for c in ["name", "environment", "disposition", "target", "model", "component",
                            "sku", "rate_basis", "quantity", "storage_gb", "monthly"]
                if c in lines.columns]
        ctx["priced_lines"] = lines[keep].round(2).to_dict("records")[:150]
        tot = r["summ"][r["summ"]["area"] == "TOTAL"]
        if not tot.empty:
            ctx["total_monthly"] = round(float(tot.iloc[0]["monthly"]), 0)
    return ctx


def apply_ai_result(result):
    """Apply a parsed assistant result to session state. Returns a status note or ''."""
    ss = st.session_state
    notes = []
    inv_changed = False
    if result.get("row_edits"):
        if ss.get("inventory") is not None:
            ss["inventory"] = E.apply_row_edits(ss["inventory"], result["row_edits"])
            inv_changed = True
        else:
            notes.append("Per-server edits will apply once you load an inventory in the Cost estimator tab.")
    if result.get("row_ops"):
        if ss.get("inventory") is not None:
            before = len(ss["inventory"])
            ss["inventory"] = E.apply_row_ops(ss["inventory"], result["row_ops"])
            inv_changed = True
            delta = before - len(ss["inventory"])
            if delta > 0:
                notes.append(f"Removed {delta} row(s) from the inventory.")
            elif delta < 0:
                notes.append(f"Added {-delta} row(s) to the inventory.")
        else:
            notes.append("Load an inventory in the Cost estimator tab before adding/deleting rows.")
    if inv_changed:
        # Force a fresh data_editor so structural changes (delete/add/dedupe) render cleanly
        # instead of colliding with the widget's stored per-cell edit deltas.
        ss["inv_editor_ver"] = ss.get("inv_editor_ver", 0) + 1
    if result.get("pricing"):
        ss["overrides"] = merge_overrides(ss["overrides"], result["pricing"])
    if result.get("params"):
        ss["pending_params"] = result["params"]
    return " ".join(notes)


def process_prompt(text):
    """Run one assistant turn end-to-end: interpret (with history + full sheet context),
    apply the result, learn generic prefs, and append both messages to the shared chat."""
    ss = st.session_state
    ss["chat"].append({"role": "user", "content": text})
    with st.spinner("Thinking..."):
        result = A.interpret(text, build_context(), history=ss["chat"][:-1])
    note = apply_ai_result(result)
    learned = M.learn(result, text) if ss.get("auto_apply_learned", True) else []
    engine = result.get("_engine", "")
    reply = result.get("reply", "Done.")
    if note:
        reply = f"{reply}\n\n> {note}"
    if learned:
        reply = f"{reply}\n\n\U0001F9E0 *Learned for future estimates:* " + "; ".join(learned)
    ss["chat"].append({"role": "assistant", "content": f"{reply}\n\n*(engine: {engine})*"})


def render_chat(loc, placeholder="e.g. apply a 15% discount and use a 3 year term", height=90):
    """Render the shared conversation + input form. `loc` disambiguates widget keys so the
    same chat can appear in more than one place (AI tab AND inline in the estimator tab)."""
    for m in st.session_state["chat"]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
    with st.form(f"ai_form_{loc}", clear_on_submit=True):
        prompt = st.text_area("Your request", height=height, key=f"ai_prompt_{loc}",
                              placeholder=placeholder)
        col_a, col_b = st.columns([1, 6])
        sent = col_a.form_submit_button("Send", type="primary")
        clear = col_b.form_submit_button("Clear chat")
    if clear:
        st.session_state["chat"] = []
        st.rerun()
    if sent and prompt and prompt.strip():
        process_prompt(prompt.strip())
        st.rerun()


def render_learned_prefs():
    """Learning-management panel (auto-apply toggle, what's been learned, forget button)."""
    with st.expander("\U0001F9E0 Learned preferences (auto-applied to new estimates)", expanded=False):
        st.checkbox("Automatically apply learned preferences to new sessions",
                    key="auto_apply_learned")
        lp_params = M.learned_params()
        lp_pricing = M.learned_pricing()
        if lp_params or lp_pricing:
            st.markdown("**What the assistant has learned so far:**")
            if lp_params:
                st.json(lp_params)
            if lp_pricing:
                st.json(lp_pricing)
            log = M.recent_log(8)
            if log:
                st.markdown("**Recent learning:**")
                for e in log:
                    st.markdown(f"- *{e['ts']}* - {', '.join(e['learned'])}  \n  \u21B3 \u201C{e['msg']}\u201D")
            if st.button("Forget all learned preferences"):
                M.clear()
                st.rerun()
        else:
            st.caption("Nothing learned yet. Ask for generic changes like \u201Calways use a 3-year term\u201D "
                       "or \u201Capply a 15% partner discount\u201D and they'll be remembered and applied "
                       "to future estimates automatically.")


# ---------------------------------------------------------------- header
st.title("Azure Migration Cost Estimator")
st.caption("Upload a workload inventory for a client-ready Azure cost estimate priced with live Azure "
           "Retail rates, then use the built-in AI assistant (in the Inventory section) to edit rows, "
           "dedupe, and shape pricing in plain English. Supports IaaS, PaaS, containers, SaaS and "
           "modernization comparisons.")

with st.sidebar:
    st.header("Parameters")
    st.selectbox("Region", REGIONS, key="p_region")
    st.radio("Pricing term", ["1y", "3y", "payg"],
             format_func=lambda t: TERM_LABEL[t], key="p_term")
    st.checkbox("Azure Hybrid Benefit (Windows Server VMs)", key="p_ahb",
                help="Applies your existing Windows Server licenses to remove the Windows license "
                     "cost from Windows VMs. Has no effect on Linux VMs or PaaS databases. OS is "
                     "read from each row's `os` column, otherwise the Default OS below.")
    st.selectbox("Default OS (for rows with no OS in file)", ["Windows", "Linux"], key="p_default_os")
    st.checkbox("Resiliency add-in (HA replica + ASR)", key="p_resiliency")
    st.checkbox("Auto-detect file/NAS servers as Azure NetApp Files", key="p_anf_autodetect",
                help="Off by default. When on, servers whose name/role clearly indicate a "
                     "file or NAS server (netapp, anf, nas, nfs, 'file server') are priced as "
                     "Azure NetApp Files instead of a VM. Explicitly set a row's target to "
                     "'anf' to price it as NetApp regardless of this toggle.")
    with st.expander("Container cost optimization", expanded=True):
        st.selectbox(
            "Apply container scenario to eligible app workloads",
            ["existing", "aks", "aca"],
            format_func=lambda value: {
                "existing": "Keep current disposition / target",
                "aks": "Price eligible apps on AKS",
                "aca": "Price eligible apps on Container Apps",
            }[value],
            key="p_container_strategy",
            help="This controls the main Summary total. Eligible app/web/API workloads are "
                 "re-targeted only when AKS or Container Apps is selected. Eligibility comes "
                 "from app/web/API role or name hints, Refactor/Modernize disposition, or an "
                 "explicit AKS/Container Apps target. Unknown servers, databases, storage, "
                 "Retain, Retire, and SaaS rows keep their existing targets.",
        )
        st.checkbox("Apply shared AKS pooling by environment (Prod / NonProd)", key="p_pool_aks",
                    help="Shares the AKS control-plane fee and models pooled node capacity "
                         "instead of charging a separate cluster for every application.")
        st.slider("Container demand vs source allocation", 0.10, 1.00, step=0.05,
                  key="p_aks_demand_factor")
        st.slider("Target AKS node utilization", 0.40, 0.90, step=0.05,
                  key="p_aks_target_utilization")
        st.slider("AKS HA / capacity headroom", 0.00, 0.50, step=0.05,
                  key="p_aks_headroom")
        st.checkbox("Apply Container Apps Consumption scaling", key="p_optimize_aca",
                    help="Uses Consumption pricing and adjustable active-time factors, including "
                         "scale-to-zero behavior for intermittent applications.")
        st.slider("Production active-time factor", 0.05, 1.00, step=0.05,
                  key="p_aca_prod_active_factor")
        st.slider("Non-Production active-time factor", 0.05, 1.00, step=0.05,
                  key="p_aca_nonprod_active_factor")
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

# recompute up front so results are fresh
ensure_results()

# ================================================================ COST ESTIMATOR
tab_est, tab_tco, tab_explore = st.tabs(
    ["Cost estimator", "On-prem TCO / ROI", "Service Pricing Explorer"])
with tab_est:
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

    up = st.file_uploader("Upload inventory (CSV or XLSX)", type=["csv", "xlsx", "xls"],
                          key=f"uploader_{st.session_state.get('uploader_ver', 0)}")
    st.caption("Uploaded files are **combined** with any manually-added services and previously "
               "loaded files. Use **Clear all inventory** below to start over.")
    with open("samples/sample_inventory.csv", "rb") as _sf:
        _sample_bytes = _sf.read()
    st.download_button("Download sample inventory template", _sample_bytes,
                       file_name="sample_inventory.csv", mime="text/csv",
                       help="Download this template, fill in your workloads, then upload it above.")

    # ---------------------------------------------------------- quick add a service
    with st.expander("\u2795 Add a service directly (no file needed) \u2014 e.g. 20 TB ANF",
                     expanded=st.session_state.get("inventory") is None):
        st.caption(f"Add individual Azure services and price them instantly. All lines are priced "
                   f"at the sidebar **Region** (currently `{st.session_state.get('p_region', 'eastus')}`) "
                   f"and **Pricing term**. You can add several, then keep uploading a sheet too.")
        _SVC = {
            "Azure NetApp Files": "anf", "Virtual Machine (IaaS)": "vm",
            "Azure SQL Database": "sqldb", "SQL Hyperscale": "hyperscale",
            "App Service": "appservice", "AKS (containers)": "aks",
            "PostgreSQL Flexible": "postgres", "MySQL Flexible": "mysql",
            "Cache for Redis": "redis", "Cosmos DB": "cosmos", "SaaS (per-user)": "saas",
        }
        qa1, qa2, qa3 = st.columns([2, 1, 1])
        _svc_label = qa1.selectbox("Service", list(_SVC.keys()), key="qa_service")
        _target = _SVC[_svc_label]
        _name = qa2.text_input("Line name", value=_target + "-1", key="qa_name")
        _qty = qa3.number_input("Quantity", min_value=1, value=1, step=1, key="qa_qty")

        _vcpu = _mem = _storage = _unit_price = 0.0
        _os = "linux"; _sku = ""
        if _target == "anf":
            b1, b2, b3 = st.columns(3)
            _tier = b1.selectbox("Tier", ["Standard", "Premium", "Ultra"], key="qa_anf_tier")
            _amt = b2.number_input("Capacity", min_value=0.0, value=20.0, step=1.0, key="qa_anf_amt")
            _u = b3.radio("Unit", ["TiB", "GiB"], horizontal=True, key="qa_anf_unit")
            _storage = _amt * 1024.0 if _u == "TiB" else _amt
            _sku = _tier
            _gib = max(_storage, 100.0)
            _cons = P.netapp_files_rate(st.session_state.get("p_region", "eastus"), _tier)
            _rsv = P.netapp_files_reserved(st.session_state.get("p_region", "eastus"), _tier)
            m1, m2, m3 = st.columns(3)
            m1.metric("Consumption (PAYG)", f"${_gib * _cons:,.0f}/mo", help=f"${_cons:.4f}/GiB-mo")
            m2.metric("Reserved 1yr (bulk)", f"${_gib * _rsv['r1y']:,.0f}/mo",
                      f"-{(1 - _rsv['r1y'] / _cons) * 100:.0f}%" if _cons else None,
                      help=f"${_rsv['r1y']:.4f}/GiB-mo")
            m3.metric("Reserved 3yr (bulk)", f"${_gib * _rsv['r3y']:,.0f}/mo",
                      f"-{(1 - _rsv['r3y'] / _cons) * 100:.0f}%" if _cons else None,
                      help=f"${_rsv['r3y']:.4f}/GiB-mo")
            st.caption("100 GiB minimum pool. Reserved ('bulk') is committed capacity sold in "
                       "100 TiB / 1 PiB blocks. The **added line uses the sidebar Pricing term** "
                       "(PAYG \u2192 Consumption, 1yr/3yr \u2192 Reserved).")
        elif _target in ("vm", "aks"):
            b1, b2, b3, b4 = st.columns(4)
            _vcpu = b1.number_input("vCPU", min_value=0.0, value=4.0, step=1.0, key="qa_vcpu")
            _mem = b2.number_input("Memory (GB)", min_value=0.0, value=16.0, step=1.0, key="qa_mem")
            _samt = b3.number_input("Disk", min_value=0.0, value=128.0, step=1.0, key="qa_disk")
            _du = b4.radio("Unit", ["GiB", "TiB"], horizontal=True, key="qa_disk_unit")
            _storage = _samt * 1024.0 if _du == "TiB" else _samt
            if _target == "vm":
                _os = st.radio("OS", ["linux", "windows"], horizontal=True, key="qa_os")
        elif _target in ("sqldb", "hyperscale", "postgres", "mysql"):
            b1, b2, b3 = st.columns(3)
            _vcpu = b1.number_input("vCores", min_value=1.0, value=4.0, step=1.0, key="qa_vcore")
            _samt = b2.number_input("Storage", min_value=0.0, value=256.0, step=1.0, key="qa_dbstor")
            _du = b3.radio("Unit", ["GiB", "TiB"], horizontal=True, key="qa_db_unit")
            _storage = _samt * 1024.0 if _du == "TiB" else _samt
        elif _target == "appservice":
            b1, b2 = st.columns(2)
            _vcpu = b1.number_input("vCPU", min_value=1.0, value=2.0, step=1.0, key="qa_as_vcpu")
            _mem = b2.number_input("Memory (GB)", min_value=1.0, value=8.0, step=1.0, key="qa_as_mem")
        elif _target == "redis":
            _mem = st.number_input("Cache size (GB)", min_value=1.0, value=6.0, step=1.0, key="qa_redis")
        elif _target == "cosmos":
            _samt = st.number_input("Data stored (GB)", min_value=0.0, value=100.0, step=10.0, key="qa_cosmos")
            _storage = _samt
        elif _target == "saas":
            b1, b2 = st.columns(2)
            _qty = b1.number_input("Users", min_value=1, value=50, step=1, key="qa_users")
            _unit_price = b2.number_input("$/user/month", min_value=0.0, value=6.0, step=0.5, key="qa_price")

        if st.button("Add to estimate", type="primary", key="qa_add"):
            _append_service_row({
                "name": _name or f"{_target}-1", "environment": "Prod", "role": "",
                "disposition": "", "target": _target, "quantity": int(_qty),
                "vcpu": _vcpu, "memory_gb": _mem, "storage_gb": _storage,
                "os": _os, "azure_sku": _sku, "unit_price": _unit_price, "hours": E.HOURS,
            })
            st.success(f"Added {_name} ({_svc_label}). Pricing updated below.")
            st.rerun()

    _history = _get_history()
    _reload_idx = None
    st.markdown("**Reload a recent inventory file**")
    if _history:
        _cols = st.columns(len(_history))
        for _i, _h in enumerate(_history):
            with _cols[_i]:
                if st.button(f"\U0001F4C2 Reload: {_h['name']}", key=f"reload_{_i}",
                             help="Reload this previously uploaded inventory file (replaces "
                                  "current inventory)."):
                    _reload_idx = _i
    else:
        st.caption("No saved files yet \u2014 the last "
                   f"{_HISTORY_LIMIT} inventory files you upload will appear here for one-click reload.")

    if up is not None:
        file_id = f"{up.name}:{getattr(up, 'size', len(up.getvalue()))}"
        if st.session_state.get("_last_upload_id") != file_id:
            raw = up.getvalue()
            _add_to_history(up.name, raw)
            _load_inventory(up.name, raw, merge=True)
            st.session_state["_last_upload_id"] = file_id
            st.rerun()  # apply combined inventory + click-to-price state cleanly
    elif _reload_idx is not None:
        _h = _history[_reload_idx]
        _load_inventory(_h["name"], _h["data"])
        st.session_state["_last_upload_id"] = None

    if st.session_state.get("inventory") is None:
        if _history:
            st.info("Upload an inventory file, or click a **Reload** button above to restore a recent file.")
        else:
            st.info("Download the sample inventory template, fill it in, then upload it to begin.")
    else:
        st.subheader("Inventory")
        _ic1, _ic2 = st.columns([4, 1])
        _ic1.caption(f"{len(st.session_state['inventory'])} row(s) loaded "
                     "(manual services + uploaded files combined).")
        if _ic2.button("\U0001F9F9 Clear all inventory", key="clear_inv",
                       help="Remove all rows (manual and uploaded) and start over."):
            st.session_state["inventory"] = None
            st.session_state["results"] = None
            st.session_state["compute_requested"] = False
            st.session_state["_last_upload_id"] = None
            st.session_state["inv_editor_ver"] = st.session_state.get("inv_editor_ver", 0) + 1
            # Reset the uploader widget so it drops the held file (else it re-ingests on rerun).
            st.session_state["uploader_ver"] = st.session_state.get("uploader_ver", 0) + 1
            st.rerun()
        _stats = E.inventory_stats(st.session_state["inventory"])
        _dupe_rows = _stats.get("name_duplicate_rows", 0)
        if _dupe_rows:
            st.warning(
                f"\u26A0\uFE0F {_stats['row_count']} rows, but only {_stats.get('unique_names', 0)} "
                f"unique hostnames \u2014 **{_dupe_rows} duplicate hostname row(s)** across "
                f"{_stats.get('duplicate_name_groups', 0)} host(s). Pricing without deduping would "
                "count the same host multiple times.")
            with st.expander(f"Show the {_stats.get('duplicate_name_groups', 0)} duplicated hostnames"):
                _dn = _stats.get("duplicate_names", {})
                st.dataframe(pd.DataFrame(
                    [{"hostname": k, "occurrences": v} for k, v in _dn.items()]),
                    use_container_width=True, hide_index=True)
            if st.button("Remove duplicate hostnames (keep first)", type="secondary"):
                st.session_state["inventory"] = E.apply_row_ops(
                    st.session_state["inventory"], [{"op": "dedupe", "subset": ["name"]}])
                st.session_state["inv_editor_ver"] = st.session_state.get("inv_editor_ver", 0) + 1
                st.session_state["results"] = None
                st.rerun()
        edited = st.data_editor(st.session_state["inventory"], num_rows="dynamic",
                                use_container_width=True,
                                key=f"inv_editor_{st.session_state.get('inv_editor_ver', 0)}")
        if st.button("Estimate cost", type="primary"):
            st.session_state["inventory"] = edited
            st.session_state["compute_requested"] = True
            st.session_state["results"] = None
            st.rerun()

        with st.expander("\U0001F4AC Ask the AI about this sheet (edit sizes, targets, pricing)",
                         expanded=True):
            st.caption("The assistant can see every column of the uploaded sheet and the live "
                       "line-item costs. Engine: " + A.engine_name())
            st.markdown(
                "Try: *\"which server is most expensive and why?\"*, "
                "*\"delete duplicate rows\"*, *\"remove the old-reporting server\"*, "
                "*\"move all SQL databases to a 3-year term\"*, "
                "*\"resize warehouse-db to 32 vCPU\"*, or *\"add a new prod VM named api-2 with 8 vCPU 32GB\"*.")
            render_chat("est", placeholder="e.g. resize sales-db to 16 vCPU and price all DBs at 3-year term")
            render_learned_prefs()

        res = None
        if not st.session_state.get("compute_requested"):
            st.info("Review or edit the inventory above, then click **Estimate cost** to price it "
                    "with live Azure rates. (Pricing runs only when you click.)")
        else:
            ensure_results()
            res = st.session_state.get("results")
        if res:
            lines, summ, modern = res["lines"], res["summ"], res["modern"]
            total = summ[summ["area"] == "TOTAL"].iloc[0]

            c1, c2, c3 = st.columns(3)
            c1.metric("Monthly", f"${total['monthly']:,.0f}")
            c2.metric("Annual", f"${total['annual']:,.0f}")
            c3.metric("3-Year", f"${total['annual']*3:,.0f}")

            t_sum, t_lines, t_mod, t_price = st.tabs(
                ["Summary", "Line items", "Modernization", "Custom pricing"])

            with t_sum:
                st.subheader("Summary by area")
                show = summ[summ["area"] != "TOTAL"].set_index("area")
                st.bar_chart(show["monthly"])
                st.dataframe(
                    summ,
                    column_config={
                        "monthly": st.column_config.NumberColumn(format="$%.0f"),
                        "annual": st.column_config.NumberColumn(format="$%.0f"),
                    },
                    use_container_width=True,
                )

            with t_lines:
                st.subheader("Line items (disposition -> target -> model)")
                cols = [c for c in ["name", "environment", "role", "disposition", "target", "model",
                                    "component", "sku", "rate_basis", "quantity", "hours",
                                    "storage_gb", "monthly"]
                        if c in lines.columns]
                st.dataframe(
                    lines[cols],
                    column_config={
                        "monthly": st.column_config.NumberColumn(format="$%.0f"),
                    },
                    use_container_width=True,
                )

            with t_mod:
                st.subheader("Modernization path comparison")
                st.caption("Per app workload, $/month. Includes per-app AKS, shared AKS pools, "
                           "always-on Container Apps, and optimized Consumption scaling.")
                if modern is not None and not modern.empty:
                    total_row = modern[modern[modern.columns[0]] == "TOTAL"]
                    if not total_row.empty:
                        selected = total_row.iloc[0]
                        caks, caca, cbest = st.columns(3)
                        caks.metric(
                            "Selected AKS scenario",
                            f"${selected['Selected AKS Scenario']:,.0f}/mo",
                        )
                        caca.metric(
                            "Selected Container Apps scenario",
                            f"${selected['Selected Container Apps Scenario']:,.0f}/mo",
                        )
                        cbest.metric(
                            "Lowest selected container option",
                            f"${selected['Selected Container Option']:,.0f}/mo",
                        )
                    st.info(
                        "The four alternative columns remain visible for comparison. The "
                        "**Selected** columns follow the sidebar checkboxes. The overall estimate "
                        "follows **Apply container scenario to eligible app workloads**. When "
                        "'Keep current disposition / target' is selected, Rehost/IaaS rows remain "
                        "unchanged. Only workloads identified as app/web/API candidates are eligible."
                    )
                    mdata = modern[modern[modern.columns[0]] != "TOTAL"]
                    numeric = list(mdata.select_dtypes(include="number").columns)
                    mchart = mdata.set_index(modern.columns[0])[numeric]
                    st.bar_chart(mchart)
                    money_columns = {
                        c: st.column_config.NumberColumn(format="$%.0f")
                        for c in modern.select_dtypes(include="number").columns
                    }
                    st.dataframe(
                        modern,
                        column_config=money_columns,
                        use_container_width=True,
                    )
                else:
                    st.info("No app workloads to compare (modernization applies to app/web/api roles).")

            with t_price:
                st.subheader("Manually customize pricing")
                ovv = st.session_state.get("overrides") or {}
                st.markdown("**Global adjustment** - shift every line up or down (discount or uplift).")
                cur_pct = int(round((ovv.get("global_multiplier", 1.0) - 1.0) * 100))
                pct = st.slider("Adjustment %", -60, 60, cur_pct, step=1,
                                help="-15 = 15% discount, +10 = 10% uplift/buffer")
                cga, cgb = st.columns([1, 6])
                if cga.button("Apply global adjustment"):
                    new_ov = dict(ovv)
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

            # export
            params = {"region": st.session_state.p_region, "term": st.session_state.p_term,
                      "term_label": TERM_LABEL[st.session_state.p_term],
                      "ahb": st.session_state.p_ahb, "resiliency": st.session_state.p_resiliency,
                      "container_options": container_options(),
                      "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
            buf = io.BytesIO()
            tmp = os.path.join(tempfile.gettempdir(), "azure_estimate.xlsx")
            W.build(lines, summ, params, tmp, modern=modern)
            with open(tmp, "rb") as f:
                buf.write(f.read())
            st.download_button("Download Excel workbook", buf.getvalue(),
                               file_name="Azure_Cost_Estimate.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ================================================================ ON-PREM TCO / ROI
with tab_tco:
    st.subheader("On-prem TCO vs Azure - migration ROI")
    st.caption("Estimate the annual **total cost of ownership** of running this same workload "
               "on-premises (VMware, OpenShift or bare-metal), sized automatically from your "
               "loaded inventory, and compare it against the Azure estimate to show savings, "
               "ROI and payback. All benchmark assumptions below are editable list-price "
               "approximations - tune them to your environment.")

    inv = st.session_state.get("inventory")
    fp = TCO.footprint(inv)
    if fp["vm_count"] == 0:
        st.info("Load an inventory (or add servers) in the **Cost estimator** tab first - "
                "the on-prem estate is sized from your workload's vCPU, memory and storage.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total vCPU", f"{fp['vcpu']:,.0f}")
        m2.metric("Total RAM", f"{fp['memory_gb']:,.0f} GB")
        m3.metric("Total storage", f"{fp['storage_gb']:,.0f} GB")
        m4.metric("VMs / servers", f"{fp['vm_count']:,}")

        platform = st.selectbox("On-prem platform", list(TCO.PLATFORMS.keys()), key="tco_platform")
        base = TCO.PLATFORMS[platform]
        a = dict(base)

        with st.expander("Host sizing & hardware assumptions", expanded=False):
            hc1, hc2, hc3 = st.columns(3)
            a["cores_per_host"] = hc1.number_input(
                "Physical cores / host", 4, 256, int(base["cores_per_host"]), 4,
                key="tco_cores")
            a["vcpu_per_core"] = hc2.number_input(
                "vCPU per core (overcommit)", 1.0, 12.0, float(base["vcpu_per_core"]), 0.5,
                key="tco_overcommit")
            a["ram_per_host_gb"] = hc3.number_input(
                "RAM per host (GB)", 64, 4096, int(base["ram_per_host_gb"]), 64,
                key="tco_ram")
            a["headroom_factor"] = hc1.number_input(
                "HA / headroom factor", 1.0, 2.0, float(base["headroom_factor"]), 0.05,
                key="tco_headroom", help="N+1 redundancy and growth spare capacity.")
            a["host_capex"] = hc2.number_input(
                "Server capex ($ / host)", 2000, 100000, int(base["host_capex"]), 500,
                key="tco_hostcapex")
            a["hardware_life_years"] = hc3.number_input(
                "Hardware life (years)", 3, 7, int(base["hardware_life_years"]), 1,
                key="tco_life")
            a["storage_capex_per_gb"] = hc1.number_input(
                "Storage capex ($ / usable GB)", 0.02, 2.0, float(base["storage_capex_per_gb"]),
                0.01, key="tco_stgcapex")
            a["storage_usable_factor"] = hc2.number_input(
                "Usable / raw storage", 0.3, 1.0, float(base["storage_usable_factor"]), 0.05,
                key="tco_usable", help="Usable capacity after RAID and overhead.")
            a["hw_support_pct"] = hc3.number_input(
                "Annual HW/SW support (% of capex)", 0.0, 0.5, float(base["hw_support_pct"]),
                0.01, key="tco_support")

        with st.expander("Licensing, facilities & labor assumptions", expanded=False):
            lc1, lc2, lc3 = st.columns(3)
            a["platform_lic_per_core_year"] = lc1.number_input(
                "Platform + OS license ($ / core / yr)", 0, 2000,
                int(base["platform_lic_per_core_year"]), 10, key="tco_lic",
                help="VMware/OpenShift subscription or Windows Datacenter, incl. guest OS.")
            a["power_watts_per_host"] = lc2.number_input(
                "Power draw ($W / host)", 100, 2000, int(base["power_watts_per_host"]), 50,
                key="tco_watts")
            a["pue"] = lc3.number_input(
                "PUE", 1.0, 2.5, float(base["pue"]), 0.05, key="tco_pue",
                help="Power usage effectiveness of the datacenter.")
            a["kwh_cost"] = lc1.number_input(
                "Electricity ($ / kWh)", 0.02, 0.60, float(base["kwh_cost"]), 0.01,
                key="tco_kwh")
            a["facilities_per_host_year"] = lc2.number_input(
                "Facilities ($ / host / yr)", 0, 10000, int(base["facilities_per_host_year"]),
                100, key="tco_fac", help="Rack space, cooling infrastructure, real estate.")
            a["network_per_host_year"] = lc3.number_input(
                "Network ($ / host / yr)", 0, 5000, int(base["network_per_host_year"]), 50,
                key="tco_net")
            a["vms_per_admin"] = lc1.number_input(
                "VMs managed per admin (FTE)", 10, 500, int(base["vms_per_admin"]), 10,
                key="tco_vmsadmin")
            a["admin_fte_cost"] = lc2.number_input(
                "Loaded admin cost ($ / FTE / yr)", 40000, 300000, int(base["admin_fte_cost"]),
                5000, key="tco_ftecost")

        tco = TCO.compute_tco(fp, a)

        # Azure comparison basis (from the priced results, if available).
        azure_annual = None
        res = st.session_state.get("results")
        if res and res.get("summ") is not None:
            tot = res["summ"][res["summ"]["area"] == "TOTAL"]
            if not tot.empty:
                azure_annual = float(tot.iloc[0]["annual"])

        st.divider()
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Hosts required", f"{tco['hosts']:,}",
                  help=f"{tco['bound']}-bound; {tco['licensed_cores']:,} licensed cores.")
        s2.metric("On-prem / month", f"${tco['monthly']:,.0f}")
        s3.metric("On-prem / year", f"${tco['annual']:,.0f}")
        s4.metric("On-prem 3-year", f"${tco['annual']*3:,.0f}")

        bd = TCO.breakdown_frame(tco)
        bc1, bc2 = st.columns([3, 2])
        with bc1:
            st.markdown("**On-prem annual cost breakdown**")
            st.bar_chart(bd.set_index("Category")["Annual"])
        with bc2:
            st.dataframe(bd.style.format({"Annual": "${:,.0f}"}),
                         use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("### Migration ROI")
        if azure_annual is None:
            st.warning("Price your workload in the **Cost estimator** tab (click *Estimate cost*) "
                       "to compare against Azure and compute ROI.")
        else:
            rc1, rc2 = st.columns([1, 3])
            years = rc1.selectbox("Horizon (years)", [1, 3, 5], index=1, key="tco_years")
            migration_cost = rc2.number_input(
                "One-time migration cost ($)", 0, 100_000_000, 0, 5000, key="tco_migcost",
                help="Assessment, tooling, professional services, cutover, training.")
            ops_pct = rc1.slider(
                "Azure ops labor (% of on-prem admin)", 0, 100,
                int(round(float(base.get("azure_ops_pct", 0.40)) * 100)), 5,
                key="tco_azops",
                help="Cloud still needs operations effort - typically 30-50% of on-prem "
                     "admin labor. Added to the Azure side for a fair comparison.")
            azure_ops_annual = tco["admin_labor"] * (ops_pct / 100.0)
            r = TCO.roi(tco["annual"], azure_annual, float(migration_cost),
                        years=int(years), azure_ops_annual=azure_ops_annual)

            g1, g2, g3, g4 = st.columns(4)
            g1.metric(f"On-prem {years}-yr", f"${r['onprem_total']:,.0f}")
            g2.metric(f"Azure {years}-yr", f"${r['azure_total']:,.0f}",
                      help="Includes selected term/AHB discounts plus estimated Azure "
                           "operations labor.")
            g3.metric(f"Net savings ({years}-yr)", f"${r['net_savings']:,.0f}",
                      delta=f"{r['savings_pct']:.0f}% vs on-prem")
            payback = (f"{r['payback_months']:.1f} mo" if r['payback_months'] is not None
                       else "n/a")
            g4.metric("Payback period", payback,
                      help="Months for cumulative savings to cover the migration cost.")

            st.caption(f"ROI over {years} years: **{r['roi_pct']:.0f}%** on an Azure + migration "
                       f"investment of ${r['azure_total'] + r['migration_cost']:,.0f} "
                       f"(Azure incl. ~${azure_ops_annual:,.0f}/yr ops labor). "
                       f"Estimated monthly savings vs on-prem: ${r['monthly_savings']:,.0f}.")

            comp = pd.DataFrame({
                "Option": [f"On-prem ({platform})", "Azure"],
                f"{years}-year total": [round(r["onprem_total"], 0), round(r["azure_total"], 0)],
            })
            st.bar_chart(comp.set_index("Option")[f"{years}-year total"])

        st.caption("Benchmarks are editable approximations for planning discussions, not a formal "
                   "quote. Azure figures reflect the region, term and Azure Hybrid Benefit "
                   "settings chosen in the Cost estimator tab.")

# ================================================================ SERVICE PRICING EXPLORER
with tab_explore:
    st.subheader("Service Pricing Explorer")
    st.caption("Look up live Azure Retail rates for **any** Azure service in **any** region "
               "(commercial or US Gov). Pick a service and region, then fetch current meters.")

    ecol1, ecol2 = st.columns([2, 2])
    svc_choice = ecol1.selectbox("Service", COMMON_SERVICES + ["Other (type below)"],
                                 key="exp_service")
    svc_custom = ecol1.text_input("Custom service name (exact Azure serviceName)",
                                  key="exp_service_custom",
                                  placeholder="e.g. Azure Bastion")
    service = svc_custom.strip() if (svc_choice == "Other (type below)" or svc_custom.strip()) else svc_choice
    exp_region = ecol2.selectbox("Region", REGIONS, key="exp_region")
    exp_ptype = ecol2.radio("Price type", ["Consumption", "Reservation"],
                            horizontal=True, key="exp_ptype")
    exp_filter = st.text_input("Filter (optional) - match SKU / meter / product text",
                               key="exp_filter", placeholder="e.g. Premium, Standard_D8, LRS")

    if st.button("Fetch live prices", type="primary", key="exp_fetch"):
        if not service:
            st.warning("Enter or select a service name.")
        else:
            with st.spinner(f"Querying Azure Retail Prices for '{service}' in {exp_region}..."):
                try:
                    rows = P.explore_prices(service, exp_region, price_type=exp_ptype,
                                            contains=exp_filter.strip() or None)
                    st.session_state["exp_results"] = {"service": service, "region": exp_region,
                                                       "ptype": exp_ptype, "rows": rows}
                except Exception as ex:  # noqa: BLE001
                    st.session_state["exp_results"] = None
                    st.error(f"Lookup failed: {ex}")

    er = st.session_state.get("exp_results")
    if er is not None:
        rows = er["rows"]
        if not rows:
            st.info(f"No {er['ptype']} meters found for '{er['service']}' in {er['region']}. "
                    "Check the exact service name (Azure `serviceName`) or try a different region.")
        else:
            edf = pd.DataFrame(rows)
            cols = ["sku", "meter", "product", "unit", "payg", "sp1y", "sp3y"]
            if er["ptype"] == "Reservation":
                cols = ["sku", "meter", "product", "unit", "reservationTerm", "payg"]
            edf = edf[[c for c in cols if c in edf.columns]]
            rename = {"payg": "PAYG / rate", "sp1y": "Savings 1yr", "sp3y": "Savings 3yr",
                      "reservationTerm": "Term"}
            edf = edf.rename(columns=rename)
            st.success(f"{len(edf)} meter(s) for **{er['service']}** in **{er['region']}** "
                       f"({er['ptype']}).")
            money = {c: "${:,.5f}" for c in ["PAYG / rate", "Savings 1yr", "Savings 3yr"]
                     if c in edf.columns}
            st.dataframe(edf.style.format(money, na_rep="-"), use_container_width=True)
            st.download_button(
                "Download prices (CSV)",
                edf.to_csv(index=False).encode("utf-8"),
                file_name=f"{er['service'].replace(' ', '_')}_{er['region']}_{er['ptype']}.csv",
                mime="text/csv", key="exp_csv")
            st.caption("Rates are per the meter's unit of measure (e.g. 1 Hour, 1 GB/Month, "
                       "1 GiB/Hour). Savings-plan columns appear only where Azure publishes them.")
