# Compliance-oriented control mapping

This is engineering guidance, not certification.

| Theme | Implementation |
|---|---|
| GDPR data minimization | Metadata index rather than footage duplication; custom evidence retained only from selected local sources |
| GDPR integrity/confidentiality | Loopback-only service, canonical path checks, no remote dependencies |
| GDPR transparency | Provenance evidence, confidence, transformations, and fidelity manifest |
| ASVS input validation | Bounded JSON, canonical filesystem checks, structured subprocess arguments |
| ASVS file handling | Extension allowlist, no source writes, atomic output promotion |
| ASVS logging | Structured request logs without request bodies or media contents |
| SOC 2 change integrity | Tests, CI checks, manifest, explicit preflight and review gate |
| SOC 2 availability | Resumable persisted library/project state; failed render leaves no promoted partial output |

