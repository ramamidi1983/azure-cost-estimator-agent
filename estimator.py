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
import concurrent.futures as _cf
import re
import pandas as pd
import yaml
import pricing as P

CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "config", "skus.yaml")))
HOURS = P.HOURS_PER_MONTH
TF = CFG["term_factors"]
PDB = CFG["paas_db"]


# ---------------------------------------------------------------- inventory normalization
# Canonical schema -> the many header variants users bring in different inventory formats.
# Order matters: earlier canonicals claim a source column first.
_COLUMN_ALIASES = [
    ("name", ["name", "host name", "hostname", "host", "server", "server name", "servername",
              "vm name", "vmname", "machine", "machine name", "computer", "computer name",
              "node", "node name", "instance name", "workload name", "resource name"]),
    ("environment", ["environment", "env", "stage", "prod nonprod", "prod non prod", "tier env"]),
    ("role", ["role", "server type", "server role", "workload", "workload type", "app role",
              "function", "component", "server function"]),
    ("disposition", ["disposition", "migration disposition", "migration strategy",
                     "migration r", "strategy", "6r", "7r", "6 r", "7 r", "recommendation",
                     "migration recommendation", "rec", "target disposition"]),
    ("target", ["target", "azure target", "target service", "azure service", "target platform"]),
    ("vcpu", ["vcpu", "vcpus", "v cpu", "cpu", "cpus", "cores", "core", "vcpu count",
              "cpu cores", "processors", "logical processors", "num cpu", "cpu count"]),
    ("memory_gb", ["memory_gb", "memory gb", "memory gib", "ram gb", "mem gb", "memory mb",
                   "memory mib", "ram mb", "memory", "ram", "mem", "memory ram"]),
    ("os", ["os", "operating system", "os type", "platform", "guest os", "os name"]),
    ("storage_gb", ["storage_gb", "storage gb", "storage gib", "disk gb", "disk gib",
                    "total disk capacity mib", "total disk capacity mb", "total disk capacity gb",
                    "total disk", "disk capacity", "disk", "storage", "provisioned storage",
                    "used storage", "capacity", "total storage"]),
    ("quantity", ["quantity", "qty", "count", "instances", "instance count", "nodes",
                  "node count", "number", "num", "servers"]),
    ("hours", ["hours", "runtime hours", "monthly hours", "hours per month", "active hours"]),
    ("azure_sku", ["azure_sku", "azure sku", "sku", "vm size", "vm sku", "instance type",
                   "target sku", "recommended sku"]),
    ("unit_price", ["unit_price", "unit price", "license price", "price per user",
                    "per user price", "list price"]),
]

_NUMERIC_FILL = {"vcpu": 0.0, "memory_gb": 0.0, "storage_gb": 0.0, "unit_price": 0.0}


def _norm_key(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).strip().lower()).strip()


def normalize_inventory(df):
    """Map an arbitrarily-formatted inventory to the canonical schema so uploads in
    different formats 'just work'. Aliases common headers (Host Name->name, CPU->vcpu,
    Memory->memory_gb, Migration Disposition->disposition, Total Disk Capacity MiB->
    storage_gb, ...), coerces numerics, and converts memory/disk units to GB.
    Idempotent: already-canonical columns are left as-is. Unmapped columns are kept."""
    if df is None or getattr(df, "empty", True):
        return df
    df = df.copy()
    norm_to_orig = {}
    for c in df.columns:
        norm_to_orig.setdefault(_norm_key(c), c)  # first occurrence wins
    rename, used, unit_source = {}, set(), {}
    for canon, keys in _COLUMN_ALIASES:
        for k in keys:
            src = norm_to_orig.get(k)
            if src is not None and src not in used:
                rename[src] = canon
                used.add(src)
                unit_source[canon] = str(src)
                break
    df = df.rename(columns=rename)

    def _num(col):
        return pd.to_numeric(df[col].astype(str).str.replace(",", "", regex=False)
                             .str.extract(r"(-?\d+\.?\d*)", expand=False), errors="coerce")

    for col, fill in _NUMERIC_FILL.items():
        if col in df.columns:
            df[col] = _num(col).fillna(fill)
    if "hours" in df.columns:
        df["hours"] = _num("hours").fillna(HOURS).replace(0, HOURS)
    if "quantity" in df.columns:
        df["quantity"] = _num("quantity").fillna(1).replace(0, 1)

    # Unit conversion for memory/disk based on the original header's unit token.
    for col in ("memory_gb", "storage_gb"):
        if col not in df.columns:
            continue
        tokens = _norm_key(unit_source.get(col, col)).split()
        factor = None
        if "mib" in tokens or "mb" in tokens:
            factor = 1 / 1024.0
        elif "tib" in tokens or "tb" in tokens:
            factor = 1024.0
        elif "kib" in tokens or "kb" in tokens:
            factor = 1 / (1024.0 * 1024.0)
        if factor:
            df[col] = df[col] * factor

    # Heuristic: memory with no explicit unit but implausibly large values is really MB.
    if "memory_gb" in df.columns:
        tokens = _norm_key(unit_source.get("memory_gb", "memory_gb")).split()
        has_unit = any(t in tokens for t in ("mib", "mb", "gib", "gb", "tib", "tb", "kib", "kb"))
        if not has_unit:
            nz = df["memory_gb"][df["memory_gb"] > 0]
            if len(nz) and nz.median() > 1024:
                df["memory_gb"] = df["memory_gb"] / 1024.0
    return df


_KEY_CANON = {"name", "vcpu", "memory_gb", "disposition", "os", "environment", "storage_gb",
              "role", "target", "azure_sku", "quantity"}


def _inventory_score(df):
    """How 'inventory-like' is this DataFrame? = count of recognized canonical columns."""
    if df is None or getattr(df, "empty", True):
        return -1
    try:
        cols = set(normalize_inventory(df.head(20)).columns)
    except Exception:
        return -1
    return len(_KEY_CANON & cols)


def read_inventory(source, filename=None):
    """Load an inventory from a path or file-like (CSV/XLSX) into the canonical schema.
    For multi-sheet workbooks, auto-picks the sheet that looks most like an inventory
    so users can upload workbooks where the data isn't on the first sheet."""
    name = (filename or getattr(source, "name", "") or str(source)).lower()
    if name.endswith((".xlsx", ".xls")):
        xl = pd.ExcelFile(source)
        best, best_score = None, -1
        for sheet in xl.sheet_names:
            try:
                d = xl.parse(sheet)
            except Exception:
                continue
            sc = _inventory_score(d)
            if sc > best_score:
                best, best_score = d, sc
        df = best if best is not None else pd.read_excel(source)
    else:
        df = pd.read_csv(source)
    return normalize_inventory(df)


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


def apply_row_edits(df, row_edits):
    """Apply AI 'row_edits' to the raw inventory DataFrame (in place on a copy) and return it.
    Each edit: {match: <name substr>, set: {vcpu, memory_gb, quantity, os, target,
    disposition, hours, unit_price}}. Matches on the 'name' column (case-insensitive substring)."""
    if df is None or df.empty or not row_edits:
        return df
    out = df.copy()
    cols = {c.strip().lower(): c for c in out.columns}
    key_map = {"vcpu": "vcpu", "memory_gb": "memory_gb", "quantity": "quantity", "os": "os",
               "target": "target", "disposition": "disposition", "hours": "hours",
               "unit_price": "unit_price"}
    name_col = cols.get("name")
    if not name_col:
        return out
    for edit in row_edits:
        match = str(edit.get("match", "")).strip()
        sets = edit.get("set") or {}
        if not match or not sets:
            continue
        mask = out[name_col].astype(str).str.contains(match, case=False, regex=False)
        if not mask.any():
            continue
        for k, v in sets.items():
            col_key = key_map.get(k)
            if not col_key:
                continue
            col = cols.get(col_key)
            if col is None:  # create the column if the inventory lacks it
                col = col_key
                out[col] = ""
                cols[col_key] = col
            out.loc[mask, col] = v
    return out


# ---------------------------------------------------------------- parallel prefetch
def _prefetch_prices(df, region, term, ahb, max_workers=8):
    """Warm the on-disk price cache concurrently.

    The estimate loop calls the Azure Retail Prices API once per unique SKU/service,
    but serially. On a cold cache that is the dominant cost (~1.5s/query). Here we
    resolve every row to the exact pricing call(s) it will make, dedupe, and fetch
    them in parallel so the subsequent loop hits a warm cache. Best-effort: any
    failure is ignored (the real loop will surface genuine pricing errors)."""
    tasks = {}  # dedupe-key -> zero-arg callable

    def add(key, fn):
        if key not in tasks:
            tasks[key] = fn

    for _, r in df.iterrows():
        role = str(r.get("role", "") or "")
        name = str(r.get("name", "") or "")
        disp = str(r.get("disposition", "") or "")
        if disp.lower() in ("nan", "none"):
            disp = ""
        override = str(r.get("target", "") or "").strip()
        if override.lower() in ("nan", "none", ""):
            override = ""
        vcpu = float(r.get("vcpu", 0) or 0)
        mem = float(r.get("memory_gb", 0) or 0)
        sku_hint = str(r.get("azure_sku", "") or "").strip()
        if sku_hint.lower() in ("nan", "none"):
            sku_hint = ""
        target, _ = resolve_target(disp, role, name, override)

        if target == "vm":
            sku = sku_hint or _pick(CFG["vm_catalog"], vcpu, mem)["sku"]
            add(("vm", sku, region), lambda s=sku: P.vm_price(s, region, "linux"))
        elif target == "aks":
            sku = sku_hint or _pick(CFG["vm_catalog"], vcpu, mem)["sku"]
            add(("vm", sku, region), lambda s=sku: P.vm_price(s, region, "linux"))
            add(("aksfee", region), lambda: P.aks_cluster_fee(region))
        elif target == "appservice":
            sku = sku_hint or _pick(CFG["appservice_catalog"], vcpu, mem)["sku"]
            add(("appsvc", sku, region), lambda s=sku: P.appservice_price(s, region))
        elif target == "redis":
            rc = _pick(CFG["redis_catalog"], 0, mem, key_v="mem", key_m="mem")
            add(("redis", rc["sku"], region), lambda rr=rc: P.redis_price(rr["sku"], region, rr["tier"]))
        elif target == "aca":
            add(("aca", region), lambda: P.aca_rates(region))
        elif target == "hyperscale":
            add(("hs", region), lambda: P.hyperscale_rates(region))
        elif target == "sqldb":
            add(("sqldb", region), lambda: P.sqldb_gp_reserved(region))
        # postgres/mysql/cosmos/saas/skip -> no live API call

    if not tasks:
        return
    with _cf.ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as ex:
        futs = [ex.submit(fn) for fn in tasks.values()]
        for f in _cf.as_completed(futs):
            try:
                f.result()
            except Exception:
                pass  # best-effort warming; real loop re-raises genuine errors


# ---------------------------------------------------------------- main estimate
def estimate(df, region="eastus", term="1y", ahb=False, resiliency=False):
    df = normalize_inventory(df)
    _prefetch_prices(df, region, term, ahb)
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
    summ = build_summary(lines, resiliency)
    return lines, summ


def build_summary(lines, resiliency=False):
    """Build the area-grouped summary DataFrame (with optional resiliency add-in + TOTAL)
    from a lines DataFrame. Kept separate so overrides can recompute without re-pricing."""
    if lines is None or lines.empty:
        return pd.DataFrame(columns=["area", "monthly", "annual"])
    lines = lines.copy()
    resil_total = 0.0
    if resiliency:
        prod = lines[lines["environment"].str.lower().str.startswith("prod")]
        db_ha = prod[prod["model"].str.contains("SQL|Hyperscale|PostgreSQL|MySQL|Cosmos", regex=True)]["monthly"].sum()
        vm_asr = prod[prod["model"].str.contains("VM|AKS")]["quantity"].sum() * CFG["addons"]["asr_instance_mo"]
        resil_total = db_ha + vm_asr

    def area(row):
        e = str(row["environment"]).lower()
        if not e.startswith("prod"):
            return "Non-Production"
        return "Prod - " + str(row["model"])
    lines["area"] = lines.apply(area, axis=1)
    summ = lines.groupby("area", as_index=False)["monthly"].sum()
    if resil_total:
        summ = pd.concat([summ, pd.DataFrame([{"area": "Resiliency Add-In", "monthly": resil_total}])],
                         ignore_index=True)
    summ["annual"] = summ["monthly"] * 12
    total = pd.DataFrame([{"area": "TOTAL", "monthly": summ["monthly"].sum(), "annual": summ["annual"].sum()}])
    summ = pd.concat([summ, total], ignore_index=True)
    return summ


def apply_overrides(lines, ov):
    """Apply AI/user pricing overrides to a priced lines DataFrame and return a new copy.

    ov keys (all optional):
      global_multiplier: float          -> scales every row's monthly
      by_model: {substr: mult}          -> scales rows whose 'model' contains substr (case-insensitive)
      by_name:  {substr: mult}          -> scales rows whose 'name' contains substr (case-insensitive)
      set_monthly: {substr: amount}     -> sets absolute monthly on rows whose 'name' contains substr
    """
    if lines is None or lines.empty or not ov:
        return lines
    df = lines.copy()
    monthly = df["monthly"].astype(float)

    gm = ov.get("global_multiplier")
    if isinstance(gm, (int, float)):
        monthly = monthly * float(gm)

    for substr, mult in (ov.get("by_model") or {}).items():
        if isinstance(mult, (int, float)):
            mask = df["model"].astype(str).str.contains(str(substr), case=False, regex=False)
            monthly = monthly.where(~mask, monthly * float(mult))

    for substr, mult in (ov.get("by_name") or {}).items():
        if isinstance(mult, (int, float)):
            mask = df["name"].astype(str).str.contains(str(substr), case=False, regex=False)
            monthly = monthly.where(~mask, monthly * float(mult))

    for substr, amount in (ov.get("set_monthly") or {}).items():
        if isinstance(amount, (int, float)):
            mask = df["name"].astype(str).str.contains(str(substr), case=False, regex=False)
            monthly = monthly.where(~mask, float(amount))

    df["monthly"] = monthly
    return df


# ---------------------------------------------------------------- modernization compare
def modernization(df, region="eastus", term="1y", ahb=False):
    """For each APP-type workload, compare cost across modernization paths."""
    df = normalize_inventory(df)
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
