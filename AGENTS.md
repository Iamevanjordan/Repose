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

Both runtimes share the same backing services (ORCA memory API, LiteLLM
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
- **ORCA memory API** — durable agent memory (`/v1/memories/ingest`,
  `/v1/memories/recall`), namespaced per agent. ORCA is the memory layer's name,
  after **Eddie's ORCA project**. Some deployed/wire-level identifiers
  (the `chronogram.http` config-key namespace, the memory-api service hostname,
  and the external `src.chronogram` client package) retain the prior
  `chronogram` name by design — they are infrastructure names, not the layer's
  cosmetic name.
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
| **observer** | `python -m repose.agents.observer_observer` (`observer_core`) | periodic observer cycle | `observer-observations`, `system-events` | live; **cold-start warmup** — durable writes proceed normally; only Telegram surfacing is withheld until each agent clears its warmup grace. The separate ORCA write-mode capability lock lifts `write_mode_activation_date: 2026-07-02` |

Internal workflow / schedule IDs in the operator's runtime may carry codenames
rather than these role names (see §7); that is expected and not reconciled here.

## 5. ORCA Namespace Map (real writes)

- `intel_feed-archive` — scored/surfaced intel signals (`intel_feed`).
- `event_monitor-events` — classified webhook events; `decision-queue` —
  events routed to "decision required" (`event_monitor`).
- `morning_brief-briefs` — delivered morning briefs + delivery receipts.
- `session-handoffs` — session wrap/handoff records; `business-state` —
  business-state deltas (`session_handoff`).
- `observer-observations` — observer findings (durable writes proceed during
  cold-start; only Telegram surfacing is withheld during warmup grace).
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
- **Idempotency keys** are emitted as `Idempotency-Key` headers on ORCA
  writes and Telegram sends. Effective dedup-on-retry requires the
  ingest endpoint / a Telegram-fronting router to honor the header; until then
  the header is inert but harmless. **(partially verified — header emitted;
  server-side dedup unverified)**
- **observer** is in its cold-start warmup window: it writes observations to
  durable memory normally, but withholds Telegram surfacing until each agent
  clears its warmup grace. The separate ORCA write-mode capability lock lifts
  `2026-07-02` and has not been exercised. **(unverified)**

## 9. Review Scope (for Codex)

**Flag:**
- Any credential/host/path read outside the Bitwarden pattern (env reads,
  hardcoded hosts, literal secrets).
- Workflow activities whose ORCA writes / Telegram sends are
  non-idempotent on Temporal retry.
- Fetch/ingest paths that bypass the egress allowlist, or sanitization paths
  that strip-but-don't-block on `block_patterns`.
- Any agent that **surfaces to the operator** (Telegram) while still inside a
  declared warmup/cold-start window, or that writes outside its declared
  namespace scope.

**Do not flag:**
- **observer** durable writes to `observer-observations` / `system-events`
  during its cold-start warmup window — intentional, decided 2026-06-18.
  Cold-start withholds operator **surfacing**, not durable writes; the
  `write_mode_activation_date: 2026-07-02` flag governs only the ORCA
  mutating-operation capability lock, never observation recording (§4, §5).
- The role-based ↔ codename naming split (§7) — intentional.
- Local TF-IDF novelty scoring instead of embeddings — intentional.
- Heuristic fallback in scoring/classification and in-memory dedup fallback —
  intentional graceful degradation.
- `Idempotency-Key` headers that a backend may not yet consume — forward-looking
  scaffolding (§8).
