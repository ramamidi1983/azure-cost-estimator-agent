"""Generate a formatted, client-ready Excel workbook from estimator output.

Sheets produced:
  * Summary            - cost-area roll-up + total (executive view)
  * Host_Detail        - one row per server/host with full sizing + pricing (RFP/Carnival style)
  * <area> sheets      - Goodyear-style per-area component sheets with subtotals
  * Modernization      - path comparison (when provided)
  * Assumptions        - global assumptions + live-rate reference
"""
import os
import re
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

BLK = Font(name="Arial", size=10, color="000000")
BLKB = Font(name="Arial", size=10, bold=True, color="000000")
GRN = Font(name="Arial", size=10, color="008000")
WHT = Font(name="Arial", size=11, bold=True, color="FFFFFF")
TITLE = Font(name="Arial", size=14, bold=True, color="1F4E78")
SUBTITLE = Font(name="Arial", size=9, italic=True, color="595959")
NOTE = Font(name="Arial", size=9, italic=True, color="C00000")
SECTION = Font(name="Arial", size=12, bold=True, color="1F4E78")
HDR = PatternFill("solid", fgColor="1F4E78")
SUB = PatternFill("solid", fgColor="D9E1F2")
CUR0 = '$#,##0;($#,##0);"-"'
CUR2 = '#,##0.0000;(#,##0.0000);"-"'
INT0 = '#,##0;(#,##0);"-"'
thin = Side(style="thin", color="BFBFBF")
BORD = Border(left=thin, right=thin, top=thin, bottom=thin)
CTR = Alignment(horizontal="center")
LEFT = Alignment(horizontal="left")


def _hdr(ws, row, labels, start=1):
    for i, l in enumerate(labels):
        c = ws.cell(row=row, column=start + i, value=l)
        c.font = WHT
        c.fill = HDR
        c.alignment = CTR
        c.border = BORD


def _widths(ws, widths, start=1):
    for i, w in enumerate(widths):
        ws.column_dimensions[get_column_letter(start + i)].width = w


def _area(row):
    """Same grouping used by estimator.build_summary."""
    env = str(row.get("environment", "")).lower()
    if not env.startswith("prod"):
        return "Non-Production"
    return "Prod - " + str(row.get("model", ""))


def _safe_sheet_name(name, used):
    clean = re.sub(r"[\[\]:*?/\\]", " ", str(name)).strip()
    clean = re.sub(r"\s+", " ", clean)[:31] or "Sheet"
    base = clean
    n = 2
    while clean.lower() in used:
        suffix = f" {n}"
        clean = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(clean.lower())
    return clean


def build(lines, summ, params, out_path, modern=None):
    wb = Workbook()
    used_names = set()

    # ---------------------------------------------------------------- Summary
    s = wb.active
    s.title = "Summary"
    used_names.add("summary")
    s.sheet_view.showGridLines = False
    s["A1"] = "Azure Migration Cost Estimate"
    s["A1"].font = TITLE
    s["A2"] = (f"Region: {params['region']}  |  Term: {params['term_label']}  |  "
               f"AHB: {'Yes' if params['ahb'] else 'No'}  |  Resiliency: {'Yes' if params['resiliency'] else 'No'}")
    s["A2"].font = SUBTITLE
    s["A3"] = f"Live Azure Retail Prices  |  Generated {params['generated']}  |  Hosting only (excludes migration services)"
    s["A3"].font = NOTE
    s["A4"] = "Per-server pricing is on the 'Host_Detail' sheet; per-area breakdowns are on the area sheets."
    s["A4"].font = SUBTITLE
    _hdr(s, 6, ["Cost Area", "Monthly ($)", "Annual ($)"])
    r = 7
    for _, row in summ.iterrows():
        is_total = row["area"] == "TOTAL"
        c1 = s.cell(r, 1, row["area"])
        c1.border = BORD
        c2 = s.cell(r, 2, round(row["monthly"], 2))
        c2.number_format = CUR0
        c2.border = BORD
        c3 = s.cell(r, 3, round(row["annual"], 2))
        c3.number_format = CUR0
        c3.border = BORD
        if is_total:
            for c in (c1, c2, c3):
                c.font = WHT
                c.fill = HDR
        else:
            c1.font = BLK
            c2.font = BLK
            c3.font = BLK
        r += 1
    _widths(s, [40, 16, 16])

    # ---------------------------------------------------------------- Host_Detail (RFP style)
    d = wb.create_sheet("Host_Detail")
    used_names.add("host_detail")
    d.sheet_view.showGridLines = False
    d["A1"] = "Host Inventory - per-server Azure hosting cost"
    d["A1"].font = SECTION
    d["A2"] = (f"One row per server/workload  |  Region {params['region']}  |  {params['term_label']}  "
               f"|  Priced with live Azure Retail Prices, {params['generated']}")
    d["A2"].font = SUBTITLE
    detail_cols = [
        ("name", "Host Name"), ("role", "Application / Role"), ("environment", "Env"),
        ("os", "OS"), ("target", "Server Type"), ("disposition", "Disposition"),
        ("vcpu", "vCPU"), ("memory_gb", "Memory (GB)"), ("storage_gb", "Storage (GB)"),
        ("quantity", "Qty"), ("sku", "Azure SKU"), ("model", "Model"),
        ("rate_basis", "Rate Basis"), ("hours", "Hours/mo"),
        ("monthly", "Monthly ($)"), ("annual", "Annual ($)"),
    ]
    present = [(k, lbl) for k, lbl in detail_cols if k in lines.columns or k == "annual"]
    hrow = 4
    _hdr(d, hrow, [lbl for _, lbl in present])
    tot_monthly = 0.0
    for i, (_, row) in enumerate(lines.iterrows(), start=hrow + 1):
        monthly = float(row.get("monthly", 0) or 0)
        tot_monthly += monthly
        for j, (key, _) in enumerate(present, start=1):
            if key == "annual":
                val = round(monthly * 12, 2)
            elif key == "monthly":
                val = round(monthly, 2)
            else:
                val = row.get(key)
            cell = d.cell(i, j, val)
            cell.border = BORD
            cell.font = BLK
            if key in ("monthly", "annual"):
                cell.number_format = CUR0
                cell.font = BLKB if key == "monthly" else BLK
            elif key in ("vcpu", "memory_gb", "storage_gb", "quantity", "hours"):
                cell.number_format = INT0
                cell.alignment = CTR
    # totals row
    trow = hrow + 1 + len(lines)
    money_idx = {k: n for n, (k, _) in enumerate(present, start=1)}
    label_col = 1
    tc = d.cell(trow, label_col, "TOTAL")
    tc.font = WHT
    tc.fill = HDR
    for k in ("monthly", "annual"):
        if k in money_idx:
            col = money_idx[k]
            val = round(tot_monthly * (12 if k == "annual" else 1), 2)
            cc = d.cell(trow, col, val)
            cc.number_format = CUR0
            cc.font = WHT
            cc.fill = HDR
    for c in range(1, len(present) + 1):
        cell = d.cell(trow, c)
        cell.border = BORD
        if cell.value is None:
            cell.fill = HDR
    d.freeze_panes = d.cell(hrow + 1, 1).coordinate
    d.auto_filter.ref = f"A{hrow}:{get_column_letter(len(present))}{trow - 1}"
    _widths(d, [22, 22, 8, 10, 13, 16, 7, 12, 13, 6, 18, 22, 16, 9, 13, 13])

    # ---------------------------------------------------------------- per-area sheets (Goodyear style)
    areas = []
    for _, row in lines.iterrows():
        a = _area(row)
        if a not in areas:
            areas.append(a)
    # production areas first, then non-production
    areas.sort(key=lambda a: (a == "Non-Production", a))
    for area in areas:
        sub = lines[lines.apply(_area, axis=1) == area]
        if sub.empty:
            continue
        title = _safe_sheet_name(area, used_names)
        ws = wb.create_sheet(title)
        ws.sheet_view.showGridLines = False
        ws["A1"] = area
        ws["A1"].font = SECTION
        ws["A2"] = f"Component-level detail for this cost area  |  {params['term_label']}  |  {params['region']}"
        ws["A2"].font = SUBTITLE
        area_cols = ["Component", "Role", "Disposition", "SKU", "Qty", "vCPU",
                     "Memory (GB)", "Storage (GB)", "Hours/mo", "Monthly ($)", "Annual ($)"]
        keymap = ["name", "role", "disposition", "sku", "quantity", "vcpu",
                  "memory_gb", "storage_gb", "hours", "monthly", "annual"]
        _hdr(ws, 4, area_cols)
        rr = 5
        sub_total = 0.0
        for _, row in sub.iterrows():
            monthly = float(row.get("monthly", 0) or 0)
            sub_total += monthly
            for j, key in enumerate(keymap, start=1):
                if key == "annual":
                    val = round(monthly * 12, 2)
                elif key == "monthly":
                    val = round(monthly, 2)
                else:
                    val = row.get(key)
                cell = ws.cell(rr, j, val)
                cell.border = BORD
                cell.font = BLK
                if key in ("monthly", "annual"):
                    cell.number_format = CUR0
                    cell.font = BLKB if key == "monthly" else BLK
                elif key in ("quantity", "vcpu", "memory_gb", "storage_gb", "hours"):
                    cell.number_format = INT0
                    cell.alignment = CTR
            rr += 1
        # subtotal
        st1 = ws.cell(rr, 1, "Subtotal")
        st1.font = BLKB
        st1.fill = SUB
        for j in range(2, len(area_cols) + 1):
            ws.cell(rr, j).fill = SUB
        m_col = keymap.index("monthly") + 1
        a_col = keymap.index("annual") + 1
        cm = ws.cell(rr, m_col, round(sub_total, 2))
        cm.number_format = CUR0
        cm.font = BLKB
        cm.fill = SUB
        ca = ws.cell(rr, a_col, round(sub_total * 12, 2))
        ca.number_format = CUR0
        ca.font = BLKB
        ca.fill = SUB
        _widths(ws, [26, 20, 15, 18, 6, 7, 12, 13, 9, 13, 13])

    # ---------------------------------------------------------------- Modernization
    if modern is not None and not modern.empty:
        mo = wb.create_sheet("Modernization")
        used_names.add("modernization")
        mo.sheet_view.showGridLines = False
        mo["A1"] = "Modernization path comparison (per app workload, $/month)"
        mo["A1"].font = SECTION
        headers = list(modern.columns)
        _hdr(mo, 3, [h.replace("_", " ") for h in headers])
        for i, (_, row) in enumerate(modern.iterrows(), start=4):
            is_total = str(row[headers[0]]).upper() == "TOTAL"
            for j, c in enumerate(headers, start=1):
                cell = mo.cell(i, j, row[c])
                cell.border = BORD
                if j == 1:
                    cell.font = BLKB if is_total else BLK
                else:
                    cell.number_format = CUR0
                    cell.font = BLKB if is_total else BLK
                    if is_total:
                        cell.fill = SUB
        mo.column_dimensions["A"].width = 24
        for col in "BCDE":
            mo.column_dimensions[col].width = 24

    # ---------------------------------------------------------------- Assumptions + rate meta
    m = wb.create_sheet("Assumptions")
    used_names.add("assumptions")
    m.sheet_view.showGridLines = False
    m["A1"] = "Azure Target Hosting Cost Model - Assumptions"
    m["A1"].font = TITLE
    m["A2"] = (f"Pricing: Azure Retail ({params['region']}) | {params['term_label']} | "
               f"Fetched via Azure Retail Prices API, {params['generated']}")
    m["A2"].font = SUBTITLE
    m["A3"] = "Steady-state HOSTING only - excludes one-time migration / professional services."
    m["A3"].font = NOTE
    _hdr(m, 5, ["Assumption", "Value", "Unit / Note"])
    rows = [
        ("Region", params["region"], "Azure region"),
        ("Pricing term", params["term_label"], "PAYG / 1-yr / 3-yr"),
        ("Azure Hybrid Benefit", "Yes" if params["ahb"] else "No", "Windows/SQL license reuse"),
        ("Resiliency add-in", "Yes" if params["resiliency"] else "No", "HA replica + ASR/DR"),
        ("Generated", params["generated"], "Report timestamp"),
        ("Pricing source", "Azure Retail Prices API", "Live list prices, 24h cache"),
    ]
    rr = 6
    for a, v, note in rows:
        c1 = m.cell(rr, 1, a)
        c1.font = BLKB
        c1.border = BORD
        c2 = m.cell(rr, 2, v)
        c2.font = BLK
        c2.border = BORD
        c3 = m.cell(rr, 3, note)
        c3.font = BLK
        c3.border = BORD
        rr += 1
    rr += 1
    m.cell(rr, 1, "LIVE RATE REFERENCE").font = SECTION
    rr += 1
    for k, v in (params.get("meta") or {}).items():
        m.cell(rr, 1, k).font = BLKB
        rr += 1
        if isinstance(v, dict):
            for kk, vv in v.items():
                m.cell(rr, 1, "   " + str(kk)).font = BLK
                m.cell(rr, 2, vv).font = BLK
                rr += 1
        else:
            m.cell(rr, 2, v).font = BLK
            rr += 1
        rr += 1
    _widths(m, [42, 24, 34])

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    wb.save(out_path)
    return out_path
