# Security posture

## Supported scope

This repository contains a self-contained static demo and a localhost-only SQLite workflow proof. Both use synthetic fixtures.

## Safety boundaries

- The static demo performs no external network calls.
- The SQLite application refuses non-localhost binds.
- Generated databases, environment files, caches, browser artifacts, and media are ignored.
- No credentials, secrets, customer data, production payment data, or proprietary reference rows belong in the repository.
- The SQLite application is not reviewed or approved for public internet exposure.

## Reporting

Use GitHub's private vulnerability reporting or repository security advisory flow for suspected vulnerabilities. Do not include real credentials, account data, payment data, or customer records in reports or reproduction fixtures.
