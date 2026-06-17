# AGENTS.md — Repose

> Generated from the post-fix source tree and verified against live runtime
> behavior (systemd units, in-container probes, gateway/Redis round-trips).
> Claims that could not be verified are marked **(unverified)**.
> Agents carry **role-based names** in this repository; see §7.

## 1. What Repose Is

Repose is a personal-operations agent OS: a small set of always-on agents that
ingest signals (RSS intel, webhooks, session events), score/classify them,
write durable memory, and surface only what matters to the operator over
Telegram. It is **two-runtime** by design:

- **agent-os runtime** — Temporal-backed workflow agents (`morning_brief`,
  `session_handoff`) running on the host Python venv at `/opt/agent-os`.
- **repose-in-container runtime** — daemon agents (`intel_feed`,
  `event_monitor`, `observer`) running inside the workspace container via
  `docker exec`, importing the `repose` package.

Both runtimes share the same backing services (Chronogram memory API, LiteLLM
gateway, Redis, Bitwarden, Telegram).

## 2. Runtime Model

| Runtime | How it starts | Where code lives |
|---|---|---|
| agent-os | host systemd units → `/opt/agent-os/.venv/bin/python3` | `src/agents/{morning_brief,session_handoff}` |
| repose-in-container | host systemd units → `docker exec -u 10010 … python3 -m repose.agents.<entry>` | `repose/agents/…` (this repo) |

- In-container agents run as uid 10010 with `PYTHONPATH` pointing at the repose
  package root. `docker exec` does **not** forward SIGTERM, so each unit uses
  `bin/stop_agent.sh <module-marker>` for both `ExecStop` and an `ExecStartPre`
  orphan guard (the container has no `pkill`); the guard waits for the prior
  process to exit before a fresh bind.
- Restart policy is `Restart=always`; singleton PID-locks live in `/tmp`.

## 3. Stack (verified)

- **Python 3.11/3.12**, `pyyaml`, `redis>=4.5`, `bitwarden-sdk>=1.0`,
  `feedparser>=6.0`, `httpx`, `temporalio`.
- **LiteLLM gateway** (OpenAI-compatible HTTP) for all model calls — no model
  SDKs in-tree; calls are plain HTTP with a Bitwarden-resolved key.
- **Chronogram memory API** — durable agent memory (`/v1/memories/ingest`,
  `/v1/memories/recall`), namespaced per agent.
- **Redis** — coordination + dedup state (resolved from Bitwarden, never
  localhost/env).
- **Telegram** — operator surfacing.
- **cloudflared** — public ingress tunnel for `event_monitor` webhooks.
- **Temporal** — durable workflow execution for the agent-os agents.

## 4. Agent Roster

| Agent (role) | Entry point | Schedule / trigger | Primary namespaces | Status |
|---|---|---|---|---|
| **morning_brief** | Temporal morning-brief workflow | daily cron **09:15 UTC** (~5:15am ET) | `morning_brief-briefs` | live |
| **session_handoff** | `session_handoff/bot_listener` → Temporal handoff workflow | Telegram handoff message → workflow | `session-handoffs`, `business-state` | live |
| **intel_feed** | `python -m repose.agents.intel_feed_scheduler` | **3×/day** at 06:00 / 13:00 / 20:00 UTC | `intel_feed-archive`, `system-events` | live |
| **event_monitor** | `python -m repose.agents.event_monitor` | webhook-driven (cloudflared → :8080) | `event_monitor-events`, `decision-queue`, `system-events` | live |
| **observer** | `python -m repose.agents.observer_observer` (`observer_core`) | periodic observer cycle | `observer-observations`, `system-events` | **read-only cold-start** until `write_mode_activation_date: 2026-07-02`; surfacing disabled |

Internal workflow / schedule IDs in the operator's runtime may carry codenames
rather than these role names (see §7); that is expected and not reconciled here.

## 5. Chronogram Namespace Map (real writes)

- `intel_feed-archive` — scored/surfaced intel signals (`intel_feed`).
- `event_monitor-events` — classified webhook events; `decision-queue` —
  events routed to "decision required" (`event_monitor`).
- `morning_brief-briefs` — delivered morning briefs + delivery receipts.
- `session-handoffs` — session wrap/handoff records; `business-state` —
  business-state deltas (`session_handoff`).
- `observer-observations` — observer findings (writes gated by cold-start).
- `system-events` — shared operational/audit events (sanitization strips/blocks,
  failures) across agents.

Writes are real `remember()` calls (not stubs). Novelty scoring in `intel_feed`
uses **local TF-IDF**, not embeddings — intentional (the gateway serves no
embedding model).

## 6. Secrets Pattern

- **Bitwarden SDK only.** Zero `os.environ` reads for credentials anywhere.
  Configs store **references** (`bitwarden:<secret-id>`), never values.
- LLM key: `litellm-master-key`. Redis: `repose-redis-host` /
  `repose-redis-port` (shared `redis_state` resolver, ping-verified, per-db
  cache). Webhook/Telegram secrets referenced by id in YAML.
- An unreachable gateway/key degrades to a deterministic heuristic so the scan
  path never blocks; Redis dedup degrades to in-memory only as last resort.

## 7. Naming Split (intentional)

Agents have **role-based names in this repository** (`morning_brief`,
`session_handoff`, `intel_feed`, `event_monitor`, `observer`). The operator's
private runtime uses internal codenames for the same agents. This split is
deliberate — **do not reconcile names** across the repo and the operator's
runtime. Internal workflow IDs / schedule IDs in the agent-os runtime may still
carry codenames; that is expected.

## 8. Known Gaps

No open items from the remediation pass — `intel_feed` config-path duplication,
the `intel_feed` scoring→gateway routing, dormant-module cleanup, the
agent-os idempotency/UUID deployment, egress-allowlist + block-pattern
enforcement, `event_monitor` Redis dedup, and the systemd orphan guard are all
resolved and verified.

Forward-looking notes (not regressions):
- **Idempotency keys** are emitted as `Idempotency-Key` headers on Chronogram
  writes and Telegram sends. Effective dedup-on-retry requires the
  ingest endpoint / a Telegram-fronting router to honor the header; until then
  the header is inert but harmless. **(partially verified — header emitted;
  server-side dedup unverified)**
- **observer** remains in its read-only cold-start window; write-mode behavior
  activates `2026-07-02` and has not been exercised. **(unverified)**

## 9. Review Scope (for Codex)

**Flag:**
- Any credential/host/path read outside the Bitwarden pattern (env reads,
  hardcoded hosts, literal secrets).
- Workflow activities whose Chronogram writes / Telegram sends are
  non-idempotent on Temporal retry.
- Fetch/ingest paths that bypass the egress allowlist, or sanitization paths
  that strip-but-don't-block on `block_patterns`.
- Any agent that can write while in a declared cold-start/read-only window.

**Do not flag:**
- The role-based ↔ codename naming split (§7) — intentional.
- Local TF-IDF novelty scoring instead of embeddings — intentional.
- Heuristic fallback in scoring/classification and in-memory dedup fallback —
  intentional graceful degradation.
- `Idempotency-Key` headers that a backend may not yet consume — forward-looking
  scaffolding (§8).
