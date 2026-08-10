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
DISK_GB_MO = CFG["addons"].get("managed_disk_premium_ssd_gb_mo", 0.12)
DEFAULT_CONTAINER_OPTIONS = {
    "container_strategy": "existing",
    "pool_aks": False,
    "aks_demand_factor": 0.50,
    "aks_target_utilization": 0.70,
    "aks_headroom": 0.20,
    "optimize_aca": False,
    "aca_prod_active_factor": 0.70,
    "aca_nonprod_active_factor": 0.35,
}


# ---------------------------------------------------------------- column mapping
# Ordered canonical schema -> accepted header variants. Earlier canonicals claim a
# source column first. Lets users upload inventories in many different formats.
_COLUMN_ALIASES = [
    ("name", ["name", "host name", "hostname", "server name", "servername", "server",
              "vm name", "vm", "machine name", "host", "node name", "computer name"]),
    ("environment", ["environment", "env", "tier", "stage"]),
    ("role", ["role", "server type", "workload type", "workload", "type", "function",
              "application", "app", "app name", "component"]),
    ("disposition", ["disposition", "migration disposition", "migration strategy",
                     "strategy", "6r", "r disposition", "recommendation"]),
    ("target", ["target", "azure target", "target service", "azure service", "target platform"]),
    ("vcpu", ["vcpu", "vcpus", "cpu", "cpus", "cores", "core", "processors",
              "req vcpu", "vcpu count", "num cpu", "cpu count", "logical processors"]),
    ("memory_gb", ["memory gb", "memory", "ram", "ram gb", "mem", "memory gib",
                   "ram gib", "memory mb", "memory mib", "ram mb", "ram mib", "memory mb "]),
    ("os", ["os", "os according to the configuration file",
            "os according to the vmware tools", "operating system", "platform",
            "os type", "guest os", "guest os full name"]),
    ("storage_gb", ["storage gb", "provisioned mib", "provisioned", "storage", "disk",
                    "disk gb", "disk capacity", "total disk capacity mib",
                    "total disk capacity", "total disk", "storage gib",
                    "used storage gb", "provisioned storage gb", "disk mib", "in use mib"]),
    ("quantity", ["quantity", "qty", "count", "instances", "instance count", "nodes", "node count"]),
    ("hours", ["hours", "hours mo", "hours month", "monthly hours", "run hours"]),
    ("azure_sku", ["azure sku", "sku", "vm sku", "instance type", "azure type", "vm size", "size"]),
    ("unit_price", ["unit price", "license cost", "price per user", "unit cost", "list price"]),
]
_NUMERIC = ["vcpu", "memory_gb", "storage_gb", "quantity", "hours", "unit_price"]


def _resolve_os(value, default_os="linux"):
    """Normalize a free-text OS value to 'windows' or 'linux'.
    Falls back to default_os when the field is blank/missing."""
    s = str(value or "").strip().lower()
    if s in ("", "nan", "none"):
        return default_os
    if "win" in s:
        return "windows"
    if any(t in s for t in ("linux", "rhel", "red hat", "ubuntu", "centos", "suse",
                            "debian", "oracle linux", "rocky", "alma", "amazon linux")):
        return "linux"
    return default_os


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


def container_options(options=None):
    """Return validated container-cost assumptions with safe defaults."""
    out = dict(DEFAULT_CONTAINER_OPTIONS)
    out.update(options or {})
    if out["container_strategy"] not in ("existing", "aks", "aca"):
        out["container_strategy"] = "existing"
    for key in ("aks_demand_factor", "aks_target_utilization", "aca_prod_active_factor",
                "aca_nonprod_active_factor"):
        out[key] = min(max(float(out[key]), 0.01), 1.0)
    out["aks_headroom"] = min(max(float(out["aks_headroom"]), 0.0), 1.0)
    out["pool_aks"] = bool(out["pool_aks"])
    out["optimize_aca"] = bool(out["optimize_aca"])
    return out


def _env_group(environment):
    value = str(environment or "").strip().lower()
    return "Prod" if value in ("prod", "production") or value.startswith("prod") else "NonProd"


def _aks_compute_factor(options):
    return (options["aks_demand_factor"] * (1.0 + options["aks_headroom"])
            / options["aks_target_utilization"])


def _aca_active_factor(environment, options):
    return (options["aca_prod_active_factor"] if _env_group(environment) == "Prod"
            else options["aca_nonprod_active_factor"])


def resolve_target(disposition, role, name, explicit):
    """Return a concrete target key using disposition + role. Defaults to IaaS 'vm'."""
    if explicit:
        ex = explicit.lower()
        if ex in ("netapp", "netappfiles", "azure netapp files", "nas"):
            return "anf", "explicit"
        return ex, "explicit"
    # File/NAS storage workloads -> Azure NetApp Files, regardless of disposition bucket.
    t = f"{role} {name}".lower()
    if any(k in t for k in ("netapp", "anf", "nas", "file server", "fileserver",
                            "file share", "fileshare", "nfs")):
        return "anf", str(disposition or "").strip().lower() or "file-storage"
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
# Each returns dict: {sku, model, rate_basis, monthly, compute_monthly, storage_monthly}
# monthly always == compute_monthly + storage_monthly. storage_sku (optional) labels
# the storage line item emitted by estimate().
def cost_vm(vcpu, mem, os_type, hours, qty, term, region, ahb=False, sku=None, storage=0.0):
    sku = sku or _pick(CFG["vm_catalog"], vcpu, mem)["sku"]
    pr = P.vm_price(sku, region, os_type)
    if not pr:
        return {"sku": sku, "model": "IaaS", "rate_basis": "N/A", "monthly": 0.0,
                "compute_monthly": 0.0, "storage_monthly": 0.0, "note": "price not found"}
    is_win = os_type.lower().startswith("win")
    lin = P.vm_price(sku, region, "linux") if is_win else pr
    basis = {"payg": "PAYG", "1y": "1yr SP", "3y": "3yr SP"}[term]

    if ahb and is_win and lin:
        # Azure Hybrid Benefit strips the Windows license -> pure Linux compute (savings-plan aware)
        rate = {"payg": lin["payg"], "1y": lin.get("sp1y"), "3y": lin.get("sp3y")}.get(term) or lin["payg"]
        basis += "+AHB"
    else:
        rate = {"payg": pr["payg"], "1y": pr.get("sp1y"), "3y": pr.get("sp3y")}.get(term)
        if rate is None:
            # The Windows meter often carries no savings plan; savings apply to the compute
            # portion only. Derive: Linux compute savings-plan rate + Windows license surcharge.
            if is_win and lin:
                lin_rate = {"1y": lin.get("sp1y"), "3y": lin.get("sp3y")}.get(term)
                if lin_rate is not None and lin.get("payg") is not None and pr.get("payg") is not None:
                    surcharge = max(pr["payg"] - lin["payg"], 0.0)
                    rate = lin_rate + surcharge
            if rate is None:
                rate = pr["payg"]
                basis = "PAYG"
    comp = qty * rate * hours
    disk = qty * float(storage or 0.0) * DISK_GB_MO
    return {"sku": sku, "model": "IaaS (VM)", "rate_basis": basis, "monthly": comp + disk,
            "compute_monthly": comp, "storage_monthly": disk,
            "storage_sku": "Managed Disk (Premium SSD)"}


def cost_aca(vcpu, mem, hours, qty, term, region, profile="auto"):
    aca = P.aca_rates(region)
    if profile == "dedicated" or (profile == "auto" and hours >= HOURS - 1):
        suffix = {"payg": "", "1y": "_1y", "3y": "_3y"}[term]
        dv = aca.get(f"ded_vcpu_hr{suffix}")
        dm = aca.get(f"ded_mem_hr{suffix}")
        dv = dv if dv is not None else aca.get("ded_vcpu_hr", 0.0)
        dm = dm if dm is not None else aca.get("ded_mem_hr", 0.0)
        m = qty * (vcpu * dv + mem * dm) * hours
        return {"sku": "ACA Dedicated", "model": "Container Apps",
                "rate_basis": {"payg": "PAYG", "1y": "1yr SP", "3y": "3yr SP"}[term],
                "monthly": m, "compute_monthly": m, "storage_monthly": 0.0}
    suffix = {"payg": "", "1y": "_1y", "3y": "_3y"}[term]
    cv = aca.get(f"cons_vcpu_sec{suffix}")
    cm = aca.get(f"cons_mem_sec{suffix}")
    cv = cv if cv is not None else aca.get("cons_vcpu_sec", 0.0)
    cm = cm if cm is not None else aca.get("cons_mem_sec", 0.0)
    m = qty * (vcpu * cv + mem * cm) * hours * 3600
    basis = {"payg": "PAYG active-hrs", "1y": "1yr SP active-hrs",
             "3y": "3yr SP active-hrs"}[term]
    return {"sku": "ACA Consumption", "model": "Container Apps", "rate_basis": basis,
            "monthly": m, "compute_monthly": m, "storage_monthly": 0.0}


def cost_aks(vcpu, mem, hours, qty, term, region, sku=None, storage=0.0,
             include_cluster_fee=True, compute_factor=1.0):
    node = _pick(CFG["vm_catalog"], vcpu, mem)
    nsku = sku or node["sku"]
    pr = P.vm_price(nsku, region, "linux")
    if not pr:
        return {"sku": f"AKS {qty}x{nsku}", "model": "Container (AKS)",
                "rate_basis": "N/A", "monthly": 0.0, "compute_monthly": 0.0,
                "storage_monthly": 0.0, "note": "price not found"}
    rate = {"payg": pr["payg"], "1y": pr["sp1y"], "3y": pr["sp3y"]}.get(term) or pr["payg"]
    nodes_cost = qty * rate * hours * max(float(compute_factor), 0.0)
    fee = P.aks_cluster_fee(region) * HOURS if include_cluster_fee else 0.0
    disk = qty * float(storage or 0.0) * DISK_GB_MO
    comp = nodes_cost + fee
    return {"sku": f"AKS {qty}x{nsku}", "model": "Container (AKS)",
            "rate_basis": {"payg": "PAYG", "1y": "1yr SP", "3y": "3yr SP"}[term],
            "monthly": comp + disk, "compute_monthly": comp, "storage_monthly": disk,
            "storage_sku": "Managed Disk (Premium SSD)"}


def cost_appservice(vcpu, mem, hours, qty, term, region, sku=None):
    plan = _pick(CFG["appservice_catalog"], vcpu, mem)
    psku = sku or plan["sku"]
    pr = P.appservice_price(psku, region)
    if not pr:
        return {"sku": psku, "model": "PaaS (App Service)", "rate_basis": "N/A",
                "monthly": 0.0, "compute_monthly": 0.0, "storage_monthly": 0.0}
    rate = {"payg": pr["payg"], "1y": pr["sp1y"], "3y": pr["sp3y"]}.get(term) or pr["payg"]
    m = qty * rate * hours
    return {"sku": f"App Service {psku}", "model": "PaaS (App Service)",
            "rate_basis": {"payg": "PAYG", "1y": "1yr SP", "3y": "3yr SP"}[term],
            "monthly": m, "compute_monthly": m, "storage_monthly": 0.0}


def cost_hyperscale(vcore, storage, qty, term, region):
    hs = P.hyperscale_rates(region)
    if term == "payg":
        comp = vcore * hs["compute_payg_hr"] * HOURS; basis = "PAYG"
    elif term == "3y" and hs["reserved_3y_vcore_yr"]:
        comp = vcore * hs["reserved_3y_vcore_yr"] / 36.0; basis = "3yr Reserved"
    else:
        comp = vcore * hs["reserved_1y_vcore_yr"] / 12.0; basis = "1yr Reserved"
    cm = qty * comp
    sm = qty * storage * hs["storage_gb_mo"]
    return {"sku": f"Hyperscale {int(vcore)} vCore", "model": "PaaS (SQL Hyperscale)",
            "rate_basis": basis, "monthly": cm + sm, "compute_monthly": cm, "storage_monthly": sm,
            "storage_sku": "Hyperscale storage"}


def cost_sqldb(vcore, storage, qty, term, region):
    r = P.sqldb_gp_reserved(region)
    if term == "payg":
        comp = vcore * r["compute_payg_hr"] * HOURS; basis = "PAYG"
    elif term == "3y":
        comp = vcore * r["r3y"] / 36.0; basis = "3yr Reserved"
    else:
        comp = vcore * r["r1y"] / 12.0; basis = "1yr Reserved"
    cm = qty * comp
    sm = qty * storage * r["storage_gb_mo"]
    return {"sku": f"SQL DB GP {int(vcore)} vCore", "model": "PaaS (Azure SQL DB)",
            "rate_basis": basis, "monthly": cm + sm, "compute_monthly": cm, "storage_monthly": sm,
            "storage_sku": "SQL DB storage"}


def cost_flexdb(kind, vcore, storage, qty, term, region):
    vhr = PDB[f"{kind}_gp_vcore_hr"]; shr = PDB[f"{kind}_storage_gb_mo"]
    comp = vcore * vhr * HOURS * TF[term]
    cm = qty * comp
    sm = qty * storage * shr
    label = {"postgres": "PaaS (PostgreSQL Flex)", "mysql": "PaaS (MySQL Flex)"}[kind]
    return {"sku": f"{kind.title()} GP {int(vcore)} vCore", "model": label,
            "rate_basis": f"{term} (approx)", "monthly": cm + sm,
            "compute_monthly": cm, "storage_monthly": sm,
            "storage_sku": f"{kind.title()} Flex storage"}


def cost_redis(mem, qty, term, region):
    r = _pick(CFG["redis_catalog"], 0, mem, key_v="mem", key_m="mem")
    pr = P.redis_price(r["sku"], region, r["tier"])
    rate = pr["payg"] if pr else 0.138
    m = qty * rate * HOURS * TF[term]
    return {"sku": f"Redis {r['tier']} {r['sku']}", "model": "PaaS (Cache for Redis)",
            "rate_basis": f"{term} (approx)", "monthly": m,
            "compute_monthly": m, "storage_monthly": 0.0}


def cost_cosmos(ru, storage, qty, term, region):
    ru = ru or 4000
    comp = (ru / 100.0) * PDB["cosmos_ru_100_hr"] * HOURS * TF[term]
    cm = qty * comp
    sm = qty * storage * PDB["cosmos_storage_gb_mo"]
    return {"sku": f"Cosmos {int(ru)} RU/s", "model": "PaaS (Cosmos DB)",
            "rate_basis": f"{term} (approx)", "monthly": cm + sm,
            "compute_monthly": cm, "storage_monthly": sm,
            "storage_sku": "Cosmos DB storage"}


def cost_saas(users, unit_price):
    m = (users or 0) * (unit_price or 0)
    return {"sku": "SaaS license", "model": "SaaS (per-user)", "rate_basis": "license",
            "monthly": m, "compute_monthly": m, "storage_monthly": 0.0}


def cost_anf(storage, qty, region, tier="Standard", term="payg"):
    """Azure NetApp Files: provisioned capacity priced $/GiB-month. Storage-only service
    (no compute line). Minimum provisioned capacity pool is 100 GiB. The pricing term
    selects the offering: 'payg' = Consumption, '1y'/'3y' = Reserved ('bulk') capacity."""
    tier = (tier or "Standard").strip().capitalize()
    gib = max(float(storage or 0), 100.0)  # ANF minimum capacity pool is 100 GiB
    if term == "1y":
        rate = P.netapp_files_reserved(region, tier)["r1y"]; basis = f"{tier} Reserved 1yr (bulk)"
    elif term == "3y":
        rate = P.netapp_files_reserved(region, tier)["r3y"]; basis = f"{tier} Reserved 3yr (bulk)"
    else:
        rate = P.netapp_files_rate(region, tier); basis = f"{tier} Consumption $/GiB-mo"
    sm = qty * gib * rate
    return {"sku": f"ANF {tier} ({int(gib)} GiB)", "model": "PaaS (Azure NetApp Files)",
            "rate_basis": basis, "monthly": sm,
            "compute_monthly": 0.0, "storage_monthly": sm,
            "storage_sku": f"Azure NetApp Files {tier}"}


# ---------------------------------------------------------------- AI/manual overrides
def apply_row_edits(df, row_edits):
    """Apply AI 'row_edits' to the raw inventory DataFrame (on a copy) and return it.
    Each edit: {match: <name substr>, set: {name, environment, role, vcpu, memory_gb,
    storage_gb, quantity, os, target, disposition, hours, unit_price}}. Matches on 'name'
    (case-insensitive substring)."""
    if df is None or getattr(df, "empty", True) or not row_edits:
        return df
    out = df.copy()
    cols = {c.strip().lower(): c for c in out.columns}
    keys = ("name", "environment", "role", "vcpu", "memory_gb", "storage_gb", "quantity",
            "os", "target", "disposition", "hours", "unit_price")
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
            if k not in keys:
                continue
            col = cols.get(k)
            if col is None:
                col = k
                out[col] = ""
                cols[k] = col
            out.loc[mask, col] = v
    return out


def apply_row_ops(df, row_ops):
    """Apply structural AI 'row_ops' to the raw inventory DataFrame (on a copy) and return it.
    Each op is one of:
      {"op": "delete", "match": "<name substr>"}      -> drop rows whose name contains match
      {"op": "dedupe", "subset": ["name", ...]}        -> drop duplicate rows (subset optional)
      {"op": "add", "set": {name, environment, role, disposition, target, vcpu, memory_gb,
                            os, storage_gb, quantity, hours, unit_price}}  -> append a new row
    """
    if df is None or getattr(df, "empty", True) or not row_ops:
        return df
    out = df.copy()
    cols = {c.strip().lower(): c for c in out.columns}
    name_col = cols.get("name")
    add_keys = ("name", "environment", "role", "disposition", "target", "vcpu", "memory_gb",
                "os", "storage_gb", "quantity", "hours", "unit_price")
    for op in row_ops:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op", "")).strip().lower()
        if kind == "delete" and name_col is not None:
            match = str(op.get("match", "")).strip()
            if match:
                mask = out[name_col].astype(str).str.contains(match, case=False, regex=False)
                out = out[~mask]
        elif kind == "dedupe":
            subset = op.get("subset")
            sub = None
            if isinstance(subset, list) and subset:
                sub = [cols.get(str(s).strip().lower()) for s in subset]
                sub = [s for s in sub if s] or None
            # Normalize to string for a robust duplicate comparison, then keep first.
            keyframe = out.astype(str)
            dup_mask = keyframe.duplicated(subset=sub, keep="first")
            out = out[~dup_mask.values]
        elif kind == "add":
            sets = op.get("set") or {}
            row = {}
            for k, v in sets.items():
                lk = str(k).strip().lower()
                if lk in add_keys:
                    row[cols.get(lk, lk)] = v
            if row:
                out = pd.concat([out, pd.DataFrame([row])], ignore_index=True)
    return out.reset_index(drop=True)


def build_summary(lines, resiliency=False):
    """Build the area-grouped summary DataFrame (optional resiliency add-in + TOTAL)
    from a lines DataFrame. Separate so overrides can recompute without re-pricing."""
    if lines is None or lines.empty:
        return pd.DataFrame(columns=["area", "monthly", "annual"])
    lines = lines.copy()
    resil_total = 0.0
    if resiliency:
        prod = lines[lines["environment"].str.lower().str.startswith("prod")]
        db_ha = prod[prod["model"].str.contains("SQL|Hyperscale|PostgreSQL|MySQL|Cosmos", regex=True)]["monthly"].sum()
        protectable = prod[~prod["role"].astype(str).eq("AKS platform")]
        vm_asr = (protectable[protectable["model"].str.contains("VM|AKS")]["quantity"].sum()
                  * CFG["addons"]["asr_instance_mo"])
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
    """Apply pricing overrides to a priced lines DataFrame and return a new copy.
    ov keys (all optional): global_multiplier (float), by_model {substr: mult},
    by_name {substr: mult}, set_monthly {substr: absolute $/month}."""
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


def inventory_stats(df, max_names=200):
    """Deterministic full-sheet duplicate/row analytics (never truncated), so the UI and the
    AI assistant can report accurate counts regardless of how many rows fit in the LLM context.
    Returns row_count, unique_names, name_duplicate_rows (rows removed if deduped by name,
    keeping first), duplicate_name_groups, full_row_duplicate_rows, and duplicate_names
    (a {name: occurrences} map, capped at max_names, highest first)."""
    try:
        df = _normalize_columns(df)
    except Exception:  # noqa: BLE001
        pass
    if df is None or getattr(df, "empty", True):
        return {"row_count": 0}
    n = int(len(df))
    stats = {"row_count": n, "full_row_duplicate_rows": int(df.astype(str).duplicated().sum())}
    if "name" in df.columns:
        vc = df["name"].astype(str).value_counts()
        dupe = vc[vc > 1]
        stats["unique_names"] = int(vc.size)
        stats["name_duplicate_rows"] = int((dupe - 1).sum())
        stats["duplicate_name_groups"] = int(dupe.size)
        stats["duplicate_names"] = {str(k): int(v) for k, v in list(dupe.items())[:max_names]}
    return stats


# ---------------------------------------------------------------- main estimate
def estimate(df, region="eastus", term="1y", ahb=False, resiliency=False, default_os="linux",
             container_opts=None):
    df = _normalize_columns(df)
    opts = container_options(container_opts)
    rows = []
    aks_groups = set()
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
        os_type = _resolve_os(r.get("os"), default_os)
        storage = float(r.get("storage_gb", 0) or 0)
        hours = float(r.get("hours", HOURS) or HOURS)
        sku_hint = str(r.get("azure_sku", "") or "").strip()
        if sku_hint.lower() in ("nan", "none"):
            sku_hint = ""
        unit_price = float(r.get("unit_price", 0) or 0)

        target, disp_used = resolve_target(disp, role, name, override)
        if (opts["container_strategy"] in ("aks", "aca")
                and target not in ("skip", "saas")
                and _role_cat(role, name) == "app"):
            target = opts["container_strategy"]
        base = {"name": name, "environment": env, "role": role, "disposition": disp or "(none->IaaS)",
                "target": target, "quantity": qty, "region": region,
                "os": os_type, "vcpu": vcpu, "memory_gb": mem, "storage_gb": storage}
        if target == "skip":
            continue

        if target == "vm":
            c = cost_vm(vcpu, mem, os_type, hours, qty, term, region, ahb, sku_hint or None, storage)
        elif target == "aca":
            if opts["optimize_aca"]:
                active_hours = hours * _aca_active_factor(env, opts)
                c = cost_aca(vcpu, mem, active_hours, qty, term, region, profile="consumption")
                c["rate_basis"] += f" ({_aca_active_factor(env, opts):.0%} active)"
            else:
                c = cost_aca(vcpu, mem, hours, qty, term, region)
        elif target == "aks":
            if opts["pool_aks"]:
                aks_groups.add(_env_group(env))
                c = cost_aks(vcpu, mem, hours, qty, term, region, sku_hint or None, storage,
                             include_cluster_fee=False, compute_factor=_aks_compute_factor(opts))
                c["rate_basis"] += " pooled"
            else:
                c = cost_aks(vcpu, mem, hours, qty, term, region, sku_hint or None, storage)
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
        elif target in ("anf", "netapp"):
            _anf_tier = sku_hint.capitalize() if sku_hint.lower() in ("standard", "premium", "ultra") else "Standard"
            c = cost_anf(storage, qty, region, tier=_anf_tier, term=term)
        else:
            c = cost_vm(vcpu, mem, os_type, hours, qty, term, region, ahb, None, storage)
        base.update(c)
        base["hours"] = hours

        storage_monthly = float(c.get("storage_monthly", 0.0) or 0.0)
        compute_monthly = float(c.get("compute_monthly", c.get("monthly", 0.0)) or 0.0)
        storage_sku = c.get("storage_sku") or f"{c.get('model', '')} storage"

        comp_row = {k: v for k, v in base.items()
                    if k not in ("compute_monthly", "storage_monthly", "storage_sku")}
        comp_row["component"] = "Compute"
        comp_row["monthly"] = compute_monthly
        comp_row["storage_gb"] = 0.0
        # Skip a zero-cost compute row for storage-only services (e.g. Azure NetApp Files).
        if compute_monthly > 0 or storage_monthly <= 0:
            rows.append(comp_row)

        if storage_monthly > 0:
            stor_row = {k: v for k, v in base.items()
                        if k not in ("compute_monthly", "storage_monthly", "storage_sku")}
            stor_row["component"] = "Storage"
            stor_row["monthly"] = storage_monthly
            stor_row["sku"] = storage_sku
            # Storage-only services (e.g. ANF) keep their real basis (Consumption/Reserved);
            # split-storage lines of compute services get a generic storage label.
            stor_row["rate_basis"] = c.get("rate_basis", "storage $/GB-mo") \
                if compute_monthly <= 0 else "storage $/GB-mo"
            stor_row["vcpu"] = 0.0
            stor_row["memory_gb"] = 0.0
            rows.append(stor_row)

    if opts["pool_aks"]:
        fee = P.aks_cluster_fee(region) * HOURS
        for group in sorted(aks_groups):
            rows.append({
                "name": f"Shared AKS Cluster ({group})", "environment": group,
                "role": "AKS platform", "disposition": "Shared container platform",
                "target": "aks", "quantity": 1, "region": region, "os": "linux",
                "vcpu": 0.0, "memory_gb": 0.0, "storage_gb": 0.0,
                "sku": f"AKS Shared Cluster - {group}", "model": "Container (AKS)",
                "rate_basis": "Shared control plane", "monthly": fee,
                "component": "Compute", "hours": HOURS,
            })

    lines = pd.DataFrame(rows)
    summ = build_summary(lines, resiliency)
    return lines, summ


# ---------------------------------------------------------------- modernization compare
def modernization(df, region="eastus", term="1y", ahb=False, default_os="linux",
                  container_opts=None):
    """For each APP-type workload, compare cost across modernization paths."""
    df = _normalize_columns(df)
    opts = container_options(container_opts)
    candidates = []
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
        env = str(r.get("environment", "Prod") or "Prod")
        os_type = _resolve_os(r.get("os"), default_os)
        hours = float(r.get("hours", HOURS) or HOURS)
        qty = int(float(r.get("quantity", 1) or 1))
        candidates.append({
            "name": name,
            "environment": env,
            "Rehost (VM)": round(cost_vm(vcpu, mem, os_type, hours, qty, term, region, ahb)["monthly"], 2),
            "Replatform (App Service)": round(cost_appservice(vcpu, mem, hours, qty, term, region)["monthly"], 2),
            "Containerize (AKS - Per App)": round(
                cost_aks(vcpu, mem, hours, qty, term, region)["monthly"], 2),
            "_aks_shared_compute": cost_aks(
                vcpu, mem, hours, qty, term, region, include_cluster_fee=False,
                compute_factor=_aks_compute_factor(opts))["monthly"],
            "Modernize (Container Apps - Always On)": round(
                cost_aca(vcpu, mem, hours, qty, term, region)["monthly"], 2),
            "Modernize (Container Apps - Optimized)": round(
                cost_aca(vcpu, mem, hours * _aca_active_factor(env, opts), qty, term, region,
                         profile="consumption")["monthly"], 2),
        })
    group_counts = {}
    for row in candidates:
        group = _env_group(row["environment"])
        group_counts[group] = group_counts.get(group, 0) + 1
    fee = P.aks_cluster_fee(region) * HOURS
    out = []
    for row in candidates:
        group = _env_group(row["environment"])
        row["Containerize (AKS - Shared)"] = round(
            row.pop("_aks_shared_compute") + fee / group_counts[group], 2)
        shared = row["Containerize (AKS - Shared)"]
        per_app = row["Containerize (AKS - Per App)"]
        always_on = row["Modernize (Container Apps - Always On)"]
        optimized = row["Modernize (Container Apps - Optimized)"]
        selected_aks = shared if opts["pool_aks"] else per_app
        selected_aca = optimized if opts["optimize_aca"] else always_on
        row["Selected AKS Scenario"] = selected_aks
        row["Selected Container Apps Scenario"] = selected_aca
        valid = [value for value in (selected_aks, selected_aca) if value > 0]
        row["Selected Container Option"] = round(min(valid), 2) if valid else 0.0
        out.append(row)
    comp = pd.DataFrame(out)
    if not comp.empty:
        totals = {"name": "TOTAL", "environment": ""}
        for c in comp.columns:
            if c not in ("name", "environment"):
                totals[c] = round(comp[c].sum(), 2)
        comp = pd.concat([comp, pd.DataFrame([totals])], ignore_index=True)
    return comp


def meta(region="eastus"):
    return {"hyperscale": P.hyperscale_rates(region), "aca": P.aca_rates(region),
            "sqldb_gp": P.sqldb_gp_reserved(region), "aks_cluster_fee_hr": P.aks_cluster_fee(region),
            "blob_hot_lrs": P.blob_hot_lrs(region)}
