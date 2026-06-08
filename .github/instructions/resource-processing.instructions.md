---
description: "Use when working on resource parsing, DOCX/PDF document processing, server normalization, resource health analysis, fleet grading."
applyTo: "services/resource_parser.py, services/resource_parser_generic.py, services/resource_calculator.py, routers/resource.py"
---

# Resource Processing Rules

## Pipeline
```
Upload DOCX/PDF → resource_parser.py → resource_calculator.py → session_cache["resource_summary"]
```

## Server Normalization
`normalize_server()` produces canonical fields:
- `cpu_pct` (canonical), `cpu_used` (alias)
- `mem_pct` (canonical), `mem_used` (alias)
- `disk_pct` (canonical), `disk_used_max` (alias)

## Resource Findings
- Fleet grade: critical/warn/ok per server
- Role-specific CPU thresholds (APP/DB/SRE)
- Dual pressure: CPU + memory simultaneously high
- CPU saturation detection
- Memory pressure events
- Data quality warnings for missing/inconsistent metrics
