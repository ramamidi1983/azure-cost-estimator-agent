"""Generate a formatted, client-ready Excel workbook from estimator output."""
import os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

BLK = Font(name="Arial", size=10, color="000000")
BLKB = Font(name="Arial", size=10, bold=True, color="000000")
GRN = Font(name="Arial", size=10, color="008000")
WHT = Font(name="Arial", size=11, bold=True, color="FFFFFF")
HDR = PatternFill("solid", fgColor="1F4E78")
SUB = PatternFill("solid", fgColor="D9E1F2")
CUR0 = '$#,##0;($#,##0);"-"'
CUR2 = '#,##0.0000;(#,##0.0000);"-"'
thin = Side(style="thin", color="BFBFBF")
BORD = Border(left=thin, right=thin, top=thin, bottom=thin)
CTR = Alignment(horizontal="center")


def _hdr(ws, row, labels, start=1):
    for i, l in enumerate(labels):
        c = ws.cell(row=row, column=start + i, value=l)
        c.font = WHT; c.fill = HDR; c.alignment = CTR; c.border = BORD


def build(lines, summ, params, out_path, modern=None):
    wb = Workbook()
    s = wb.active; s.title = "Summary"; s.sheet_view.showGridLines = False
    s["A1"] = "Azure Migration Cost Estimate"
    s["A1"].font = Font(name="Arial", size=14, bold=True, color="1F4E78")
    s["A2"] = (f"Region: {params['region']}  |  Term: {params['term_label']}  |  "
               f"AHB: {'Yes' if params['ahb'] else 'No'}  |  Resiliency: {'Yes' if params['resiliency'] else 'No'}")
    s["A2"].font = Font(name="Arial", size=9, italic=True, color="595959")
    s["A3"] = f"Live Azure Retail Prices  |  Generated {params['generated']}  |  Hosting only (excludes migration services)"
    s["A3"].font = Font(name="Arial", size=9, italic=True, color="C00000")
    _hdr(s, 5, ["Cost Area", "Monthly ($)", "Annual ($)"])
    r = 6
    for _, row in summ.iterrows():
        is_total = row["area"] == "TOTAL"
        c1 = s.cell(r, 1, row["area"]); c1.border = BORD
        c2 = s.cell(r, 2, round(row["monthly"], 2)); c2.number_format = CUR0; c2.border = BORD
        c3 = s.cell(r, 3, round(row["annual"], 2)); c3.number_format = CUR0; c3.border = BORD
        if is_total:
            for c in (c1, c2, c3): c.font = WHT; c.fill = HDR
        else:
            c1.font = BLK; c2.font = BLK; c3.font = BLK
        r += 1
    for col, w in zip("ABC", [40, 16, 16]): s.column_dimensions[col].width = w

    # Line items detail
    d = wb.create_sheet("Line_Items"); d.sheet_view.showGridLines = False
    cols = ["name", "environment", "role", "disposition", "target", "model", "sku", "rate_basis",
            "quantity", "hours", "storage_gb", "monthly"]
    present = [c for c in cols if c in lines.columns]
    _hdr(d, 1, [c.replace("_", " ").title() for c in present])
    for i, (_, row) in enumerate(lines.iterrows(), start=2):
        for j, c in enumerate(present, start=1):
            cell = d.cell(i, j, row.get(c))
            cell.border = BORD; cell.font = BLK
            if c == "monthly":
                cell.number_format = CUR0; cell.font = BLKB
            elif c == "rate_hr":
                cell.number_format = CUR2
    d.freeze_panes = "A2"
    for col, w in zip("ABCDEFGHIJKL", [22, 12, 14, 13, 11, 20, 20, 13, 9, 8, 11, 13]):
        d.column_dimensions[col].width = w

    # Modernization comparison
    if modern is not None and not modern.empty:
        mo = wb.create_sheet("Modernization"); mo.sheet_view.showGridLines = False
        mo["A1"] = "Modernization path comparison (per app workload, $/month)"
        mo["A1"].font = Font(name="Arial", size=12, bold=True, color="1F4E78")
        headers = list(modern.columns)
        _hdr(mo, 3, [h.replace("_", " ") for h in headers])
        for i, (_, row) in enumerate(modern.iterrows(), start=4):
            is_total = str(row[headers[0]]).upper() == "TOTAL"
            for j, c in enumerate(headers, start=1):
                cell = mo.cell(i, j, row[c]); cell.border = BORD
                if j == 1:
                    cell.font = BLKB if is_total else BLK
                else:
                    cell.number_format = CUR0
                    cell.font = BLKB if is_total else BLK
                    if is_total: cell.fill = SUB
        mo.column_dimensions["A"].width = 24
        for col in "BCDE": mo.column_dimensions[col].width = 24

    # Assumptions / rate meta
    m = wb.create_sheet("Rates_Meta"); m.sheet_view.showGridLines = False
    m["A1"] = "Live rate reference (Azure Retail Prices API)"
    m["A1"].font = BLKB
    rr = 3
    for k, v in params.get("meta", {}).items():
        m.cell(rr, 1, k).font = BLKB; rr += 1
        if isinstance(v, dict):
            for kk, vv in v.items():
                m.cell(rr, 1, "   " + kk).font = BLK
                m.cell(rr, 2, vv).font = BLK; rr += 1
        else:
            m.cell(rr, 1, v).font = BLK; rr += 1
        rr += 1
    m.column_dimensions["A"].width = 42; m.column_dimensions["B"].width = 18

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    wb.save(out_path)
    return out_path
