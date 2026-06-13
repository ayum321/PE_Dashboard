"""Verify whether _parse_time handles datetime.time objects, text strings,
and multi-line cells correctly."""
from datetime import time, datetime
import re

_TIME_FORMATS = [
    "%I:%M %p", "%I.%M %p", "%I.%M%p",
    "%H:%M", "%I %p", "%I:%M%p",
    "%H:%M:%S", "%I:%M:%S %p", "%H:%M:%S.%f",
    "%I%p",
]
_TZ_SUFFIX = re.compile(
    r'\s+(?:CST|CDT|EST|EDT|PST|PDT|MST|MDT|IST|GMT|UTC[+-]?\d*|PHT|ET|CT|PT|MT)\s*$',
    re.IGNORECASE)

def _parse_time_current(raw):
    if not raw or str(raw).lower() in ("nan", "none", "", "nat"):
        return None
    raw = str(raw).strip()
    raw = _TZ_SUFFIX.sub('', raw).strip()
    raw = re.sub(r'\s*\([^)]*\)\s*$', '', raw).strip()
    _tm = re.match(r'^(\d{1,2}[.:]\d{2}(?::\d{2})?(?:\s*[AP]M)?|\d{1,2}\s*[AP]M|\d{1,2}[AP]M)', raw, re.I)
    if _tm:
        raw = _tm.group(1).strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).time()
        except (ValueError, TypeError):
            continue
    m = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?', raw, re.I)
    if m:
        try:
            hh = int(m.group(1)); mm = int(m.group(2))
            ampm = (m.group(4) or "").upper()
            if ampm == "PM" and hh < 12: hh += 12
            elif ampm == "AM" and hh == 12: hh = 0
            return time(hh, mm)
        except Exception:
            pass
    return None

cases = [
    # (label, input, expected_output)
    ("Excel Time cell (datetime.time)",    time(10, 45, 0),     time(10, 45)),
    ("Excel Time cell (datetime.time) PM", time(21, 0, 0),      time(21, 0)),
    ("Text '10.45 AM'",                    "10.45 AM",          time(10, 45)),
    ("Text '10.45AM' (no space)",          "10.45AM",           time(10, 45)),
    ("Text '10.45 am' (lowercase)",        "10.45 am",          time(10, 45)),
    ("Text '9:00 PM'",                     "9:00 PM",           time(21, 0)),
    ("Text '21:00'",                       "21:00",             time(21, 0)),
    ("Multi-line '10.45 AM\\nExtra'",      "10.45 AM\nExtra",   time(10, 45)),
    ("Multi-line '11.23 AM\\nExtra'",      "11.23 AM\nExtra",   time(11, 23)),
    ("Text '5AM'",                         "5AM",               time(5, 0)),
    ("Float from Excel (0.448611=10:46)",  0.448611,            None),  # pandas serial
]

print("%-45s  %-20s  %-12s  %s" % ("Case", "Input", "Expected", "Got"))
print("-" * 100)
all_pass = True
for label, inp, expected in cases:
    result = _parse_time_current(inp)
    ok = "PASS" if result == expected else "FAIL"
    if ok == "FAIL":
        all_pass = False
    print("%-45s  %-20s  %-12s  %s" % (label, repr(inp)[:20], str(expected), str(result) + " " + ok))

print()
print("All PASS" if all_pass else "FAILURES DETECTED — fix required")
