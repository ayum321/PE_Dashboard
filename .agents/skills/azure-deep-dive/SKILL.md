---
name: azure-deep-dive
description: Work on Azure Monitor collection, VM resource analysis, infrastructure baselines, hot hours, time-series charts, or the Azure deep-dive route and frontend. Use for services/azure_monitor.py, routers/azure_resource.py, and static/deep_dive.js.
---

# Azure deep dive

1. Read the Azure hard-won gotcha in `CLAUDE.md` before changing authentication or transport code.
2. Preserve the flow: Azure identity → Azure Monitor query → `azure_monitor.py` → `azure_resource.py` → `deep_dive.js`.
3. Keep canonical fields (`cpu_pct`, `mem_pct`, `disk_pct`) and only use documented backward-compatible aliases when necessary.
4. Treat the following as permanent safeguards: no `DefaultAzureCredential` fallback, no `TokenCachePersistenceOptions`, retain the platform-call workaround, and force IPv4 for Microsoft login DNS where the existing code does so.
5. Base conclusions on observed windows and measured trends: confidence tiers, recurring hot hours, acceleration, weekday/weekend divergence, chronic pressure, and fleet patterns. Upgrade evidence from inferred to measured only when Azure data corroborates it.
6. Run `npm run check:pe-config` for backend changes, `npm run check:js` for deep-dive UI changes, and make a safe real or mocked endpoint check without exposing tokens or tenant data.
