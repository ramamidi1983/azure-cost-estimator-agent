"""CLI: python cli.py samples/sample_inventory.csv --region eastus --term 1y --ahb --resiliency"""
import argparse, datetime as dt, os
import pandas as pd
import estimator as E
import workbook as W

TERM_LABEL = {"payg": "Pay-as-you-go", "1y": "1-Year Savings Plan/Reserved",
              "3y": "3-Year Savings Plan/Reserved"}


def load(path):
    if path.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(path)
    return pd.read_csv(path)


def run(path, region="eastus", term="1y", ahb=False, resiliency=False, out=None,
        container_opts=None):
    df = load(path)
    lines, summ = E.estimate(df, region=region, term=term, ahb=ahb,
                             resiliency=resiliency, container_opts=container_opts)
    modern = E.modernization(df, region=region, term=term, ahb=ahb,
                             container_opts=container_opts)
    params = {"region": region, "term": term, "term_label": TERM_LABEL.get(term, term),
              "ahb": ahb, "resiliency": resiliency,
              "container_options": E.container_options(container_opts),
              "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    if not out:
        base = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(os.path.dirname(__file__), "output", f"{base}_Azure_Estimate.xlsx")
    W.build(lines, summ, params, out, modern=modern)
    return lines, summ, out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Azure Migration Cost Estimator agent")
    ap.add_argument("inventory")
    ap.add_argument("--region", default="eastus")
    ap.add_argument("--term", default="1y", choices=["payg", "1y", "3y"])
    ap.add_argument("--ahb", action="store_true", help="Azure Hybrid Benefit")
    ap.add_argument("--resiliency", action="store_true")
    ap.add_argument("--pool-aks", action="store_true",
                    help="Share AKS clusters by Prod/NonProd and pool node capacity")
    ap.add_argument("--optimize-container-apps", action="store_true",
                    help="Use Container Apps Consumption scaling assumptions")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    opts = {"pool_aks": a.pool_aks, "optimize_aca": a.optimize_container_apps}
    lines, summ, out = run(a.inventory, a.region, a.term, a.ahb, a.resiliency, a.out, opts)
    print(summ.to_string(index=False))
    print("\nWorkbook:", out)
