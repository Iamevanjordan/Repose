# Repository Architecture — Public/Private Split

## Two Trees, One System

Repose maintains two intentionally divergent git histories. This is by design.

**Private tree** (VPS: `/opt/hermes-workspace/workspace-home/repose/`)
- Agent codenames: Hermes, Helios, Hëktr, Libra, Pulse, Watson
- 1 root commit. HEAD: d2393cf
- Live operational system. Never pushed publicly.
- Source of truth for runtime behavior.

**Public tree** (GitHub: `Iamevanjordan/Repose`, staging: `~/repose-public-staging`)
- Role-based names: morning_brief, intel_feed, event_monitor, session_handoff, observer
- 14 commits. HEAD: eda371d
- Sanitized for open source. No real IPs, tokens, usernames, or absolute paths.
- Source of truth for Codex review, documentation, and external readers.

## Rules

1. Changes to behavior happen in the private tree first.
2. Sanitized equivalents are ported to the public tree — never the reverse.
3. The name mapping is 1:1. Every private agent has one public role name.
4. Any task touching the public tree must use role-based names throughout.
5. Codex review, README, architecture docs all operate against the public tree.

## Name Map

| Private (operational) | Public (role-based) |
|---|---|
| Helios | morning_brief |
| Hëktr | session_handoff |
| Libra | intel_feed |
| Pulse | event_monitor |
| Watson | observer |

## Memory-Layer Naming (ORCA)

The cross-agent memory layer is named **ORCA** (after Eddie's ORCA project).
This casing — all-caps **ORCA** — is used in both trees, public and private,
across code identifiers, docstrings, comments, and prose.

This is a **cosmetic/identifier** rename of the memory layer's name. It is
**not** a credentials or infrastructure migration. The following retain the
prior `chronogram` identifier **by design**, in both trees:

| Retained `chronogram` reference | Why it stays |
|---|---|
| `chronogram:` / `chronogram.http` config-key namespace (YAML + `cfg.get("chronogram")`) | Wiring convention bound to the live deployed config |
| `chronogram-memory-api-service-1` service hostname | Real deployed Docker DNS name; renaming breaks resolution |
| `bitwarden:agent-os-chronogram-key`, `bitwarden:observer-chronogram-read-token` | Bitwarden secret IDs — never renamed |
| `chronogram_read_secret_id` / `chronogram_read_namespaces` | Config keys (one references a secret) |
| `src.chronogram.client` / `ChronogramClient` | External client package, not part of this repo |
| `write_brief_to_chronogram` / `write_handoff_to_chronogram` | Temporal activity names + idempotency-key strings (dedup contract) |

The local memory module was renamed `repose.utils.chronogram` →
`repose.utils.orca` (class `ChronogramError` → `OrcaError`); the file is
`utils/orca.py` (lowercase per Python convention; identifiers and docs use
`ORCA`).

## Why

The split protects operational security (no real infra identifiers in the public repo) while enabling a clean open-source release. The unrelated git histories are intentional — the public tree was initialized separately to avoid leaking any commit history from the private tree.

### Build Architecture Note (V1 artifact, V2 target)
The two runtimes (agent-os Temporal workflows vs repose-in-container daemons)
are an incremental build artifact, not a design decision. morning_brief and
session_handoff were built first on the host with Temporal. intel_feed,
event_monitor, and observer were built later inside the workspace container by
the Hermes swarm. V2 target: migrate all five to Temporal workflows on the host,
eliminating the docker exec pattern and container-as-execution-environment dependency.