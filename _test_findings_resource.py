from fastapi.testclient import TestClient
from main import app
import time

client = TestClient(app)

payload = {
    "resource_kpis": {
        "total_servers": 5, "known_servers": 5,
        "fleet_grade": "D", "fleet_score": 57.8,
        "avg_cpu": 73.6, "avg_mem": 50.4, "avg_disk": 0.0,
        "n_critical": 2, "n_warning": 3, "n_healthy": 0,
        "n_dual_pressure": 1
    },
    "servers": [
        {"host": "SRE",      "type": "SRE", "cpu_pct": 82.6, "effective_cpu": 82.6, "mem_pct": 87.0, "status": "Critical", "dual_pressure": True},
        {"host": "Utility",  "type": "APP", "cpu_pct": 80.8, "effective_cpu": 80.8, "mem_pct": 36.0, "status": "Warning",  "dual_pressure": False},
        {"host": "App_1",    "type": "APP", "cpu_pct": 72.0, "effective_cpu": 72.0, "mem_pct": 18.0, "status": "Warning",  "dual_pressure": False},
        {"host": "App_2",    "type": "APP", "cpu_pct": 71.3, "effective_cpu": 71.3, "mem_pct": 19.0, "status": "Warning",  "dual_pressure": False},
        {"host": "Database", "type": "DB",  "cpu_pct": 61.5, "effective_cpu": 61.5, "mem_pct": 92.0, "status": "Critical", "dual_pressure": False},
    ]
}

t = time.time()
r = client.post("/api/generate-findings", json=payload)
elapsed = time.time() - t
data = r.json()
print(f"HTTP {r.status_code} in {elapsed:.2f}s")
print(f"Findings: {len(data.get('findings', []))}")
for f in data.get("findings", []):
    print(f"  [{f['level']:8}] {f['text']}")
