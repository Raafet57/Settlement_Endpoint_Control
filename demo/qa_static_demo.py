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
DB_README = ROOT.parent / "demo-db" / "README.md"

required_files = [INDEX, README, LLMS, ROBOTS, HOSTING]
missing = [str(p) for p in required_files if not p.exists()]
if not DB_README.exists():
    missing.append(str(DB_README))
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
    "Skip to demo console",
    "not a settlement instruction",
    "Replay the control moment",
    # Control-moment copy stays advisory: the model selects the fallback route;
    # it never implies moving value.
    "selects the governed fiat fallback",
    # Blocked-scenario repair text must name every failed or expiring control.
    "refresh the stale wallet allowlist",
    "complete the endpoint-control payload",
    "renew the expiring authority evidence",
    # Working-application section must describe the localhost depth accurately.
    "deterministic evaluator",
    "localhost reference implementation",
    "synthetic operational-depth proof",
    "computes deterministic advisory outputs",
    "advisory",
    "superseded",
    "evidence refresh",
    "deterministic revalidation",
    "new linked decision version",
    # Decision history is scoped to the application API, never the database.
    "Decision history is append-only through the localhost application API",
    "no overwrite path",
    "Nondestructive, versioned schema migrations through v6",
    "127.0.0.1",
    "no authentication",
    "no payment execution",
]
missing_strings = [s for s in required_strings if s not in text]
if missing_strings:
    raise SystemExit(f"MISSING_STRINGS {missing_strings}")

# Landing-page narrative contract: each of the five section anchors occurs
# exactly once and in the required reading order.
section_ids = [
    "id=\"problem\"",
    "id=\"moment\"",
    "id=\"console\"",
    "id=\"application\"",
    "id=\"boundary\"",
]
positions = []
for marker in section_ids:
    count = text.count(marker)
    if count != 1:
        raise SystemExit(f"SECTION_ID_COUNT {marker} count={count}")
    positions.append(text.index(marker))
if positions != sorted(positions):
    raise SystemExit(f"SECTION_ORDER {list(zip(section_ids, positions))}")

avoid_terms = [
    "production-ready",
    "payment-network-integrated",
    "certified",
    "real-time wallet verification",
    "live vLEI verification",
    "fully compliant",
    "regulator-approved",
    "production-grade",
    "bank-grade",
    "enterprise-ready",
    "battle-tested",
    "tamper-evident",
    "tamper-proof",
    "signed receipt",
    "real-time screening",
]
found = [term for term in avoid_terms if re.search(re.escape(term), text, re.IGNORECASE)]
if found:
    raise SystemExit(f"FORBIDDEN_TERMS {found}")

# Overreach phrases retired by review: execution implication and database-level
# immutability claims must stay out of every shipped copy file.
overreach_terms = [
    "computes real advisory decisions",
    "keeps the payment moving",
    "immutable decision history",
    "immutable decision versions",
    "immutable decision snapshots",
    "never rewrite history",
    "names exactly what to refresh",
]
copy_files = [INDEX, README, LLMS, HOSTING, DB_README]
overreach_found = []
for path in copy_files:
    body = path.read_text(encoding="utf-8")
    for term in overreach_terms:
        if re.search(re.escape(term), body, re.IGNORECASE):
            overreach_found.append(f"{path.name}:{term}")
if overreach_found:
    raise SystemExit(f"OVERREACH_CLAIMS {overreach_found}")

# Decision-history claims stay scoped to the localhost application API in the
# supporting docs as well (whitespace-tolerant: markdown wraps lines).
scoped_history_claim = re.compile(
    r"append-only\s+decision\s+history\s+through\s+the\s+localhost\s+application\s+API"
)
for path in (README, LLMS):
    if not scoped_history_claim.search(path.read_text(encoding="utf-8")):
        raise SystemExit(f"MISSING_SCOPED_HISTORY_CLAIM {path.name}")
if not re.search(
    r"append-only\s+decision\s+snapshots\s*/\s*version\s+links\s+through\s+the\s+application\s+API",
    DB_README.read_text(encoding="utf-8"),
    re.IGNORECASE,
):
    raise SystemExit("MISSING_DB_API_SCOPED_HISTORY_CLAIM")

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
