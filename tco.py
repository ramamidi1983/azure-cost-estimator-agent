"""On-premises TCO model for migration ROI.

Sizes an on-prem estate from the loaded inventory footprint (total vCPU, RAM,
storage and VM count) and computes an annualized total cost of ownership across
the usual capex/opex categories. All benchmark assumptions are editable in the
UI - the defaults below are industry-approximate list-price ballparks, not
quotes. Results are compared against the computed Azure cost to show savings,
ROI and payback.
"""
from __future__ import annotations

import math
import pandas as pd

# Cost categories used for the breakdown table / chart, in display order.
CATEGORIES = [
    "Server hardware",
    "Storage hardware",
    "Platform & OS licensing",
    "Hardware support",
    "Power & cooling",
    "Datacenter facilities",
    "Network",
    "IT admin labor",
]

# Assumptions shared by every platform (host sizing + facility/opex benchmarks).
_COMMON = {
    "cores_per_host": 32,          # physical cores per server
    "vcpu_per_core": 4.0,          # virtualization overcommit ratio
    "ram_per_host_gb": 512,        # usable RAM per server
    "headroom_factor": 1.25,       # N+1 / HA + growth spare capacity
    "hardware_life_years": 5,      # capex amortization period
    "host_capex": 18000.0,         # $ per dual-socket server (one-time)
    "storage_capex_per_gb": 0.25,  # $/GB usable enterprise storage (one-time)
    "storage_usable_factor": 0.70, # usable/raw after RAID + overhead
    "power_watts_per_host": 500,   # average draw per server
    "pue": 1.6,                    # datacenter power usage effectiveness
    "kwh_cost": 0.12,              # $/kWh blended
    "facilities_per_host_year": 1500.0,  # rack space, cooling infra, etc.
    "network_per_host_year": 500.0,      # switch ports, cabling, bandwidth
    "vms_per_admin": 150,          # VMs managed per FTE (virtualized estate)
    "admin_fte_cost": 130000.0,    # fully-loaded annual $ per FTE
    "hw_support_pct": 0.10,        # annual HW+SW support as % of hardware capex
    "azure_ops_pct": 0.40,         # Azure ops labor as fraction of on-prem admin labor
}

# Per-platform licensing (annual $ per physical core, incl. guest OS entitlement).
PLATFORMS = {
    "VMware vSphere": {
        **_COMMON,
        "platform_lic_per_core_year": 350.0,  # VMware VVF/VCF per-core subscription
    },
    "Red Hat OpenShift": {
        **_COMMON,
        "platform_lic_per_core_year": 300.0,  # OpenShift + RHEL per-core subscription
    },
    "Bare-metal / Hyper-V": {
        **_COMMON,
        "platform_lic_per_core_year": 120.0,  # Windows Server Datacenter (SA) per core
    },
}

HOURS_PER_YEAR = 8760


def footprint(inv: pd.DataFrame) -> dict:
    """Aggregate the total resource footprint from an inventory DataFrame."""
    if inv is None or getattr(inv, "empty", True):
        return {"vcpu": 0.0, "memory_gb": 0.0, "storage_gb": 0.0, "vm_count": 0}

    def col(name):
        if name in inv.columns:
            return pd.to_numeric(inv[name], errors="coerce").fillna(0.0)
        return pd.Series([0.0] * len(inv))

    qty = col("quantity")
    qty = qty.mask(qty <= 0, 1.0) if len(qty) else qty
    return {
        "vcpu": float((col("vcpu") * qty).sum()),
        "memory_gb": float((col("memory_gb") * qty).sum()),
        "storage_gb": float((col("storage_gb") * qty).sum()),
        "vm_count": int(qty.sum()),
    }


def size_hosts(fp: dict, a: dict) -> dict:
    """Number of physical hosts required to run the footprint, with headroom."""
    cores = max(int(a["cores_per_host"]), 1)
    vcpu_cap = cores * float(a["vcpu_per_core"])          # vCPU per host
    ram_cap = max(float(a["ram_per_host_gb"]), 1.0)       # GB per host
    by_cpu = fp["vcpu"] / vcpu_cap if vcpu_cap else 0.0
    by_ram = fp["memory_gb"] / ram_cap if ram_cap else 0.0
    base = max(by_cpu, by_ram)
    hosts = math.ceil(base * float(a["headroom_factor"])) if base > 0 else 0
    hosts = max(hosts, 1) if fp["vm_count"] > 0 else 0
    return {
        "hosts": hosts,
        "licensed_cores": hosts * cores,
        "bound": "CPU" if by_cpu >= by_ram else "Memory",
    }


def compute_tco(fp: dict, a: dict) -> dict:
    """Annualized on-prem TCO breakdown (dict of category -> annual $)."""
    hs = size_hosts(fp, a)
    hosts, cores = hs["hosts"], hs["licensed_cores"]
    life = max(int(a["hardware_life_years"]), 1)

    server_capex = hosts * float(a["host_capex"])
    raw_storage_gb = fp["storage_gb"] / max(float(a["storage_usable_factor"]), 0.01)
    storage_capex = raw_storage_gb * float(a["storage_capex_per_gb"])
    hardware_capex = server_capex + storage_capex

    power_kwh = (hosts * float(a["power_watts_per_host"]) / 1000.0
                 * float(a["pue"]) * HOURS_PER_YEAR)
    admin_ftes = (fp["vm_count"] / max(float(a["vms_per_admin"]), 1.0)) if fp["vm_count"] else 0.0

    breakdown = {
        "Server hardware": server_capex / life,
        "Storage hardware": storage_capex / life,
        "Platform & OS licensing": cores * float(a["platform_lic_per_core_year"]),
        "Hardware support": hardware_capex * float(a["hw_support_pct"]),
        "Power & cooling": power_kwh * float(a["kwh_cost"]),
        "Datacenter facilities": hosts * float(a["facilities_per_host_year"]),
        "Network": hosts * float(a["network_per_host_year"]),
        "IT admin labor": admin_ftes * float(a["admin_fte_cost"]),
    }
    annual = sum(breakdown.values())
    return {
        "hosts": hosts,
        "licensed_cores": cores,
        "bound": hs["bound"],
        "hardware_capex": hardware_capex,
        "admin_ftes": admin_ftes,
        "admin_labor": breakdown["IT admin labor"],
        "breakdown": breakdown,
        "annual": annual,
        "monthly": annual / 12.0,
    }


def breakdown_frame(tco: dict) -> pd.DataFrame:
    """Category breakdown as a tidy DataFrame for tables/charts."""
    rows = [{"Category": c, "Annual": round(tco["breakdown"].get(c, 0.0), 0)}
            for c in CATEGORIES]
    return pd.DataFrame(rows)


def roi(onprem_annual: float, azure_annual: float, migration_cost: float,
        years: int = 3, azure_ops_annual: float = 0.0) -> dict:
    """Multi-year savings, ROI% and simple payback vs the Azure run-rate.

    ``azure_ops_annual`` adds cloud operations labor to the Azure side so the
    comparison is not on-prem-with-admin vs Azure-with-none.
    """
    azure_annual_all = azure_annual + azure_ops_annual
    onprem = onprem_annual * years
    azure = azure_annual_all * years
    invest = azure + migration_cost
    net_savings = onprem - invest
    monthly_savings = (onprem_annual - azure_annual_all) / 12.0
    payback_months = (migration_cost / monthly_savings) if monthly_savings > 0 else None
    return {
        "years": years,
        "onprem_total": onprem,
        "azure_total": azure,
        "azure_ops_annual": azure_ops_annual,
        "migration_cost": migration_cost,
        "net_savings": net_savings,
        "savings_pct": (net_savings / onprem * 100.0) if onprem > 0 else 0.0,
        "roi_pct": (net_savings / invest * 100.0) if invest > 0 else 0.0,
        "monthly_savings": monthly_savings,
        "payback_months": payback_months,
    }
