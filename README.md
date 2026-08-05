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
6. **Chat** with a built-in **AI assistant** (Azure OpenAI) to tweak assumptions and pricing in plain English, and **customize prices** manually — see below.

## AI assistant & custom pricing (dashboard)
The dashboard has two interactive tabs on top of the estimate:

- **AI assistant** — type requests in plain English and the estimate updates live. Examples:
  *"switch to a 3-year term and turn on hybrid benefit"*, *"apply a 15% partner discount"*,
  *"add a 10% contingency buffer"*, *"set the web-frontend rows to 5 instances"*,
  *"bump every SQL line by 20%"*, *"price prod-sql01 at $1,200/month"*.
  The model returns structured changes (parameter toggles, per-row edits, and pricing multipliers/absolute prices) that are validated and applied. If no AI endpoint is configured it falls back to a deterministic rule-based parser so the chat still works offline.
- **Custom pricing** — a global adjustment slider (discount/uplift) plus an editable per-line monthly table to pin absolute prices. Active overrides are shown in the sidebar and can be reset.

**Wiring up Azure OpenAI** (via environment variables — the app uses **Entra ID / managed-identity auth** by default; no API key needed when the account has `disableLocalAuth=true`):
```
AZURE_OPENAI_ENDPOINT=https://<account>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-chat          # your chat model deployment name
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_CLIENT_ID=<user-assigned managed identity clientId>   # in Azure; omit locally (uses az login)
```
The caller (managed identity in Azure, or your `az login` user locally) needs the **"Cognitive Services OpenAI User"** role on the Azure OpenAI account. If the account allows key auth, set `AZURE_OPENAI_API_KEY` instead.

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
| `estimator.py` | Inventory → disposition-driven target → costed line items + summary + modernization compare; plus `apply_overrides`/`apply_row_edits`/`build_summary` for AI/manual customization |
| `assistant.py` | AI assistant: turns natural-language requests into validated structured changes (Azure OpenAI via Entra ID, with rule-based fallback) |
| `workbook.py` | Formatted Excel generator (Summary / Line_Items / Modernization / Rates_Meta) |
| `cli.py` | Command-line entry point |
| `app.py` | Streamlit dashboard: inventory editor, IaaS/PaaS/SaaS pricing instructions, **AI assistant** + **Custom pricing** tabs |
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
A production `Dockerfile` is included (Python 3.12-slim, Streamlit on port 8501, health check).

## Deploy & host on Azure (end-to-end automation)

Everything is scripted — infrastructure as code + one command + auto-deploy on every push.

**What gets created:** Resource Group → Azure Container Registry → Log Analytics → Container Apps Environment → Container App (scale-to-zero) → a GitHub **OIDC** app registration + federated credential + role assignments (no secrets stored).

**Prereqs:** `az login` and `gh auth login` (already authenticated).

```powershell
# One command provisions Azure AND wires up CI/CD:
./deploy/provision.ps1 `
  -ResourceGroup rg-cost-estimator `
  -Location eastus `
  -GitHubRepo ramamidi1983/azure-cost-estimator-agent
```

The script prints the live dashboard URL, e.g. `https://cost-estimator.<region>.azurecontainerapps.io`.

**Continuous deployment:** after provisioning, every push to `main` triggers
`.github/workflows/deploy.yml`, which logs in with OIDC, runs `az acr build`, and
updates the Container App to the new image. Config is passed via GitHub Actions
**repo variables** (set automatically by the provision script):
`AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, ACR_NAME, CONTAINERAPP_NAME`.

**Files**
| File | Purpose |
|---|---|
| `Dockerfile` / `.dockerignore` | Container image for the dashboard |
| `infra/main.bicep` | Log Analytics + ACA env + managed identity (AcrPull) + Container App |
| `deploy/provision.ps1` | One-shot: RG + ACR + image build + Bicep deploy + GitHub OIDC/CI wiring |
| `deploy/enable-entra-auth.ps1` | Turn on Microsoft Entra ID (Easy Auth) sign-in on the public app |
| `deploy/make-private.ps1` | Optional: recreate the env internal-only (VNet) + Bastion + jump VM |
| `deploy/cleanup-private.ps1` | Tear down the private-networking resources to stop their cost |
| `.github/workflows/deploy.yml` | Build → push → deploy on every `main` push (OIDC, no secrets) |

**Quick manual alternative** (no CI/CD):
```powershell
az containerapp up --name cost-estimator --resource-group rg-cost-estimator `
  --source . --ingress external --target-port 8501 --env-vars STREAMLIT_SERVER_HEADLESS=true
```

> Add **Entra ID (Easy Auth)** on the Container App to require sign-in. The Retail Prices API is public (no key), so no app secrets are required.

## Public hosting with Entra ID sign-in (recommended)

The simplest secure setup — and the one currently deployed — is a **public** Container Apps
environment with **Microsoft Entra ID (Easy Auth)** in front of it. It's reachable from any
browser on the public internet, but every visitor must sign in with a Microsoft account in
your tenant.

```powershell
# 1) Public env + app (external ingress) already created by provision.ps1, then:
# 2) Turn on Entra sign-in (creates the app registration + wires Easy Auth):
./deploy/enable-entra-auth.ps1 -ResourceGroup rg-cost-estimator -AppName cost-estimator-pub
```

- Unauthenticated browser requests are **redirected to the Microsoft sign-in page**.
- Sign-in is restricted to your tenant (`AzureADMyOrg`).
- No app secrets in code — the client secret lives in the Container App's secret store.

**Optional IP allowlist** (layer on top of, or instead of, Entra auth):
```powershell
az containerapp ingress access-restriction set -g rg-cost-estimator -n cost-estimator-pub `
  --rule-name allow-my-ip --ip-address <your.public.ip>/32 --action Allow
# One Allow rule => everything else is implicitly denied. Remove it to reopen:
az containerapp ingress access-restriction remove -g rg-cost-estimator -n cost-estimator-pub --rule-name allow-my-ip
```

### Stop / start to save cost (scale-to-zero)
```powershell
# Stop: no replicas run when idle (~$0 compute); auto-starts on next request
az containerapp update -g rg-cost-estimator -n cost-estimator-pub --min-replicas 0 --max-replicas 3
# Warm/always-on: keep one replica ready (no cold start)
az containerapp update -g rg-cost-estimator -n cost-estimator-pub --min-replicas 1
```

## Private (internal) hosting — access via Azure Bastion

> **Heads-up:** Internal (VNet-only) Container Apps ingress can hit an Azure **platform bug**
> where the internal load balancer returns `404 "Azure Container App - Unavailable"` for
> healthy apps (envoy has no route), even with correct DNS, ports, and no NSG/UDR. If you hit
> this, prefer **public hosting with Entra ID sign-in** (above) — optionally with an IP
> allowlist — which is reliable. Use `deploy/cleanup-private.ps1` to tear down the private
> resources below.

For sensitive workloads you can host the dashboard **privately** (no public internet exposure).
A Container Apps environment's internal/external mode is **immutable**, so this recreates the
environment inside a VNet with an internal load balancer.

```powershell
# After provision.ps1 has built the ACR image:
./deploy/make-private.ps1 -ResourceGroup rg-cost-estimator -Location eastus -AcrName <yourAcr>
```

**What it builds**
- VNet `vnet-cost-estimator` with `snet-aca` (delegated), `AzureBastionSubnet`, `snet-jump`
- Container App re-deployed with **internal ingress** (private VNet IP only) via `infra/main.bicep -internal`
- **Private DNS zone** for the environment domain (wildcard A records → the environment static IP)
- **Azure Bastion (Basic)** + a **Windows jump VM** (no public IP)

**How to access the private dashboard**
1. Azure Portal → the jump VM `vm-jump` → **Connect → Bastion**.
2. Sign in with the admin user/password printed by the script.
3. In the VM, open **Edge** and browse to the app FQDN
   (`https://cost-estimator.internal.<env-domain>.azurecontainerapps.io`).

The FQDN resolves **only inside the VNet** (via the private DNS zone), so it is not reachable
from the public internet. `-internal` support is built into `infra/main.bicep`
(`internal` + `infrastructureSubnetId` params).

> **Cost note:** Azure Bastion Basic ≈ $140/mo and the jump VM add ongoing cost. To remove
> all of it, run `./deploy/cleanup-private.ps1 -ResourceGroup rg-cost-estimator` (deletes the
> internal env/app, Bastion, jump VM, VNet, and private DNS zone; keeps ACR + the public app).

## Roadmap / making it more "agentic"
- **LLM-assisted sizing** (optional): add an Azure OpenAI step that reads a messy inventory or an RFP PDF and proposes `target`/`vcpu`/`memory_gb` before pricing. Keep it human-in-the-loop.
- **Multi-cloud**: add AWS/GCP price adapters for side-by-side comparison.
- **3-yr TCO tab** and **rehost-vs-refactor** compute comparison.
- **Scheduled refresh** of the price cache.
