"""Azure Retail Prices API client with on-disk caching.

Fetches live PAYG, Savings Plan (1yr/3yr) and Reserved rates.
Docs: https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices
"""
import json, os, time, urllib.parse, urllib.request

API = "https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&$filter="
# Durable price cache on the Azure Files mount when PERSIST_DIR is set, else local dir.
_PERSIST = os.environ.get("PERSIST_DIR")
CACHE_DIR = os.path.join(_PERSIST, "price_cache") if _PERSIST else os.path.join(os.path.dirname(__file__), ".cache")
CACHE_TTL = 60 * 60 * 24  # 24h


def _cache_path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in key)[:150]
    return os.path.join(CACHE_DIR, safe + ".json")


def _query(filt: str, max_pages: int = 10):
    key = _cache_path(filt)
    if os.path.exists(key) and (time.time() - os.path.getmtime(key) < CACHE_TTL):
        with open(key, "r", encoding="utf-8") as f:
            return json.load(f)
    url = API + urllib.parse.quote(filt)
    items = []
    for _ in range(max_pages):
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.load(r)
        items += d.get("Items", [])
        url = d.get("NextPageLink")
        if not url:
            break
    with open(key, "w", encoding="utf-8") as f:
        json.dump(items, f)
    return items


def _sp(item):
    return {s.get("term"): s.get("retailPrice") for s in (item.get("savingsPlan") or [])}


def vm_price(sku: str, region: str, os_type: str = "linux"):
    """Return dict with payg + 1yr/3yr savings plan hourly rates for a VM SKU."""
    filt = (f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
            f"and armSkuName eq '{sku}' and priceType eq 'Consumption'")
    want_win = os_type.lower().startswith("win")
    for it in _query(filt):
        pn = it.get("productName", "")
        is_win = "Windows" in pn
        mn = it.get("meterName", "")
        if "Spot" in mn or "Low Priority" in mn:
            continue
        if is_win != want_win:
            continue
        sp = _sp(it)
        return {"sku": sku, "payg": it.get("retailPrice"),
                "sp1y": sp.get("1 Year"), "sp3y": sp.get("3 Years"),
                "meter": mn, "product": pn}
    return None


def hyperscale_rates(region: str):
    """SQL Hyperscale Gen5 compute (PAYG hr + 1yr/3yr reserved per vCore/yr) and storage."""
    out = {"compute_payg_hr": None, "reserved_1y_vcore_yr": None,
           "reserved_3y_vcore_yr": None, "storage_gb_mo": None, "io_1m": None}
    for it in _query(f"serviceName eq 'SQL Database' and armRegionName eq '{region}' and priceType eq 'Consumption'"):
        n = (it.get("meterName", "") + "|" + it.get("productName", "")).lower()
        if "hyperscale" in n and "compute" in n and "gen5" in n and "serverless" not in n:
            out["compute_payg_hr"] = it.get("retailPrice")
        if "hyperscale" in n and "data stored" in n and "backup" not in n and it.get("retailPrice") == 0.1:
            out["storage_gb_mo"] = it.get("retailPrice")
        if "hyperscale" in n and "io" in n and it.get("unitOfMeasure") == "1M":
            out["io_1m"] = it.get("retailPrice")
    for it in _query(f"serviceName eq 'SQL Database' and armRegionName eq '{region}' and priceType eq 'Reservation'"):
        n = (it.get("meterName", "") + "|" + it.get("productName", "")).lower()
        if "hyperscale" in n and "compute" in n and "gen5" in n and "serverless" not in n:
            if it.get("reservationTerm") == "1 Year":
                out["reserved_1y_vcore_yr"] = it.get("retailPrice")
            elif it.get("reservationTerm") == "3 Years":
                out["reserved_3y_vcore_yr"] = it.get("retailPrice")
    if out["storage_gb_mo"] is None:
        out["storage_gb_mo"] = 0.10
    return out


def aca_rates(region: str):
    """Azure Container Apps Dedicated + Consumption rates (with savings plan)."""
    out = {}
    for it in _query(f"serviceName eq 'Azure Container Apps' and armRegionName eq '{region}'"):
        mn = it.get("meterName", "")
        sp = _sp(it)
        rp = it.get("retailPrice")
        if mn == "Dedicated vCPU Usage":
            out["ded_vcpu_hr"] = rp
            out["ded_vcpu_hr_1y"] = sp.get("1 Year")
            out["ded_vcpu_hr_3y"] = sp.get("3 Years")
        elif mn == "Dedicated Memory Usage":
            out["ded_mem_hr"] = rp
            out["ded_mem_hr_1y"] = sp.get("1 Year")
            out["ded_mem_hr_3y"] = sp.get("3 Years")
        elif mn == "Dedicated Plan Management":
            out["ded_mgmt_hr"] = rp
            out["ded_mgmt_hr_1y"] = sp.get("1 Year")
            out["ded_mgmt_hr_3y"] = sp.get("3 Years")
        elif mn == "Standard vCPU Active Usage":
            out["cons_vcpu_sec"] = rp
            out["cons_vcpu_sec_1y"] = sp.get("1 Year")
            out["cons_vcpu_sec_3y"] = sp.get("3 Years")
        elif mn == "Standard Memory Active Usage":
            out["cons_mem_sec"] = rp
            out["cons_mem_sec_1y"] = sp.get("1 Year")
            out["cons_mem_sec_3y"] = sp.get("3 Years")
    return out


def blob_hot_lrs(region: str):
    for it in _query(f"serviceName eq 'Storage' and armRegionName eq '{region}' and priceType eq 'Consumption'"):
        if it.get("skuName") == "Hot LRS" and "Data Stored" in it.get("meterName", "") \
                and it.get("productName") == "General Block Blob v2":
            return it.get("retailPrice")
    return 0.0208


def meter_price(service, region, meter_eq=None, meter_contains=None,
                product_contains=None, product_excludes=None, price_type="Consumption"):
    """Generic resolver: first item matching filters -> {payg, sp1y, sp3y, meter, product}."""
    filt = f"serviceName eq '{service}' and armRegionName eq '{region}' and priceType eq '{price_type}'"
    for it in _query(filt):
        mn = it.get("meterName", ""); pn = it.get("productName", "")
        if meter_eq and mn != meter_eq:
            continue
        if meter_contains and meter_contains.lower() not in mn.lower():
            continue
        if product_contains and product_contains.lower() not in pn.lower():
            continue
        if product_excludes and any(x.lower() in pn.lower() for x in product_excludes):
            continue
        if "Spot" in mn or "Low Priority" in mn:
            continue
        sp = _sp(it)
        return {"payg": it.get("retailPrice"), "sp1y": sp.get("1 Year"),
                "sp3y": sp.get("3 Years"), "meter": mn, "product": pn}
    return None


def aks_cluster_fee(region):
    p = meter_price("Azure Kubernetes Service", region, meter_eq="Standard Uptime SLA")
    return p["payg"] if p else 0.10


def appservice_price(sku_meter, region):
    """Premium v3 plan; prefer Linux. sku_meter like 'P1 v3'."""
    p = meter_price("Azure App Service", region, meter_eq=f"{sku_meter} App",
                    product_contains="Premium v3 Plan - Linux")
    if not p:
        p = meter_price("Azure App Service", region, meter_eq=f"{sku_meter} App",
                        product_contains="Premium v3 Plan")
    return p


def sqldb_gp_reserved(region):
    """Azure SQL DB General Purpose Gen5: reserved $/vCore/yr (1yr,3yr) + storage $/GB/mo."""
    out = {"r1y": 867.0, "r3y": 1800.0, "compute_payg_hr": 0.1264, "storage_gb_mo": 0.115}
    for it in _query(f"serviceName eq 'SQL Database' and armRegionName eq '{region}' and priceType eq 'Reservation'"):
        pn = it.get("productName", "").lower(); mn = it.get("meterName", "")
        if "general purpose" in pn and "gen5" in pn and "serverless" not in pn and mn == "vCore":
            if it.get("reservationTerm") == "1 Year":
                out["r1y"] = it.get("retailPrice")
            elif it.get("reservationTerm") == "3 Years":
                out["r3y"] = it.get("retailPrice")
    return out


def redis_price(sku_meter, region, tier="Standard"):
    return meter_price("Redis Cache", region, meter_eq=f"{sku_meter} Cache",
                       product_contains=f"Azure Redis Cache {tier}")


HOURS_PER_MONTH = 730

# Fallback $/GiB-mo if the live meter is unavailable for a region.
_ANF_FALLBACK_GIB_MO = {"standard": 0.1475, "premium": 0.2942, "ultra": 0.3927}


def netapp_files_rate(region: str, tier: str = "Standard"):
    """Azure NetApp Files provisioned capacity $/GiB-month for the given tier
    (Standard | Premium | Ultra). Meters are published per GiB/Hour; convert to month."""
    tier = (tier or "Standard").strip().capitalize()
    for it in _query(f"serviceName eq 'Azure NetApp Files' and armRegionName eq '{region}' "
                     f"and priceType eq 'Consumption'"):
        mn = it.get("meterName", "")
        if mn == f"{tier} Capacity":
            rp = it.get("retailPrice")
            if rp:
                return round(rp * HOURS_PER_MONTH, 4)
    return _ANF_FALLBACK_GIB_MO.get(tier.lower(), 0.1475)


# Reserved-capacity ("bulk") blocks are sold as 100 TiB or 1 PiB commitments.
_ANF_BLOCK_GIB = {"100 TiB": 100 * 1024, "1 PiB": 1024 * 1024}


def netapp_files_reserved(region: str, tier: str = "Standard", block: str = "100 TiB"):
    """Azure NetApp Files reserved ('bulk') capacity effective $/GiB-month for 1yr & 3yr.
    Reservations are committed in fixed blocks (100 TiB or 1 PiB); the API publishes the
    total reservation price for the term, so we normalize to an effective $/GiB-month."""
    tier = (tier or "Standard").strip().capitalize()
    blk_gib = _ANF_BLOCK_GIB.get(block, _ANF_BLOCK_GIB["100 TiB"])
    out = {"r1y": None, "r3y": None, "block": block}
    for it in _query(f"serviceName eq 'Azure NetApp Files' and armRegionName eq '{region}' "
                     f"and priceType eq 'Reservation'"):
        if it.get("meterName", "") == f"{tier} - {block} Capacity":
            rp = it.get("retailPrice"); term = it.get("reservationTerm")
            if rp and term == "1 Year":
                out["r1y"] = round(rp / blk_gib / 12.0, 4)
            elif rp and term == "3 Years":
                out["r3y"] = round(rp / blk_gib / 36.0, 4)
    cons = netapp_files_rate(region, tier)
    if out["r1y"] is None:
        out["r1y"] = round(cons * 0.82, 4)  # ~18% typical reserved discount
    if out["r3y"] is None:
        out["r3y"] = out["r1y"]
    return out


def explore_prices(service: str, region: str, price_type: str = "Consumption",
                   contains: str = None, limit: int = 300):
    """Generic price lookup for ANY Azure service in ANY region. Returns a list of
    normalized rows: {sku, meter, product, unit, type, payg, sp1y, sp3y, reservationTerm}.
    Used by the Service Pricing Explorer. `price_type` is 'Consumption' or 'Reservation'."""
    filt = f"serviceName eq '{service}' and armRegionName eq '{region}'"
    if price_type:
        filt += f" and priceType eq '{price_type}'"
    rows = []
    for it in _query(filt, max_pages=20):
        mn = it.get("meterName", ""); pn = it.get("productName", "")
        if contains and contains.lower() not in (mn + " " + pn + " " + it.get("skuName", "")).lower():
            continue
        sp = _sp(it)
        rows.append({
            "sku": it.get("skuName", ""), "meter": mn, "product": pn,
            "unit": it.get("unitOfMeasure", ""), "type": it.get("priceType", ""),
            "payg": it.get("retailPrice"), "sp1y": sp.get("1 Year"),
            "sp3y": sp.get("3 Years"), "reservationTerm": it.get("reservationTerm", ""),
        })
        if len(rows) >= limit:
            break
    return rows
