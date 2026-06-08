import requests, json

BASE = "http://127.0.0.1:8000"
path = r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_15_Days_Report_of_CS_HNK_HNS_2022_SCPO_ESP_TEST.csv"

# Clear session
requests.post(f"{BASE}/api/clear-session", json={})

# Upload
with open(path, "rb") as f:
    r = requests.post(f"{BASE}/api/process-batch",
                      files={"file": ("henkel.csv", f)}, timeout=120)
data = r.json()

kpis = data.get("kpis") or data.get("batch_kpis", {})
print(f"compliance: {kpis.get('compliance')}%")
print(f"batch_window_compliance: {kpis.get('batch_window_compliance')}%")
print(f"window_breach_days: {kpis.get('window_breach_days')}")
print(f"window_total_days: {kpis.get('window_total_days')}")
print(f"total_runs: {kpis.get('total_runs')}")
print(f"total_jobs: {kpis.get('total_jobs')}")

# Check window data
window = data.get("window", [])
print(f"\nWindow entries: {len(window)}")
for w in window:
    rd = w.get("run_date", "?")
    eh = w.get("elapsed_hrs", 0)
    breach = "BREACH" if eh > 6.0 else "ok"
    print(f"  {rd}: elapsed={eh:.2f}h total={w.get('total_hrs', 0):.2f}h  {breach}")
