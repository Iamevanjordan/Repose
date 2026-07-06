# Repose

Ease, tranquility, and freedom are some of the grounding principles in my life that led me to become the person I am today. Repose is a byproduct of the passion I have for creating solutions that genuinely bring people back to themselves.

I built this because I got tired of my brain and a few folders being the only places important context lived. The only sources that remembered what happened yesterday. The thing that caught what mattered in the noise and kept watch over whether any of this was actually working.

So I built five scheduled workflows, gave them shared memory, and started handing off pieces of the weight I've held for so long.

Not to something that thinks for me or some magic agent pretending to run my life. Repose is more like an extension of myself: a set of narrow systems built to relieve cognitive load, preserve context, and make it easier to keep moving without constantly rebuilding the same mental map from scratch.

This is an LLM orchestration framework, not an agent framework, and that distinction matters. Nothing in Repose plans, decides, or acts on its own initiative. Each piece is a scheduled or event-triggered workflow that calls a model for one specific job: write this brief, classify this event, score this item, summarize this context. The workflow logic controls what happens and when. The model handles the language-shaped part of the task. I built it this way on purpose because it is simpler, more debuggable, and more honest about what language models are actually useful for right now.

## Where this came from

I did not build Repose because someone asked for it or because it would look good on a portfolio. I built it because I was running my own operation by hand — audio engineering, harness architecture, consulting, ops work, project after project — and I kept losing time to the same overhead.

Reconstructing context. Catching things late. Forgetting what mattered at technically intensive and crucial junctures inside my builds. Not knowing what was actually working versus what only felt like it was working.

Repose is what happens when that friction gets annoying enough that you stop talking about the fix and just build it.

It is also a marker of how far I have come technically. My last major system, Paxly, took about six months to build. Repose is more complex: real infrastructure, real orchestration, a live messaging interface, shared memory across five independent workflows, and production deployment on a self-hosted VPS. This took me a quarter of the time.

I am not saying that like it is my magnum opus, because it is not. Although it is the most complex system I have built thus far, this feels more like an opening move than a final destination. A proof that this pattern works before more specialized systems get built on top of it.

## What it is made of

Temporal handles scheduling and workflow state. Every workflow here is a real Temporal workflow with real run history, not a cron job dressed up like infrastructure.

LiteLLM sits in front of every model call as a self-hosted gateway, so no workflow ever talks to a vendor directly. Right now the primary reasoning and coding model is DeepSeek V4 Pro, with Claude Sonnet 4.6, Claude Opus 4.6, Claude Haiku 4.5, and a lighter DeepSeek V4 Flash all wired in for different weights of work — long-horizon knowledge tasks, architectural review, fast classification, bulk cleanup — and a local Llama 3.1 parked in the roster as a last-ditch fallback. If a call to the primary model fails, the gateway automatically falls back to a Claude model and retries instead of dropping the job, and the whole thing runs under a hard daily spend cap. Which model does which job is still being actively tuned. This is a live routing table I keep adjusting as I learn what each model is genuinely good at inside this system, not a finalized roster.

ORCA, the Objective Relational Contextual Archive, is the shared memory layer, and it is not a single database. Working state lives in Redis, semantic recall runs on a Weaviate vector store, and the relationships between things live in a Neo4j knowledge graph. Every workflow reads from ORCA and writes back into its own namespace — business-state, session-handoffs, system-events, decision-queue — which is how one workflow can know what another already handled without altering or breaking each other's work.

Bitwarden Secrets Manager is the only place credentials live. No .env files. No hardcoded secrets. No fallback credentials. No exceptions in this system or any other system I run.

Arize Phoenix is the tracing layer, and it runs as its own service. Every model call and workflow step emits an OpenTelemetry trace, and Phoenix is where those land — latency, retries, which model a call actually routed to, and where something failed. It has its own UI, and right now that is where the call-level detail lives. If I need to see why a specific call was slow or fell back to another model, I open Phoenix directly.

A mission control dashboard is the higher-up, coarser view. It is where each workflow and agent is registered and where I track tasks, cost, and the shape of what is in memory — the at-a-glance picture of whether the system is healthy. It reads from its own store rather than reaching live into every backend, so it is deliberately a separate surface from Phoenix for now. Folding Phoenix's traces into it as a panel — so there is genuinely one place to see everything from call-level trace to system health — is active work in progress as of this writing, not something I am going to claim is done when it isn't.

## How the last three came together

`morning_brief` and `session_handoff` were built by hand, one at a time. They were the first real Temporal workflows I wrote myself.

`intel_feed`, `event_monitor`, and `observer` were built differently. I wrote a brief for each one, built a shared utilities layer they all depended on, and then ran my first agentic swarm dispatch through Hermes: several coding agents working in parallel, each building against its own brief and specified quality gates.

None of the three could start until the shared layer passed its gate first. Once that cleared, all three tracks ran in parallel. They came back built and verified together instead of one after another.

That was the first time I ran parallel agent dispatch against real infrastructure instead of a playful example. Lo and behold, it held up!

## The five workflows

I have personalized names for each workflow, which makes the system feel more personal and makes it easier for me to know who is doing what. Out of the box though, each workflow has a generic placeholder name so anyone using the system can come up with names that make sense for them.

**morning_brief** pulls the prior day's activity, decisions that need to be made to continue or start autonomous work, and critical system events into one synthesized morning update. It is live, running at 5:15 AM, and delivering every day.

**session_handoff** closes the gap between work sessions. It creates a structured handoff based on the work you completed, then makes that handoff available to the rest of the system so context does not die when a session ends. Although it may seem small on the surface, this was one of the biggest game changers for me because I had finally found a way to create an internal flow that allowed the current system state to update itself without me manually having to reconstruct everything.

**intel_feed** scans curated, vetted information sources on a fixed schedule and scores what it finds for relevance based on my current system and projects before anything reaches me. Personal autonomous researcher that is live and running every day. The reliability is there; the relevance tuning is still being refined to my personal tastes and interests.

**event_monitor** classifies inbound events and routes them by urgency. In my own setup right now, nothing is pointed at it because I do not currently have six different services throwing webhooks that need triage. It ships as a complete working template. Wire it to Stripe, GitHub, a form, or whatever generates signal for your setup, and it sorts by urgency the way it was built to.

**observer** watches the other workflows for drift: silence, errors, quality dropping off. Seeing the unseen. It reads agent logs, Temporal workflow states, and model-routing patterns across the whole system. Right now it is intentionally quiet — read-only, cold-start, by design. It can look, but it cannot write what it finds to memory yet, and it cannot surface anything to me over Telegram yet either. I did not want it earning either of those privileges until what it was seeing had proven trustworthy first, which is the same standard I have held every piece of this build to.

## Why I built it this way

One of my governing rules, derived from the wise words of Jensen Huang, is:

**as complex as necessary, as simple as possible.**

That does two jobs.

On the architecture side, it means there is no machinery here that is not earning its place. Every piece exists because a simpler version either would not hold up or would have made the system harder to trust once it was actually running.

On the human side, it means Repose should be understandable without needing to already live inside my head. Someone should be able to understand the everyday relevance quickly — less fog, less repeated context reconstruction, less mental drag — and still have enough architectural detail to see how the system actually works underneath.

That balance matters to me which is why I would always rather build something usable than something that only sounds impressive from far away.

## What comes after this

Repose proves the pattern works: scheduled workflows, shared memory, live messaging, relevance filtering, autonomous research, and routing attention toward what needs a human now instead of later.

The next layer being developed is more specialized: custom harnesses combined with finely tuned models that allow for self-sufficient systems with domain-specific routing and tool-use authority. Repose is not the harness layer. Repose is the substrate those systems can run on, coordinate through, or grow out of.

This purely sets the stage.

To be honest, I do not look at this like I made the best thing in the world. I built a real thing because I had a real need. It is still being tuned and curated, but it works, and it was built with true care and attention to detail.

## Where it stands right now

Repose V1 is running in production on a single self-hosted VPS and is actively being extended.

This is also the last project I am putting up on this GitHub account before moving future work to a more permanent, safer home for the codebases I plan to keep building on.

## Getting it running

There is no generic install script here on purpose.

Every environment is different enough that providing a one-size-fits-all setup guide may end up harming more than helping you down the line. Point your coding agent at this repository and have it read the codebase line by line, then generate a setup guide for your actual environment.

That will give you something much more accurate than a static guide written in advance that is bound to go stale.

The one rule that does not bend:

**Secrets live in a secrets manager.**

Never in an `.env` file. Never hardcoded. Never as a fallback. If a setup guide — yours, mine, or anyone else's — tells you to put a credential in an `.env` file, that step is wrong.

## Credit

ORCA, the memory layer this system runs on, is my friend and mentor Eddie's creation.(@eddiksonpena)

Repose is a consumer and customizer of ORCA, not its origin.
