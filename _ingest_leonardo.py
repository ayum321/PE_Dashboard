"""Ingest Leonardo PE data and validate findings accuracy."""
import requests
import json
import sys

BASE = "http://127.0.0.1:8765/api"

def main():
    # 1. Clear session
    r = requests.post(f"{BASE}/clear-session")
    print(f"Clear session: {r.status_code}")

    # 2. Upload Batch CSV
    print("\n=== BATCH UPLOAD ===")
    batch_path = r"C:\Users\1039081\Downloads\Work\Performance-Engagement\Leonardo_PE_Review\Leonardo\Last_15_Days_Report_of_CS_LHD_SCPO_2025_TEST.csv"
    with open(batch_path, "rb") as f:
        r = requests.post(f"{BASE}/process-batch", files={"file": ("batch.csv", f, "text/csv")})
    batch_data = r.json()
    print(f"Status: {r.status_code}")
    kpis = batch_data.get("kpis", {})
    print(f"  Total Jobs: {kpis.get('total_jobs')}")
    print(f"  Total Runs: {kpis.get('total_runs')}")
    print(f"  Window Compliance: {kpis.get('batch_window_compliance')}%")
    print(f"  Job SLA Compliance: {kpis.get('job_sla_compliance')}%")
    print(f"  Breach: {kpis.get('jobs_breach')} jobs")
    print(f"  At-Risk: {kpis.get('jobs_at_risk')} jobs")
    print(f"  OK: {kpis.get('jobs_ok')} jobs")
    fb = kpis.get("fleet_sla_buffer", {})
    print(f"  Fleet Buffer: {fb.get('buffer_pct')}% ({fb.get('status')})")
    print(f"  Worst Job: {kpis.get('worst_job_name')} peak={kpis.get('worst_job_peak')}h")

    dc = batch_data.get("data_coverage", {})
    print(f"\n  Confidence: {dc.get('confidence')}% ({dc.get('confidence_label')})")
    print(f"  Date Range: {dc.get('date_range')}")
    print(f"  Has End_Time: {dc.get('has_end_time')}")
    print(f"  Synthetic Timestamps: {dc.get('has_synthetic_timestamps', False)}")
    print(f"  Zero Runtime %: {dc.get('zero_runtime_pct', 0)}")
    for w in dc.get("warnings", []):
        print(f"  [!] [{w['severity'].upper()}] {w['code']}: {w['text'][:120]}")

    # SLA source
    sla_src = batch_data.get("sla_source", {})
    print(f"\n  SLA Source: {sla_src.get('type')} | {sla_src.get('note', '')[:100]}")

    # 3. Upload BatchSLA_info.xlsx
    print("\n=== BATCH SLA UPLOAD ===")
    sla_path = r"C:\Users\1039081\Downloads\Work\Performance-Engagement\Leonardo_PE_Review\Leonardo\Leonardov2025_BatchSLA_info.xlsx"
    with open(sla_path, "rb") as f:
        r = requests.post(f"{BASE}/batch-sla/upload", files={"file": ("sla.xlsx", f)})
    print(f"Status: {r.status_code}")
    if r.status_code == 200:
        sla = r.json()
        wfs = sla.get("workflows", [])
        print(f"  Workflows parsed: {len(wfs)}")
        for wf in wfs[:20]:
            print(f"    {wf.get('workflow','?')}: type={wf.get('batch_type','?')} "
                  f"SLA={wf.get('sla_hours')}h actual={wf.get('last_run_hours_xlsx')}h "
                  f"compliance={wf.get('compliance')} source={wf.get('sla_source')}")
        for w in sla.get("warnings", []):
            print(f"  [!] {w}")
    else:
        print(f"  Error: {r.text[:300]}")

    # 4. Upload Resource DOCX
    print("\n=== RESOURCE UPLOAD ===")
    res_path = r"C:\Users\1039081\Downloads\Work\Performance-Engagement\Leonardo_PE_Review\Leonardo\Leonardo_Utilization_Report.docx"
    with open(res_path, "rb") as f:
        r = requests.post(f"{BASE}/upload", files={"file": ("resource.docx", f)})
    print(f"Status: {r.status_code}")
    if r.status_code == 200:
        resource_data = r.json()
        servers = resource_data.get("servers", [])
        print(f"  Servers parsed: {len(servers)}")
        print(f"  Image only: {resource_data.get('image_only')}")
        print(f"  Vision attempted: {resource_data.get('vision_attempted')}")
        for s in servers[:10]:
            host = s.get("host", "?")
            cpu = s.get("cpu_pct", s.get("cpu_used", "?"))
            mem = s.get("mem_pct", s.get("mem_used", "?"))
            disk = s.get("disk_pct", s.get("disk_used_max", "?"))
            role = s.get("server_type", "?")
            print(f"    {host} [{role}] CPU={cpu}% MEM={mem}% DISK={disk}%")
    else:
        resource_data = {}
        print(f"  Error: {r.text[:300]}")

    # 5. Generate Findings - pass collected data from above
    print("\n=== FINDINGS ===")
    findings_body = {}
    # Pass batch data if we got it
    if batch_data:
        findings_body["batch_kpis"] = batch_data.get("kpis")
        findings_body["top_jobs"] = batch_data.get("top_jobs")
        findings_body["top_breaches"] = batch_data.get("top_breaches")
        findings_body["window"] = batch_data.get("window")
        findings_body["anomalies"] = batch_data.get("anomalies")
        findings_body["sub_stats"] = batch_data.get("sub_stats")
    # Pass resource data if we got it
    if resource_data:
        findings_body["servers"] = resource_data.get("servers")
    r = requests.post(f"{BASE}/generate-findings", json=findings_body)
    findings = r.json()
    print(f"Status: {r.status_code}")
    items = findings if isinstance(findings, list) else findings.get("findings", [])
    critical = [f for f in items if f.get("severity") == "critical"]
    warning = [f for f in items if f.get("severity") == "warning"]
    info = [f for f in items if f.get("severity") == "info"]
    print(f"  Total: {len(items)} (Critical: {len(critical)}, Warning: {len(warning)}, Info: {len(info)})")

    print("\n--- CRITICAL FINDINGS ---")
    for f in critical:
        print(f"  [{f.get('rule', '?')}] {f.get('title', '?')}")
        print(f"    {f.get('detail', f.get('description', ''))[:200]}")
        print(f"    evidence={f.get('evidence', '')[:150]}")
        print(f"    root_cause={f.get('root_cause', '')[:100]}")
        print()

    print("\n--- WARNING FINDINGS ---")
    for f in warning:
        print(f"  [{f.get('rule', '?')}] {f.get('title', '?')}")
        print(f"    {f.get('detail', f.get('description', ''))[:200]}")
        print()

    print("\n--- INFO FINDINGS ---")
    for f in info[:10]:
        print(f"  [{f.get('rule', '?')}] {f.get('title', '?')}")
        print()

    print("\n--- ALL FINDINGS (raw) ---")
    for f in items:
        print(f"  KEYS: {list(f.keys())}")
        print(f"  RAW: {json.dumps(f, default=str)[:500]}")
        print()

if __name__ == "__main__":
    main()
