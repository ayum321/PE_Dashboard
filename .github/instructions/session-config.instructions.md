---
description: "Use when working on session cache, config store, data persistence, session clearing, engagement data lifecycle, SOW baseline management, cache invalidation."
applyTo: "services/session_cache.py, services/config_store.py, services/pe_config.py, routers/config.py"
---

# Session & Config Rules

## Session Cache (`services/session_cache.py`)
- `ac_set(key, val)` / `ac_get(key)` / `ac_snapshot()` — in-memory audit context
- `_PERSIST_AC_SLOTS` = batch/SLA/resource KPIs → persisted in `.pe_cache.json`
- `sow_contract`, `volume_vs_sow`, `customer_name` → NOT persisted (engagement-specific)
- Key: `"resolved_workflow_df"` — canonical per-workflow list of dicts

## Config Store (`services/config_store.py`)
- Persisted JSON (`.pe_config.json`)
- `get(key, default)` / `set(key, val)` / `get_all()`

## pe_config (`services/pe_config.py`)
- Single canonical source for ALL thresholds
- `reload()` re-reads from config_store — call after Settings save
- `_safe_float()` helper prevents crash on dict values

## Lifespan (main.py)
- Server restart wipes ALL session data + SOW engagement keys
- SOW keys: `sow_baseline`, `sow_dfu`, `sow_sku`, `_sow_sla_windows`, etc.

## Clear Session Flow
1. `session_cache.clear()` — wipes `.pe_cache.json` + in-memory
2. Reset SLA values to `pe_config.SLA_DEFAULTS`
3. Call `pe_config.reload()`
4. Wipe `customer_name` from config_store
