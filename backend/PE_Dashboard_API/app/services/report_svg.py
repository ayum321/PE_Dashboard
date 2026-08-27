"""Small, dependency-free SVG renderers for the standalone audit report.

Each renderer consumes the frozen ``audit_report_payload`` contract.  It does
not calculate SLA, severity, grade, or anomaly events; it only draws values
and classifications that have already been resolved by dashboard engines.
"""
from __future__ import annotations

from html import escape
from typing import Any


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _svg(width: int, height: int, content: str, label: str) -> str:
    return (
        f'<svg class="report-chart" role="img" aria-label="{escape(label)}" '
        f'viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="100%" height="100%" rx="8" fill="#111827"/>{content}</svg>'
    )


def _status_color(status: Any) -> str:
    value = str(status or "").lower()
    if value in {"breach", "failed", "critical", "critical_sustained"}:
        return "#ff4444"
    if value in {"at_risk", "long_job", "tight", "warning"}:
        return "#ffaa00"
    return "#22c55e"


def batch_window_chart(rows: list[Any]) -> tuple[str | None, str | None]:
    records = [row for row in rows if isinstance(row, dict)]
    plotted: list[tuple[dict[str, Any], float]] = []
    for row in records:
        value = next((_float(row.get(key)) for key in ("effective_hrs", "window_hrs", "elapsed_hrs", "runtime_hrs") if _float(row.get(key)) is not None), None)
        if value is not None:
            plotted.append((row, value))
    if not plotted:
        return None, "Daily batch window chart was requested but contains zero plotted points."
    width, height, pad = 920, 310, 42
    max_value = max(value for _, value in plotted)
    ceiling_values = [_float(row.get("sla_hrs") or row.get("sla_ceiling") or row.get("sla")) for row, _ in plotted]
    ceiling_values = [value for value in ceiling_values if value is not None and value > 0]
    ceiling = max(ceiling_values, default=0.0)
    scale_max = max(max_value, ceiling, 1.0) * 1.12
    chart_h = height - pad * 2
    step = (width - pad * 2) / max(len(plotted), 1)
    items = [f'<text x="{pad}" y="22" fill="#e8eaed" font-size="14" font-weight="600">Daily batch window</text>']
    for i in range(5):
        y = pad + chart_h * i / 4
        val = scale_max * (1 - i / 4)
        items.append(f'<line x1="{pad}" x2="{width-pad}" y1="{y:.1f}" y2="{y:.1f}" stroke="#2a3040"/>')
        items.append(f'<text x="4" y="{y+4:.1f}" fill="#9aa0a6" font-size="10">{val:.1f}h</text>')
    if ceiling:
        y = pad + chart_h * (1 - ceiling / scale_max)
        items.append(f'<line x1="{pad}" x2="{width-pad}" y1="{y:.1f}" y2="{y:.1f}" stroke="#ff4444" stroke-dasharray="6 4"/>')
        items.append(f'<text x="{width-pad-2}" y="{y-4:.1f}" text-anchor="end" fill="#ff6b6b" font-size="10">SLA ceiling {ceiling:g}h</text>')
    for index, (row, value) in enumerate(plotted):
        bar_w = max(4.0, step * .64)
        x = pad + index * step + (step - bar_w) / 2
        y = pad + chart_h * (1 - value / scale_max)
        h = pad + chart_h - y
        colour = _status_color(row.get("status"))
        items.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="2" fill="{colour}"><title>{escape(str(row.get("run_date") or row.get("date") or row.get("day") or "date"))}: {value:.2f}h</title></rect>')
        if row.get("spike") or str(row.get("status") or "").lower() in {"breach", "failed"}:
            items.append(f'<text x="{x+bar_w/2:.1f}" y="{max(36, y-6):.1f}" text-anchor="middle" fill="#ffb020" font-size="13">▲</text>')
        if index % max(1, len(plotted)//8) == 0:
            label = str(row.get("run_date") or row.get("date") or row.get("day") or "")[-5:]
            items.append(f'<text x="{x+bar_w/2:.1f}" y="{height-12}" text-anchor="middle" fill="#9aa0a6" font-size="9">{escape(label)}</text>')
    return _svg(width, height, "".join(items), "Daily batch window with SLA breach and spike markers"), None


def long_pole_chart(matrix: dict[str, Any]) -> tuple[str | None, str | None]:
    rows = [row for row in _list(_dict(matrix).get("rows")) if isinstance(row, dict)]
    if not rows:
        return None, "Long-pole trend chart was requested but contains zero plotted points."
    width, height, pad = 920, 300, 44
    max_value = max((_float(row.get("max_min")) or 0 for row in rows), default=0)
    if max_value <= 0:
        return None, "Long-pole trend chart was requested but contains zero plotted points."
    chart_h = height - pad * 2
    step = chart_h / len(rows)
    items = ['<text x="44" y="22" fill="#e8eaed" font-size="14" font-weight="600">Long-pole jobs — peak runtime</text>']
    for index, row in enumerate(rows):
        avg = _float(row.get("avg_min")) or 0
        peak = _float(row.get("max_min")) or 0
        y = pad + index * step + 5
        label = escape(str(row.get("job") or "Unknown job"))
        label = label[:34] + ("…" if len(label) > 34 else "")
        items.append(f'<text x="6" y="{y+10:.1f}" fill="#e8eaed" font-size="10">{label}</text>')
        x0 = 270
        usable = width - x0 - 62
        avg_w, peak_w = usable * avg / max_value, usable * peak / max_value
        colour = "#ff4444" if bool(row.get("is_longpole")) else "#4a9eff"
        items.append(f'<rect x="{x0}" y="{y:.1f}" width="{peak_w:.1f}" height="12" rx="2" fill="#2a3040"/>')
        items.append(f'<rect x="{x0}" y="{y:.1f}" width="{avg_w:.1f}" height="12" rx="2" fill="{colour}"/>')
        items.append(f'<text x="{x0+peak_w+6:.1f}" y="{y+10:.1f}" fill="#e8eaed" font-size="10">avg {avg:.0f}m · max {peak:.0f}m</text>')
    return _svg(width, height, "".join(items), "Long-pole job runtime trend"), None


def fleet_heatmap_chart(heatmap: dict[str, Any]) -> tuple[str | None, str | None]:
    grids = _dict(heatmap).get("grids")
    rows = _list(_dict(grids).get("cpu") if isinstance(grids, dict) else _dict(heatmap).get("vms"))
    timestamps = _list(_dict(heatmap).get("timestamps"))
    data_rows = [row for row in rows if isinstance(row, dict)]
    if not data_rows or not timestamps:
        return None, "Fleet heatmap source data is absent; no chart was rendered."
    width, height, left, top = 920, max(180, 72 + len(data_rows) * 18), 190, 40
    cell_w = (width - left - 10) / max(len(timestamps), 1)
    items = ['<text x="10" y="22" fill="#e8eaed" font-size="14" font-weight="600">Fleet CPU heatmap</text>', '<text x="170" y="22" fill="#9aa0a6" font-size="10">higher utilisation = more pressure</text>']
    plotted = 0
    for row_index, row in enumerate(data_rows):
        y = top + row_index * 18
        name = escape(str(row.get("name") or row.get("host") or "Unknown"))
        items.append(f'<text x="8" y="{y+11}" fill="#e8eaed" font-size="10">{name[:28]}</text>')
        values = _list(row.get("values"))
        for col, value in enumerate(values[:len(timestamps)]):
            x = left + col * cell_w
            number = _float(value)
            if number is None:
                fill, stroke = "#111827", "#64748b"
            elif number >= 90:
                fill, stroke = "#ff4444", "#ff4444"; plotted += 1
            elif number >= 75:
                fill, stroke = "#ffaa00", "#ffaa00"; plotted += 1
            else:
                fill, stroke = "#22c55e", "#22c55e"; plotted += 1
            items.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(1, cell_w-1):.1f}" height="14" fill="{fill}" stroke="{stroke}" stroke-width=".5"/>')
    if plotted == 0:
        return None, "Fleet heatmap source has rows but zero plotted metric points."
    return _svg(width, height, "".join(items), "Fleet CPU heatmap; outlined cells are metric not emitted"), None


def exception_timeseries_chart(timeseries: dict[str, Any], exception_hosts: list[str]) -> tuple[str | None, str | None]:
    if not exception_hosts:
        return None, None
    source = _dict(timeseries)
    # Host casing differs between the resource list and Azure time-series keys.
    # Resolve it case-insensitively, rather than suppressing a valid exception
    # chart because one endpoint normalised the host differently.
    source_names = {str(name).lower(): str(name) for name in source}
    host = next((source_names.get(candidate.lower()) for candidate in exception_hosts if source_names.get(candidate.lower())), None)
    detail = _dict(source.get(host)) if host else {}
    series = _dict(detail.get("series"))
    cpu = _list(series.get("Percentage CPU"))
    points = [(index, _float(_dict(point).get("v"))) for index, point in enumerate(cpu)]
    points = [(index, value) for index, value in points if value is not None]
    if not points:
        return None, "Exception-host time-series source is absent; no chart was rendered."
    width, height, pad = 920, 260, 38
    span = max(1, len(cpu)-1)
    path = " ".join(f"{'M' if i == 0 else 'L'} {pad + index*(width-pad*2)/span:.1f} {pad + (100-value)*(height-pad*2)/100:.1f}" for i, (index, value) in enumerate(points))
    content = (f'<text x="{pad}" y="22" fill="#e8eaed" font-size="14" font-weight="600">CPU time series — {escape(host or "exception host")}</text>'
               f'<line x1="{pad}" x2="{width-pad}" y1="{pad+(100-80)*(height-pad*2)/100:.1f}" y2="{pad+(100-80)*(height-pad*2)/100:.1f}" stroke="#ffaa00" stroke-dasharray="6 4"/>'
               f'<path d="{path}" fill="none" stroke="#4a9eff" stroke-width="2"/>')
    return _svg(width, height, content, f"CPU time series for exception host {host}"), None


def correlation_chart(events: list[Any]) -> tuple[str | None, str | None]:
    rows = [row for row in events if isinstance(row, dict)]
    if not rows:
        return None, None
    numeric = [(row, _float(row.get("confidence_pct"))) for row in rows]
    known = [(row, confidence) for row, confidence in numeric if confidence is not None]
    if not known:
        return None, "Correlation events have no numeric confidence values; the report retains the textual disclaimer but does not invent confidence." 
    width, height = 920, max(160, 54 + len(known[:8]) * 24)
    content = ['<text x="12" y="22" fill="#e8eaed" font-size="14" font-weight="600">Cross-source correlation confidence</text>']
    for index, (row, confidence) in enumerate(known[:8]):
        y = 44 + index * 24
        label = escape(str(row.get("title") or row.get("metric") or "Correlation event"))[:80]
        content.append(f'<text x="12" y="{y+11}" fill="#e8eaed" font-size="10">{label}</text>')
        content.append(f'<rect x="470" y="{y}" width="380" height="13" rx="2" fill="#2a3040"/>')
        content.append(f'<rect x="470" y="{y}" width="{max(0, min(100, confidence))*3.8:.1f}" height="13" rx="2" fill="#4a9eff"/>')
        content.append(f'<text x="862" y="{y+11}" fill="#e8eaed" font-size="10">{confidence:.0f}%</text>')
    return _svg(width, height, "".join(content), "Correlation confidence; time correlation is not proof of causation"), None


def render_report_charts(report: dict[str, Any]) -> tuple[dict[str, str], list[str], list[str]]:
    """Return inline SVGs plus validation errors and non-blocking warnings."""
    charts: dict[str, str] = {}
    errors: list[str] = []
    warnings: list[str] = []
    batch = _dict(report.get("batch_sla"))
    resource = _dict(report.get("resource_review"))
    chart_calls: dict[str, tuple[str | None, str | None]] = {}
    window_series = _list(batch.get("window_chart_series"))
    long_pole_series = _dict(batch.get("long_pole_trend_series"))
    heatmap_series = _dict(resource.get("fleet_heatmap_series"))
    exception_rows = [str(row.get("host")) for row in _list(resource.get("exception_table")) if isinstance(row, dict)]
    timeseries = _dict(resource.get("timeseries_by_host"))
    correlations = _list(report.get("correlation_rca"))

    # Absent evidence is visible but does not masquerade as a failed chart.
    # Once a source exists, a renderer that yields zero points is a hard gate.
    if window_series:
        chart_calls["batch_window"] = batch_window_chart(window_series)
    else:
        warnings.append("Daily batch window source evidence is absent; chart was not rendered.")
    if long_pole_series:
        chart_calls["long_pole"] = long_pole_chart(long_pole_series)
    else:
        warnings.append("Long-pole trend source evidence is absent; chart was not rendered.")
    if heatmap_series:
        chart_calls["fleet_heatmap"] = fleet_heatmap_chart(heatmap_series)
    else:
        warnings.append("Fleet heatmap source evidence is absent; chart was not rendered.")
    if exception_rows and timeseries:
        chart_calls["exception_timeseries"] = exception_timeseries_chart(timeseries, exception_rows)
    elif exception_rows:
        warnings.append("Exception hosts exist but their time-series source evidence is absent; chart was not rendered.")
    if correlations:
        chart_calls["correlation"] = correlation_chart(correlations)
    for key, (svg, issue) in chart_calls.items():
        if svg:
            charts[key] = svg
        if issue:
            # A populated data source with zero points is a generation error;
            # a genuinely absent optional data source is only a visible warning.
            if "zero plotted" in issue:
                errors.append(issue)
            else:
                warnings.append(issue)
    return charts, errors, warnings
