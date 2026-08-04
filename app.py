"""Azure Migration Cost Estimator - dashboard.
Run:  streamlit run app.py
"""
import datetime as dt
import io
import os
import tempfile
import pandas as pd
import streamlit as st
import estimator as E
import workbook as W
from cli import TERM_LABEL

st.set_page_config(page_title="Azure Migration Cost Estimator", layout="wide")
st.title("Azure Migration Cost Estimator")
st.caption("Upload a workload inventory - get a client-ready Azure cost estimate priced with live Azure Retail rates. "
           "Supports IaaS, PaaS, containers, SaaS and modernization comparisons.")

with st.sidebar:
    st.header("Parameters")
    region = st.selectbox("Region", ["eastus", "eastus2", "westus2", "westeurope",
                                     "northeurope", "centralus", "southcentralus"], index=0)
    term = st.radio("Pricing term", ["1y", "3y", "payg"],
                    format_func=lambda t: TERM_LABEL[t], index=0)
    ahb = st.checkbox("Azure Hybrid Benefit (Windows/SQL)", value=False)
    resiliency = st.checkbox("Resiliency add-in (HA replica + ASR)", value=True)
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

up = st.file_uploader("Upload inventory (CSV or XLSX)", type=["csv", "xlsx", "xls"])
use_sample = st.button("Use sample inventory")

df = None
if up is not None:
    df = pd.read_excel(up) if up.name.lower().endswith(("xlsx", "xls")) else pd.read_csv(up)
elif use_sample:
    df = pd.read_csv("samples/sample_inventory.csv")

if df is not None:
    st.subheader("Inventory")
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True)

    if st.button("Estimate cost", type="primary"):
        with st.spinner("Fetching live Azure pricing and computing..."):
            lines, summ = E.estimate(edited, region=region, term=term, ahb=ahb, resiliency=resiliency)
            modern = E.modernization(edited, region=region, term=term, ahb=ahb)
        total = summ[summ["area"] == "TOTAL"].iloc[0]

        c1, c2, c3 = st.columns(3)
        c1.metric("Monthly", f"${total['monthly']:,.0f}")
        c2.metric("Annual", f"${total['annual']:,.0f}")
        c3.metric("3-Year", f"${total['annual']*3:,.0f}")

        tab1, tab2, tab3 = st.tabs(["Summary", "Line items", "Modernization"])

        with tab1:
            st.subheader("Summary by area")
            show = summ[summ["area"] != "TOTAL"].set_index("area")
            st.bar_chart(show["monthly"])
            st.dataframe(summ.style.format({"monthly": "${:,.0f}", "annual": "${:,.0f}"}),
                         use_container_width=True)

        with tab2:
            st.subheader("Line items (disposition -> target -> model)")
            cols = [c for c in ["name", "environment", "role", "disposition", "target", "model",
                                "sku", "rate_basis", "quantity", "hours", "storage_gb", "monthly"]
                    if c in lines.columns]
            st.dataframe(lines[cols].style.format({"monthly": "${:,.0f}"}),
                         use_container_width=True)

        with tab3:
            st.subheader("Modernization path comparison")
            st.caption("Per app workload, $/month. Compare Rehost vs Replatform vs Containerize vs Modernize.")
            if modern is not None and not modern.empty:
                mchart = modern[modern[modern.columns[0]] != "TOTAL"].set_index(modern.columns[0])
                st.bar_chart(mchart)
                fmt = {c: "${:,.0f}" for c in modern.columns if c != modern.columns[0]}
                st.dataframe(modern.style.format(fmt), use_container_width=True)
            else:
                st.info("No app workloads to compare (modernization applies to app/web/api roles).")

        params = {"region": region, "term": term, "term_label": TERM_LABEL[term],
                  "ahb": ahb, "resiliency": resiliency,
                  "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                  "meta": E.meta(region)}
        buf = io.BytesIO()
        tmp = os.path.join(tempfile.gettempdir(), "azure_estimate.xlsx")
        W.build(lines, summ, params, tmp, modern=modern)
        with open(tmp, "rb") as f:
            buf.write(f.read())
        st.download_button("Download Excel workbook", buf.getvalue(),
                           file_name="Azure_Cost_Estimate.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
else:
    st.info("Upload an inventory file or click **Use sample inventory** to begin.")
