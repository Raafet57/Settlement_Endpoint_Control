# Static hosting checklist

## Package contents

```text
index.html
robots.txt
llms.txt
README.md
hosting-checklist.md
```

## Safety checks

- No credentials, tokens, keys, passwords, or connection strings.
- No backend calls from the static application.
- No analytics, telemetry, third-party scripts, or remote assets.
- Synthetic fixtures only.
- Clear no-live-operation boundary in UI and documentation.
- Evidence export contains synthetic fixture values only.

## Pre-publication verification

```text
/ returns HTTP 200 text/html
/robots.txt returns HTTP 200 text/plain
/llms.txt returns HTTP 200 text/plain or text/markdown
/README.md returns HTTP 200
/hosting-checklist.md returns HTTP 200
all four tabs are selectable
all three scenarios return the expected verdict
all three role views render
no console or JavaScript errors
no visible undefined/null/NaN/[object Object]
no external browser resources
claim-boundary scan passes
```

Hosting, domain, analytics, and publication decisions remain operator-controlled.
