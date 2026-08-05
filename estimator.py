"""Core estimator: inventory + migration disposition -> Azure targets -> costs.

DISPOSITION (the "7 R's") drives the deployment model:
  Rehost / Lift-and-shift            -> IaaS      (VM)
  Replatform / PaaS                  -> PaaS      (App Service | Azure SQL DB | PostgreSQL | MySQL | Redis)
  Refactor / Containerize            -> Container (AKS)
  Rearchitect / Rebuild / Modernize  -> Modernize (Container Apps | Hyperscale | Cosmos)
  Repurchase                         -> SaaS      (per-user license line item)
  Retire / Retain                    -> skipped

If a row has NO disposition and NO explicit `target`, it DEFAULTS TO standard IaaS (VM).

Inventory columns (CSV/XLSX, case-insensitive):
  name, environment(Prod/NonProd), role, disposition, target(optional override),
  vcpu, memory_gb, os(linux|windows), storage_gb, quantity, hours,
  azure_sku(optional), unit_price(SaaS $/user/mo)
"""
import os
import re
import pandas as pd
import yaml
import pricing as P

CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "config", "skus.yaml")))
HOURS = P.HOURS_PER_MONTH
TF = CFG["term_factors"]


# ---------------------------------------------------------------- column mapping
# Ordered canonical schema -> accepted header variants. Earlier canonicals claim a
# source column first. Lets users upload inventories in many different formats.
_COLUMN_ALIASES = [
    ("name", ["name", "host name", "hostname", "server name", "servername", "server",
              "vm name", "machine name", "host", "node name", "computer name"]),
    ("environment", ["environment", "env", "tier", "stage"]),
    ("role", ["role", "server type", "workload type", "workload", "type", "function",
              "application", "app", "app name", "component"]),
    ("disposition", ["disposition", "migration disposition", "migration strategy",
                     "strategy", "6r", "r disposition", "recommendation"]),
    ("target", ["target", "azure target", "target service", "azure service", "target platform"]),
    ("vcpu", ["vcpu", "vcpus", "cpu", "cpus", "cores", "core", "processors",
              "req vcpu", "vcpu count", "num cpu", "cpu count", "logical processors"]),
    ("memory_gb", ["memory gb", "memory", "ram", "ram gb", "mem", "memory gib",
                   "ram gib", "memory mb", "memory mib", "ram mb", "ram mib"]),
    ("os", ["os", "operating system", "platform", "os type", "guest os"]),
    ("storage_gb", ["storage gb", "storage", "disk", "disk gb", "disk capacity",
                    "total disk capacity mib", "total disk capacity", "total disk",
                    "storage gib", "used storage gb", "provisioned storage gb", "disk mib"]),
    ("quantity", ["quantity", "qty", "count", "instances", "instance count", "nodes", "node count"]),
    ("hours", ["hours", "hours mo", "hours month", "monthly hours", "run hours"]),
    ("azure_sku", ["azure sku", "sku", "vm sku", "instance type", "azure type", "vm size", "size"]),
    ("unit_price", ["unit price", "license cost", "price per user", "unit cost", "list price"]),
]
_NUMERIC = ["vcpu", "memory_gb", "storage_gb", "quantity", "hours", "unit_price"]


def _norm_key(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).strip().lower()).strip()


def _normalize_columns(df):
    """Map arbitrary inventory headers onto the canonical schema + coerce/convert units.
    Pure pandas; no network or threads. Idempotent."""
    df = df.copy()
    norm_to_orig = {}
    for c in df.columns:
        norm_to_orig.setdefault(_norm_key(c), c)
    rename, claimed = {}, set()
    src_header = {}  # canonical -> original header (for unit detection)
    for canonical, variants in _COLUMN_ALIASES:
        if canonical in claimed:
            continue
        for v in [canonical] + variants:
            key = _norm_key(v)
            if key in norm_to_orig and norm_to_orig[key] not in rename:
                orig = norm_to_orig[key]
                rename[orig] = canonical
                src_header[canonical] = _norm_key(orig)
                claimed.add(canonical)
                break
    df = df.rename(columns=rename)
    # numeric coercion
    for col in _NUMERIC:
        if col in df.columns:
            df[col] = (df[col].astype(str)
                       .str.replace(",", "", regex=False)
                       .str.extract(r"(-?\d+\.?\d*)")[0])
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    # unit conversion for memory / storage based on the original header token
    for col in ("memory_gb", "storage_gb"):
        if col in df.columns:
            hdr = src_header.get(col, "")
            toks = set(hdr.split())
            if {"mib", "mb"} & toks:
                df[col] = df[col] / 1024.0
            elif {"tib", "tb"} & toks:
                df[col] = df[col] * 1024.0
            elif {"kib", "kb"} & toks:
                df[col] = df[col] / (1024.0 ** 2)
            elif not ({"gib", "gb"} & toks):
                # No explicit unit: infer MB if the values are implausibly large for GB.
                nz = df[col][df[col] > 0]
                if len(nz) and nz.median() > 1024:
                    df[col] = df[col] / 1024.0
    return df
PDB = CFG["paas_db"]


# ---------------------------------------------------------------- helpers
def _pick(catalog, vcpu, mem, key_v="vcpu", key_m="mem"):
    best = None
    for c in catalog:
        if c[key_v] >= vcpu and c[key_m] >= mem:
            score = c[key_v] + c[key_m] / 8.0
            if best is None or score < best[0]:
                best = (score, c)
    return best[1] if best else catalog[-1]


def _role_cat(role, name):
    t = f"{role} {name}".lower()
    if any(k in t for k in ("postgres", "postgresql")):
        return "postgres"
    if "mysql" in t:
        return "mysql"
    if "cosmos" in t or "mongo" in t:
        return "cosmos"
    if any(k in t for k in ("sql", "db", "database", "oracle")):
        return "db"
    if any(k in t for k in ("cache", "redis")):
        return "cache"
    return "app"


def resolve_target(disposition, role, name, explicit):
    """Return a concrete target key using disposition + role. Defaults to IaaS 'vm'."""
    if explicit:
        return explicit.lower(), "explicit"
    disp = str(disposition or "").strip().lower()
    bucket = CFG["disposition_map"].get(disp, "iaas" if disp == "" else "iaas")
    if bucket == "skip":
        return "skip", disp
    rc = _role_cat(role, name)
    if bucket == "iaas":
        return "vm", disp or "default-iaas"
    if bucket == "saas":
        return "saas", disp
    if bucket == "paas":
        return ({"db": "sqldb", "postgres": "postgres", "mysql": "mysql", "cosmos": "cosmos",
                 "cache": "redis", "app": "appservice"}[rc], disp)
    if bucket == "container":
        return ({"db": "sqldb", "postgres": "postgres", "mysql": "mysql", "cosmos": "cosmos",
                 "cache": "redis", "app": "aks"}[rc], disp)
    if bucket == "modernize":
        return ({"db": "hyperscale", "postgres": "postgres", "mysql": "mysql", "cosmos": "cosmos",
                 "cache": "redis", "app": "aca"}[rc], disp)
    return "vm", disp


# ---------------------------------------------------------------- cost functions
# Each returns dict: {sku, model, rate_basis, monthly}
def cost_vm(vcpu, mem, os_type, hours, qty, term, region, ahb=False, sku=None):
    sku = sku or _pick(CFG["vm_catalog"], vcpu, mem)["sku"]
    pr = P.vm_price(sku, region, os_type)
    if not pr:
        return {"sku": sku, "model": "IaaS", "rate_basis": "N/A", "monthly": 0.0, "note": "price not found"}
    rate = {"payg": pr["payg"], "1y": pr["sp1y"], "3y": pr["sp3y"]}.get(term) or pr["payg"]
    basis = {"payg": "PAYG", "1y": "1yr SP", "3y": "3yr SP"}[term]
    if ahb and os_type.startswith("win"):
        lin = P.vm_price(sku, region, "linux")
        if lin:
            rate = {"payg": lin["payg"], "1y": lin["sp1y"], "3y": lin["sp3y"]}.get(term) or lin["payg"]
            basis += "+AHB"
    return {"sku": sku, "model": "IaaS (VM)", "rate_basis": basis, "monthly": qty * rate * hours}


def cost_aca(vcpu, mem, hours, qty, term, region):
    aca = P.aca_rates(region)
    if hours >= HOURS - 1:
        dv = aca.get("ded_vcpu_hr_1y") if term != "payg" else aca.get("ded_vcpu_hr")
        dm = aca.get("ded_mem_hr_1y") if term != "payg" else aca.get("ded_mem_hr")
        m = qty * (vcpu * dv + mem * dm) * hours
        return {"sku": "ACA Dedicated", "model": "Container Apps",
                "rate_basis": "1yr SP" if term != "payg" else "PAYG", "monthly": m}
    cv, cm = aca["cons_vcpu_sec"], aca["cons_mem_sec"]
    m = qty * (vcpu * cv + mem * cm) * hours * 3600
    return {"sku": "ACA Consumption", "model": "Container Apps", "rate_basis": "active-hrs", "monthly": m}


def cost_aks(vcpu, mem, hours, qty, term, region, sku=None):
    node = _pick(CFG["vm_catalog"], vcpu, mem)
    nsku = sku or node["sku"]
    pr = P.vm_price(nsku, region, "linux")
    rate = {"payg": pr["payg"], "1y": pr["sp1y"], "3y": pr["sp3y"]}.get(term) or pr["payg"]
    nodes_cost = qty * rate * hours
    fee = P.aks_cluster_fee(region) * HOURS  # one cluster control-plane fee
    return {"sku": f"AKS {qty}x{nsku}", "model": "Container (AKS)",
            "rate_basis": {"payg": "PAYG", "1y": "1yr SP", "3y": "3yr SP"}[term],
            "monthly": nodes_cost + fee}


def cost_appservice(vcpu, mem, hours, qty, term, region, sku=None):
    plan = _pick(CFG["appservice_catalog"], vcpu, mem)
    psku = sku or plan["sku"]
    pr = P.appservice_price(psku, region)
    if not pr:
        return {"sku": psku, "model": "PaaS (App Service)", "rate_basis": "N/A", "monthly": 0.0}
    rate = {"payg": pr["payg"], "1y": pr["sp1y"], "3y": pr["sp3y"]}.get(term) or pr["payg"]
    return {"sku": f"App Service {psku}", "model": "PaaS (App Service)",
            "rate_basis": {"payg": "PAYG", "1y": "1yr SP", "3y": "3yr SP"}[term],
            "monthly": qty * rate * hours}


def cost_hyperscale(vcore, storage, qty, term, region):
    hs = P.hyperscale_rates(region)
    if term == "payg":
        comp = vcore * hs["compute_payg_hr"] * HOURS; basis = "PAYG"
    elif term == "3y" and hs["reserved_3y_vcore_yr"]:
        comp = vcore * hs["reserved_3y_vcore_yr"] / 12.0; basis = "3yr Reserved"
    else:
        comp = vcore * hs["reserved_1y_vcore_yr"] / 12.0; basis = "1yr Reserved"
    m = qty * (comp + storage * hs["storage_gb_mo"])
    return {"sku": f"Hyperscale {int(vcore)} vCore", "model": "PaaS (SQL Hyperscale)",
            "rate_basis": basis, "monthly": m}


def cost_sqldb(vcore, storage, qty, term, region):
    r = P.sqldb_gp_reserved(region)
    if term == "payg":
        comp = vcore * r["compute_payg_hr"] * HOURS; basis = "PAYG"
    elif term == "3y":
        comp = vcore * r["r3y"] / 12.0; basis = "3yr Reserved"
    else:
        comp = vcore * r["r1y"] / 12.0; basis = "1yr Reserved"
    m = qty * (comp + storage * r["storage_gb_mo"])
    return {"sku": f"SQL DB GP {int(vcore)} vCore", "model": "PaaS (Azure SQL DB)",
            "rate_basis": basis, "monthly": m}


def cost_flexdb(kind, vcore, storage, qty, term, region):
    vhr = PDB[f"{kind}_gp_vcore_hr"]; shr = PDB[f"{kind}_storage_gb_mo"]
    comp = vcore * vhr * HOURS * TF[term]
    m = qty * (comp + storage * shr)
    label = {"postgres": "PaaS (PostgreSQL Flex)", "mysql": "PaaS (MySQL Flex)"}[kind]
    return {"sku": f"{kind.title()} GP {int(vcore)} vCore", "model": label,
            "rate_basis": f"{term} (approx)", "monthly": m}


def cost_redis(mem, qty, term, region):
    r = _pick(CFG["redis_catalog"], 0, mem, key_v="mem", key_m="mem")
    pr = P.redis_price(r["sku"], region, r["tier"])
    rate = pr["payg"] if pr else 0.138
    m = qty * rate * HOURS * TF[term]
    return {"sku": f"Redis {r['tier']} {r['sku']}", "model": "PaaS (Cache for Redis)",
            "rate_basis": f"{term} (approx)", "monthly": m}


def cost_cosmos(ru, storage, qty, term, region):
    ru = ru or 4000
    comp = (ru / 100.0) * PDB["cosmos_ru_100_hr"] * HOURS * TF[term]
    m = qty * (comp + storage * PDB["cosmos_storage_gb_mo"])
    return {"sku": f"Cosmos {int(ru)} RU/s", "model": "PaaS (Cosmos DB)",
            "rate_basis": f"{term} (approx)", "monthly": m}


def cost_saas(users, unit_price):
    return {"sku": "SaaS license", "model": "SaaS (per-user)", "rate_basis": "license",
            "monthly": (users or 0) * (unit_price or 0)}


# ---------------------------------------------------------------- main estimate
def estimate(df, region="eastus", term="1y", ahb=False, resiliency=False):
    df = _normalize_columns(df)
    rows = []
    for _, r in df.iterrows():
        name = str(r.get("name", "unnamed"))
        env = str(r.get("environment", "Prod") or "Prod")
        role = str(r.get("role", "") or "")
        disp = str(r.get("disposition", "") or "")
        if disp.lower() in ("nan", "none"):
            disp = ""
        override = str(r.get("target", "") or "").strip()
        if override.lower() in ("nan", "none", ""):
            override = ""
        qty = int(float(r.get("quantity", 1) or 1))
        vcpu = float(r.get("vcpu", 0) or 0)
        mem = float(r.get("memory_gb", 0) or 0)
        os_type = str(r.get("os", "linux") or "linux").lower()
        storage = float(r.get("storage_gb", 0) or 0)
        hours = float(r.get("hours", HOURS) or HOURS)
        sku_hint = str(r.get("azure_sku", "") or "").strip()
        if sku_hint.lower() in ("nan", "none"):
            sku_hint = ""
        unit_price = float(r.get("unit_price", 0) or 0)

        target, disp_used = resolve_target(disp, role, name, override)
        base = {"name": name, "environment": env, "role": role, "disposition": disp or "(none->IaaS)",
                "target": target, "quantity": qty, "region": region,
                "os": os_type, "vcpu": vcpu, "memory_gb": mem, "storage_gb": storage}
        if target == "skip":
            continue

        if target == "vm":
            c = cost_vm(vcpu, mem, os_type, hours, qty, term, region, ahb, sku_hint or None)
        elif target == "aca":
            c = cost_aca(vcpu, mem, hours, qty, term, region)
        elif target == "aks":
            c = cost_aks(vcpu, mem, hours, qty, term, region, sku_hint or None)
        elif target == "appservice":
            c = cost_appservice(vcpu, mem, hours, qty, term, region, sku_hint or None)
        elif target == "hyperscale":
            c = cost_hyperscale(vcpu, storage, qty, term, region)
        elif target == "sqldb":
            c = cost_sqldb(vcpu, storage, qty, term, region)
        elif target in ("postgres", "mysql"):
            c = cost_flexdb(target, vcpu, storage, qty, term, region)
        elif target == "redis":
            c = cost_redis(mem, qty, term, region)
        elif target == "cosmos":
            c = cost_cosmos(storage and storage * 10 or 4000, storage, qty, term, region)
        elif target == "saas":
            c = cost_saas(qty, unit_price)
        else:
            c = cost_vm(vcpu, mem, os_type, hours, qty, term, region, ahb)
        base.update(c)
        base["hours"] = hours
        rows.append(base)

    lines = pd.DataFrame(rows)

    resil_total = 0.0
    if resiliency and not lines.empty:
        prod = lines[lines["environment"].str.lower().str.startswith("prod")]
        db_ha = prod[prod["model"].str.contains("SQL|Hyperscale|PostgreSQL|MySQL|Cosmos", regex=True)]["monthly"].sum()
        vm_asr = prod[prod["model"].str.contains("VM|AKS")]["quantity"].sum() * CFG["addons"]["asr_instance_mo"]
        resil_total = db_ha + vm_asr

    def area(row):
        e = row["environment"].lower()
        if not e.startswith("prod"):
            return "Non-Production"
        return "Prod - " + row["model"]
    lines["area"] = lines.apply(area, axis=1)
    summ = lines.groupby("area", as_index=False)["monthly"].sum()
    if resil_total:
        summ = pd.concat([summ, pd.DataFrame([{"area": "Resiliency Add-In", "monthly": resil_total}])],
                         ignore_index=True)
    summ["annual"] = summ["monthly"] * 12
    total = pd.DataFrame([{"area": "TOTAL", "monthly": summ["monthly"].sum(), "annual": summ["annual"].sum()}])
    summ = pd.concat([summ, total], ignore_index=True)
    return lines, summ


# ---------------------------------------------------------------- modernization compare
def modernization(df, region="eastus", term="1y", ahb=False):
    """For each APP-type workload, compare cost across modernization paths."""
    df = _normalize_columns(df)
    out = []
    for _, r in df.iterrows():
        name = str(r.get("name", "unnamed"))
        role = str(r.get("role", "") or "")
        disp = str(r.get("disposition", "") or "").strip().lower()
        bucket = CFG["disposition_map"].get(disp, "iaas")
        if bucket in ("saas", "skip"):
            continue
        if _role_cat(role, name) != "app":
            continue
        vcpu = float(r.get("vcpu", 0) or 0); mem = float(r.get("memory_gb", 0) or 0)
        os_type = str(r.get("os", "linux") or "linux").lower()
        hours = float(r.get("hours", HOURS) or HOURS)
        qty = int(float(r.get("quantity", 1) or 1))
        out.append({
            "name": name,
            "Rehost (VM)": round(cost_vm(vcpu, mem, os_type, hours, qty, term, region, ahb)["monthly"], 2),
            "Replatform (App Service)": round(cost_appservice(vcpu, mem, hours, qty, term, region)["monthly"], 2),
            "Containerize (AKS)": round(cost_aks(vcpu, mem, hours, qty, term, region)["monthly"], 2),
            "Modernize (Container Apps)": round(cost_aca(vcpu, mem, hours, qty, term, region)["monthly"], 2),
        })
    comp = pd.DataFrame(out)
    if not comp.empty:
        totals = {"name": "TOTAL"}
        for c in comp.columns:
            if c != "name":
                totals[c] = round(comp[c].sum(), 2)
        comp = pd.concat([comp, pd.DataFrame([totals])], ignore_index=True)
    return comp


def meta(region="eastus"):
    return {"hyperscale": P.hyperscale_rates(region), "aca": P.aca_rates(region),
            "sqldb_gp": P.sqldb_gp_reserved(region), "aks_cluster_fee_hr": P.aks_cluster_fee(region),
            "blob_hot_lrs": P.blob_hot_lrs(region)}
