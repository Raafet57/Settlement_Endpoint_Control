#!/usr/bin/env python3
"""Static QA for the Settlement Endpoint Control Tower demo."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
README = ROOT / "README.md"
LLMS = ROOT / "llms.txt"
ROBOTS = ROOT / "robots.txt"
HOSTING = ROOT / "hosting-checklist.md"

required_files = [INDEX, README, LLMS, ROBOTS, HOSTING]
missing = [str(p) for p in required_files if not p.exists()]
if missing:
    raise SystemExit(f"MISSING_FILES {missing}")

text = INDEX.read_text(encoding="utf-8")
required_strings = [
    "Settlement Endpoint Control Tower",
    "Content-Security-Policy",
    "static browser app",
    "data-screen=\"profile\"",
    "data-screen=\"validation\"",
    "data-screen=\"decision\"",
    "data-screen=\"audit\"",
    "Blocked wallet endpoint",
    "Refreshed endpoint approved",
    "Authority evidence expired",
    "Download synthetic evidence JSON",
    "Start Guided Walkthrough",
    "Guided walkthrough",
    "Evidence Receipt",
    "Download evidence receipt",
    "settlement-endpoint-evidence.json",
    "evidenceReceipt",
    "window.__settlementDemo",
    "connect-src 'none'",
    "synthetic data",
]
missing_strings = [s for s in required_strings if s not in text]
if missing_strings:
    raise SystemExit(f"MISSING_STRINGS {missing_strings}")

avoid_terms = [
    "production-ready",
    "payment-network-integrated",
    "certified",
    "real-time wallet verification",
    "live vLEI verification",
    "fully compliant",
    "regulator-approved",
]
found = [term for term in avoid_terms if re.search(re.escape(term), text, re.IGNORECASE)]
if found:
    raise SystemExit(f"FORBIDDEN_TERMS {found}")

external_refs = [m.group(0) for m in re.finditer(r"https?://[^'\"\s<]+|//cdn\.|analytics|gtag|segment\.com|plausible|posthog", text, re.I)]
unexpected_refs = external_refs
if unexpected_refs:
    raise SystemExit(f"EXTERNAL_REF_HINTS {unexpected_refs[:8]}")

retired_terms = ["sw" + "ift", "ap" + "ix", "hack" + "athon"]
retired_found = [term for term in retired_terms if re.search(term, text, re.IGNORECASE)]
if retired_found:
    raise SystemExit("RETIRED_EVENT_ORGANIZER_REFERENCE")

if text.count("<section id=\"view-") != 4:
    raise SystemExit("EXPECTED_4_VIEWS")
if text.count("<option value=") != 6:
    raise SystemExit("EXPECTED_3_SCENARIOS_AND_3_ROLES")

print("STATIC_QA PASS")
print(f"index_bytes={INDEX.stat().st_size}")
print(f"files={','.join(p.name for p in required_files)}")
