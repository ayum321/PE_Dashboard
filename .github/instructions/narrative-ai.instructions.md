---
description: "Use when working on PE narrative generation, AI engine, Gemini integration, report writing, executive summary, findings narrative, consultant responses."
applyTo: "routers/pe_narrative.py, routers/pe_consultant.py, routers/ai.py, routers/executive.py, services/ai_engine.py, services/ai_narrator.py, services/ai_agent.py, services/nvidia_llm.py, services/gemini_vision.py"
---

# PE Narrative & AI Rules

## PE Narrative (routers/pe_narrative.py)
- POST `/api/pe-narrative`
- 3-layer protection: `_bare_fallback` → top-level catch → `_pe_narrative_inner`
- `annual_fee` safe format: `f"{float(_fee_raw):,.0f}"`

## 4-Section Format
1. **Data Volume** — SOW vs Actual (DFU, SKU), utilization %
2. **Batch SLA** — per-workflow max runtime vs SLA window, buffer %
3. **Infrastructure** — CPU/Memory/Disk per server role, peak vs avg
4. **UAT** — test case pass rates by category

## Writing Style
- Direct, factual — NO hedging or AI fluff
- Lead with numbers: "59,316 SKU" not "the SKU volume is approximately..."
- Parenthetical specifics: "(19/19)", "(~4.03 hours)", "(33% buffer)"
- Status markers: "✓ COMPLIANT", "APPROVED"

## AI Engine
- Primary: Google Gemini (`google-genai` SDK)
- Fallback: legacy `google-generativeai` SDK
- Vision: PyMuPDF for image extraction from PDF/DOCX
