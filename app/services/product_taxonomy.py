"""
product_taxonomy — canonical PE product/module list for the "Products Reviewed"
selector on the SOW Contract Intelligence tab.

This is generic across ALL customers — it is the full catalogue of Blue Yonder
supply-chain planning modules that a PE audit can cover, NOT a customer-specific
value. The PE reviewer manually selects which of these modules were in scope for
THIS engagement (a given customer may run any subset). Nothing here is
hardcoded per-customer; the selection itself is stored in config_store /
session audit context, same as any other engagement-specific input (e.g. SOW
baseline numbers).

Single source of truth: routers/sow.py exposes this via
GET /api/sow/product-taxonomy so the frontend never hardcodes the list twice.
"""
from __future__ import annotations

# Each entry: (canonical value, display label, group)
# Groups mirror how these modules are actually sold/implemented together:
#   DEMAND      — forecasting / demand-side planning
#   ESP         — supply/production planning & sequencing
#   FULFILLMENT — allocation, order promising, replenishment, fulfillment
#   PLATFORM    — cross-cutting platform/foundation offerings
_TAXONOMY: list[tuple[str, str, str]] = [
    # ── Demand family ─────────────────────────────────────────────
    ("DEMAND",                      "Demand",                                   "DEMAND"),
    ("DEMAND_360",                  "Demand 360",                               "DEMAND"),
    ("DEMAND_CLASSIFICATION",       "Demand Classification",                    "DEMAND"),
    ("FLOWCASTING",                 "Flowcasting",                              "DEMAND"),
    ("CONSENSUS_DEMAND_PLANNING",   "Consensus Demand Planning",                "DEMAND"),
    ("STATISTICAL_FORECAST",        "Statistical Forecast Roll-out",            "DEMAND"),

    # ── ESP (Enterprise Supply Planning) family ──────────────────
    ("ESP",                         "Enterprise Supply Planning (ESP)",         "ESP"),
    ("ESP_MANUFACTURING_SEQ",       "ESP – Manufacturing & Sequencing",         "ESP"),
    ("ESP_DEPLOYMENT",              "ESP – Deployment",                        "ESP"),
    ("ESP_LPOPT",                   "ESP – LP Optimization",                   "ESP"),
    ("MANUFACTURING_PLANNING",      "Manufacturing Planning",                  "ESP"),
    ("SEQUENCING",                  "Sequencing",                              "ESP"),
    ("SNOP",                        "S&OP (Sales & Operations Planning)",      "ESP"),

    # ── Fulfillment / Order Optimization family ──────────────────
    ("FULFILLMENT",                 "Fulfillment",                             "FULFILLMENT"),
    ("ORDER_OPTIMIZATION",          "Order Optimization",                     "FULFILLMENT"),
    ("ORDER_PROMISER",              "Order Promiser",                         "FULFILLMENT"),
    ("DYNAMIC_ALLOCATION",          "Dynamic Allocation",                     "FULFILLMENT"),
    ("REPLENISHMENT_INTERVAL_OPT",  "Replenishment Interval Optimization",    "FULFILLMENT"),
    ("FORWARD_BUY",                 "Forward Buy",                            "FULFILLMENT"),
    ("INVENTORY_OPTIMIZATION",      "Inventory Optimization",                 "FULFILLMENT"),

    # ── Platform / Foundation ─────────────────────────────────────
    ("SUPPLY_CHAIN_PLANNING_FOUNDATION", "Supply Chain Planning Foundation",  "PLATFORM"),
    ("PLATFORM",                    "Platform",                                "PLATFORM"),
    ("ENTERPRISE_KNOWLEDGE_BASE",   "Enterprise Knowledge Base",               "PLATFORM"),
    ("ENTERPRISE_PLANNING_SERVER",  "Enterprise Planning Server Edition",      "PLATFORM"),
]

GROUP_LABELS = {
    "DEMAND":      "Demand",
    "ESP":         "Enterprise Supply Planning (ESP)",
    "FULFILLMENT": "Fulfillment / Order Optimization",
    "PLATFORM":    "Platform / Foundation",
}

# Fast lookup: canonical value -> display label (used to render badges anywhere
# a raw value list is stored, e.g. audit context / narrative context).
VALUE_TO_LABEL = {v: label for v, label, _ in _TAXONOMY}


def taxonomy_payload() -> dict:
    """Return the taxonomy grouped for the frontend multi-select dropdown."""
    groups: dict[str, list[dict]] = {}
    for value, label, group in _TAXONOMY:
        groups.setdefault(group, []).append({"value": value, "label": label})
    return {
        "groups": [
            {"key": g, "label": GROUP_LABELS.get(g, g), "items": items}
            for g, items in groups.items()
        ],
    }


def labels_for(values: list[str]) -> list[str]:
    """Map stored canonical values back to display labels; passthrough for any
    custom/free-text value a PE reviewer added that isn't in the catalogue
    (customer engagements occasionally run modules not yet in this list)."""
    out = []
    for v in values or []:
        out.append(VALUE_TO_LABEL.get(v, v))
    return out
