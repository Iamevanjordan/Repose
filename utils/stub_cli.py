"""
Stub CLI for agents not yet built (Track 1/2/3 placeholders).

Provides basic list/history/test commands that return empty/deterministic
results so POL criterion 10 can pass before the full agent is built.
"""

from repose.utils.cli_base import CLIBase


class StubCLI(CLIBase):
    """Placeholder CLI for an agent that has not been built yet."""

    nouns = ["sources", "observations", "status"]

    def handle(self, noun: str, verb: str, args: list):
        """Stub handler with deterministic output for list/history/test."""
        if verb in ("list", "history"):
            return []

        if verb == "test":
            return {
                "agent": self.agent_name,
                "status": "not_implemented",
                "message": f"Agent '{self.agent_name}' not yet built.",
            }

        if verb in ("enable", "disable"):
            target = args[0] if args else "all"
            return {
                "agent": self.agent_name,
                "noun": noun,
                "verb": verb,
                "target": target,
                "status": "not_implemented",
            }

        if verb == "setup":
            return {
                "agent": self.agent_name,
                "status": "not_implemented",
            }

        return {
            "agent": self.agent_name,
            "noun": noun,
            "verb": verb,
            "status": "not_implemented",
        }


# ── Pre-built stub CLIs for each agent ──────────────────────────────────

# Intel_feedCLI is now a real implementation in repose.cli.intel_feed_cli
# Import it from there instead of using the stub.
try:
    from repose.cli.intel_feed_cli import Intel_feedCLI as _RealIntel_feedCLI
    Intel_feedCLI = _RealIntel_feedCLI
except ImportError:
    class Intel_feedCLI(StubCLI):
        agent_name = "intel_feed"
        nouns = ["sources", "observations", "sanitization", "scan", "status", "test"]


class Event_monitorCLI(StubCLI):
    agent_name = "event_monitor"
    nouns = ["sources", "events", "ingress", "stripe", "github", "form", "status"]


class ObserverCLI(StubCLI):
    agent_name = "observer"
    nouns = ["observations", "ack", "admin", "status"]


class Morning_briefCLI(StubCLI):
    agent_name = "morning_brief"
    nouns = ["brief", "sources", "status"]


class Session_handoffCLI(StubCLI):
    agent_name = "session_handoff"
    nouns = ["session", "handoffs", "status"]
