"""
Boot-time config seeder — called from start.bat on every launch.

Writes default values into .pe_config.json only if they are missing.
Safe to run multiple times (idempotent).
"""
from __future__ import annotations
import sys
import os

# Make sure the project root is on sys.path
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from services.config_store import (
        get, set as cfg_set,
        get_gemini_key, set_gemini_key,
        get_nvidia_key, set_nvidia_key,
    )
except ImportError as exc:
    print(f" [CONFIG] WARNING: could not import config_store: {exc}")
    sys.exit(0)  # non-fatal — server will still start

# ── Gemini API key ──────────────────────────────────────────────
# Pass key as first CLI arg: python _seed_config.py <gemini_key> [<nvidia_key>]
key_arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""

if key_arg:
    if not get_gemini_key():
        set_gemini_key(key_arg)
        print(" [CONFIG] Gemini API key stored.")
    else:
        print(" [CONFIG] Gemini key already set.")
else:
    existing = get_gemini_key() or ""
    if existing:
        print(" [CONFIG] Gemini key present.")
    else:
        print(" [CONFIG] No Gemini key provided — set it in the Settings tab.")

# ── NVIDIA NIM API key ──────────────────────────────────────────
nv_arg = sys.argv[2].strip() if len(sys.argv) > 2 else os.environ.get("NVIDIA_API_KEY", "").strip()
if nv_arg:
    if not get_nvidia_key():
        set_nvidia_key(nv_arg)
        print(" [CONFIG] NVIDIA NIM key stored.")
    else:
        print(" [CONFIG] NVIDIA key already set.")
else:
    if get_nvidia_key():
        print(" [CONFIG] NVIDIA key present.")
    else:
        print(" [CONFIG] No NVIDIA key provided — set it in the Settings tab to enable LLM resource parsing.")

# ── SLA / benchmark defaults ────────────────────────────────────
defaults = {
    "daily_sla_hrs":       6.0,
    "weekly_sla_hrs":      8.0,
    "monthly_sla_hrs":     8.0,
    "custom_sla_hrs":      6.0,
    "sla_mode":            "daily",
    "benchmark_threshold": 10.0,
}

for key, value in defaults.items():
    if get(key) is None:
        cfg_set(key, value)

print(" [CONFIG] SLA defaults: daily=6h | weekly=8h | monthly=8h | bench-thresh=10%")
print(" [CONFIG] Config ready: .pe_config.json")
