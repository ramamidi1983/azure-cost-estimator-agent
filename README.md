# Azure Migration Cost Estimator (Agent + Dashboard)

Turns a workload **inventory** (CSV/XLSX) into a **client-ready Azure cost estimate**, priced with **live Azure Retail Prices** (PAYG, 1-yr/3-yr Savings Plan & Reserved, Azure Hybrid Benefit, resiliency add-in). Outputs a formatted Excel workbook and a web dashboard.

Built from the pricing + workbook pipeline proven on the RFP-Carnival and Goodyear CTSC estimates.

## What it does
1. **Ingest** an inventory of servers/workloads.
2. **Map** each row to an Azure target driven by its **migration disposition** (the "7 R's") + workload `role`:
   - **Rehost** → IaaS VM · **Replatform** → App Service / Azure SQL DB / PostgreSQL·MySQL Flex / Redis · **Refactor** → AKS · **Rearchitect/Modernize** → Container Apps / SQL Hyperscale / Cosmos DB · **Repurchase** → SaaS (per-user license) · **Retire/Retain** → skipped.
   - **If no disposition and no target are given, the row defaults to standard IaaS (a VM).**
3. **Price** via the Azure Retail Prices API (cached 24h) for the chosen region/term.
4. **Apply** toggles: term (PAYG / 1yr / 3yr), Azure Hybrid Benefit, resiliency add-in, prod vs non-prod.
5. **Produce** a Summary + Line_Items + **Modernization** + Rates_Meta Excel workbook, plus interactive charts and a **modernization comparison** (Rehost vs Replatform vs Containerize vs Modernize) in the dashboard.

## Pricing by service model
- **IaaS** (Rehost / default): size from `vcpu`/`memory_gb` → nearest VM SKU (or `azure_sku` override); live VM rate for the term; optional Azure Hybrid Benefit.
- **PaaS** (Replatform / Modernize): App Service, Azure SQL DB (GP reserved), SQL Hyperscale, PostgreSQL/MySQL Flexible Server, Cache for Redis, Cosmos DB — priced from each service's meters.
- **SaaS** (Repurchase): `quantity` (users) × `unit_price` ($/user/mo); no compute meter.
- **Combination**: mix all of the above in one inventory; results group by model on the Summary sheet.

## Inventory columns
`name, environment(Prod/NonProd), role, disposition(Rehost|Replatform|Refactor|Modernize|Repurchase|Retire...), target(optional: vm|aca|aks|appservice|hyperscale|sqldb|postgres|mysql|redis|cosmos|saas), vcpu, memory_gb, os(linux|windows), storage_gb, quantity, hours, azure_sku(optional), unit_price(SaaS only)`

Leave `disposition` **and** `target` blank → priced as standard IaaS (VM). See `samples/sample_inventory.csv`.

## Run locally
```powershell
pip install -r requirements.txt

# CLI
python cli.py samples\sample_inventory.csv --region eastus --term 1y --ahb --resiliency

# Dashboard
streamlit run app.py
```

## Files
| File | Purpose |
|---|---|
| `pricing.py` | Azure Retail Prices API client (VM, Hyperscale, Container Apps, AKS, App Service, SQL DB, Redis, Blob) + disk cache |
| `estimator.py` | Inventory → disposition-driven target → costed line items + summary + modernization compare |
| `workbook.py` | Formatted Excel generator (Summary / Line_Items / Modernization / Rates_Meta) |
| `cli.py` | Command-line entry point |
| `app.py` | Streamlit dashboard (with in-app IaaS/PaaS/SaaS pricing instructions) |
| `config/skus.yaml` | Disposition map, VM/App Service/Redis catalogs, PaaS DB rates + add-on rates |

## Where to host it

Pick based on how "always-on" and how shared it needs to be:

| Option | Best for | Notes |
|---|---|---|
| **Azure Container Apps** (recommended) | Shared internal tool, scale-to-zero | Containerize (`streamlit run`), 1 replica; scales to zero when idle → cheapest for bursty use. Add Entra ID (Easy Auth). |
| **Azure App Service (Linux, B1/B2)** | Simple always-on web app | Easiest CI/CD from GitHub; built-in auth; ~$13–55/mo. |
| **Azure Static Web Apps + Functions** | If you split UI/api | More work; overkill for Streamlit. |
| **Local / internal VM** | Just you | `streamlit run app.py`; zero cloud cost. |

**Recommendation:** **Azure Container Apps** with **Entra ID auth** and a **user-assigned managed identity**. The Retail Prices API is public (no key), so the only secrets you need are for optional Azure OpenAI (see below). Put the SKU-mapping `config/` in the image and mount `output/` to a storage share if you want to keep generated workbooks.

### Containerize (for App Service or Container Apps)
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit","run","app.py","--server.port=8501","--server.address=0.0.0.0"]
```
```bash
az containerapp up --name cost-estimator --resource-group rg-tools \
  --source . --ingress external --target-port 8501 --env-vars STREAMLIT_SERVER_HEADLESS=true
```

## Roadmap / making it more "agentic"
- **LLM-assisted sizing** (optional): add an Azure OpenAI step that reads a messy inventory or an RFP PDF and proposes `target`/`vcpu`/`memory_gb` before pricing. Keep it human-in-the-loop.
- **Multi-cloud**: add AWS/GCP price adapters for side-by-side comparison.
- **3-yr TCO tab** and **rehost-vs-refactor** compute comparison.
- **Scheduled refresh** of the price cache.
