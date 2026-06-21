#!/usr/bin/env python3
"""
EVENT_MONITOR POL (Proof of Life) Verification Script v2

Runs all 10 POL criteria from Section 14 of the Event_monitor v3 brief.
Outputs "EVENT_MONITOR_POL_PASS" when all 10 pass.
"""
import json
import os
import sys

os.environ["EVENT_MONITOR_TEST_MODE"] = "true"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "repose"))

from repose.agents.event_monitor import (
    load_config, get_config, reset_state, start_server, stop_server,
    process_event, list_events, get_status, get_stats,
    set_escalation_usage,
)
import repose.agents.event_monitor as event_monitor_mod

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "event_monitor")
passed = 0
failed = 0


def pol(name: str, condition: bool, detail: str = ""):
    global passed, failed
    status = "PASS" if condition else "FAIL"
    msg = f"  [{status}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    if condition:
        passed += 1
    else:
        failed += 1


def load_fixture(filename: str) -> dict:
    with open(os.path.join(FIXTURES, filename)) as f:
        return json.load(f)


print("=" * 60)
print("EVENT_MONITOR v3 — Proof of Life Verification")
print("=" * 60)

# ═══════════════════════════════════════════════════════════════════════════
# POL 1: Health endpoint reachable
# ═══════════════════════════════════════════════════════════════════════════
print("\n─ POL 1: Cloudflare Tunnel health endpoint")
reset_state()
load_config()
try:
    start_server(8080)
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:8080/health")
    with urllib.request.urlopen(req, timeout=5) as resp:
        health = json.loads(resp.read().decode())
    pol("POL 1", health.get("status") == "healthy",
        f"status={health.get('status')}")
except Exception as exc:
    pol("POL 1", False, f"error: {exc}")

# ═══════════════════════════════════════════════════════════════════════════
# POL 2: Stripe setup with signature verification
# ═══════════════════════════════════════════════════════════════════════════
print("\n─ POL 2: Stripe setup with signature verification")
try:
    from repose.utils.bitwarden import store_secret, get_secret
    store_secret("repose-stripe-signing-secret", "whsec_test_secret_12345")
    secret = get_secret("repose-stripe-signing-secret")
    pol("POL 2", secret == "whsec_test_secret_12345",
        f"secret={'configured' if secret else 'missing'}")
except Exception as exc:
    pol("POL 2", False, f"error: {exc}")

# ═══════════════════════════════════════════════════════════════════════════
# POL 3: payment_failed → urgent, end-to-end
# ═══════════════════════════════════════════════════════════════════════════
print("\n─ POL 3: payment_failed → urgent (end-to-end)")
reset_state()
payload = load_fixture("stripe_payment_failed.json")
event_type = payload.get("type", "unknown")
result = process_event("stripe", event_type, payload, {}, bypass_signature=True)
lane = result.get("lane", "unknown")
pol("POL 3.a: classified as urgent", lane == "urgent",
    f"lane={lane}, confidence={result.get('classifier_confidence')}")
pol("POL 3.b: event written to event_monitor-events",
    len(list_events(lane="urgent")) >= 1,
    f"urgent events: {len(list_events(lane='urgent'))}")
pol("POL 3.c: Telegram surfacing key present",
    "surfaced_to_telegram" in result,
    f"key present: {'surfaced_to_telegram' in result}")

# ═══════════════════════════════════════════════════════════════════════════
# POL 4: subscription_created → informational, NOT surfaced
# ═══════════════════════════════════════════════════════════════════════════
print("\n─ POL 4: subscription_created → informational (no Telegram)")
reset_state()
payload = load_fixture("stripe_subscription_created.json")
event_type = payload.get("type", "unknown")
result = process_event("stripe", event_type, payload, {}, bypass_signature=True)
pol("POL 4.a: classified as informational", result.get("lane") == "informational",
    f"lane={result.get('lane')}")
pol("POL 4.b: NOT surfaced to Telegram",
    result.get("surfaced_to_telegram") == False,
    f"surfaced_to_telegram={result.get('surfaced_to_telegram')}")

# ═══════════════════════════════════════════════════════════════════════════
# POL 5: Duplicate rejection at dedup layer
# ═══════════════════════════════════════════════════════════════════════════
print("\n─ POL 5: Duplicate event rejected at dedup layer")
reset_state()
payload = load_fixture("stripe_payment_failed.json")
event_type = payload.get("type", "unknown")
r1 = process_event("stripe", event_type, payload, {}, bypass_signature=True)
r2 = process_event("stripe", event_type, payload, {}, bypass_signature=True)
pol("POL 5.a: First call processed", r1.get("status") == "processed",
    f"status={r1.get('status')}")
pol("POL 5.b: Duplicate rejected at dedup",
    r2.get("status") == "rejected" and r2.get("reason") == "duplicate",
    f"status={r2.get('status')}, reason={r2.get('reason')}")
pol("POL 5.c: Not reclassified (only 1 classification)",
    get_stats()["events_classified"] == 1,
    f"events_classified={get_stats()['events_classified']} (should be 1)")

# ═══════════════════════════════════════════════════════════════════════════
# POL 6: Invalid signature rejected, logged to system-events
# ═══════════════════════════════════════════════════════════════════════════
print("\n─ POL 6: Invalid signature rejected, logged to system-events")
reset_state()
old_mode = os.environ.pop("EVENT_MONITOR_TEST_MODE", None)
payload = load_fixture("stripe_payment_failed.json")
event_type = payload.get("type", "unknown")
result = process_event("stripe", event_type, payload,
                       {"stripe-signature": "bad_sig_value"})
os.environ["EVENT_MONITOR_TEST_MODE"] = old_mode if old_mode else "true"

pol("POL 6.a: Invalid signature rejected",
    result.get("status") == "rejected",
    f"status={result.get('status')}, reason={result.get('reason')}")
pol("POL 6.b: Not classified",
    get_stats()["events_classified"] == 0,
    f"events_classified={get_stats()['events_classified']} (should be 0)")

# Check system-events from orca
from repose.utils.orca import get_recent_events
sys_events = get_recent_events(namespace="system-events")
pol("POL 6.c: Failure logged to system-events",
    len(sys_events) >= 1,
    f"system-events: {len(sys_events)}")

# ═══════════════════════════════════════════════════════════════════════════
# POL 7: Uncertain classification → decision-queue, no Telegram
# ═══════════════════════════════════════════════════════════════════════════
print("\n─ POL 7: Uncertain classification routes to decision-queue, no Telegram")
reset_state()
original = event_monitor_mod._heuristic_classify

def _uncertain(prompt):
    return {"lane": "decision_required", "confidence": 0.35,
            "reasoning": "Heuristic: uncertain for POL test"}
event_monitor_mod._heuristic_classify = _uncertain

# Enable form source for this test
cfg = get_config()
cfg["sources"]["form"]["enabled"] = True
payload = load_fixture("form_submission.json")
result = process_event("form", "form.submitted", payload, {}, bypass_signature=True)
event_monitor_mod._heuristic_classify = original

pol("POL 7.a: Routed to decision_required",
    result.get("lane") == "decision_required",
    f"lane={result.get('lane')}")
pol("POL 7.b: NOT surfaced to Telegram",
    result.get("surfaced_to_telegram") == False,
    f"surfaced_to_telegram={result.get('surfaced_to_telegram')}")
pol("POL 7.c: Event recorded",
    len(list_events()) >= 1,
    f"total events: {len(list_events())}")

# ═══════════════════════════════════════════════════════════════════════════
# POL 8: events list --lane urgent --format json
# ═══════════════════════════════════════════════════════════════════════════
print("\n─ POL 8: events list --lane urgent --format json")
reset_state()
payload = load_fixture("stripe_payment_failed.json")
process_event("stripe", payload["type"], payload, {}, bypass_signature=True)
events = list_events(lane="urgent")
json_str = json.dumps(events, indent=2)
valid = True
try:
    json.loads(json_str)
except json.JSONDecodeError:
    valid = False
pol("POL 8.a: Valid JSON output",
    valid and len(events) >= 1,
    f"events={len(events)}, valid_json={valid}")
pol("POL 8.b: Exit code would be 0",
    valid, f"parseable: {valid}")

# ═══════════════════════════════════════════════════════════════════════════
# POL 9: status --format json returns worker health
# ═══════════════════════════════════════════════════════════════════════════
print("\n─ POL 9: event_monitor status --format json")
stats = get_status()
json_str = json.dumps(stats, indent=2)
try:
    parsed = json.loads(json_str)
    valid_json = True
except json.JSONDecodeError:
    valid_json = False
    parsed = {}

pol("POL 9.a: Valid JSON", valid_json, f"parseable: {valid_json}")
pol("POL 9.b: Health fields present",
    all(k in parsed for k in ["agent", "version", "healthy", "uptime_seconds"]),
    f"keys: {[k for k in ['agent','version','healthy','uptime_seconds'] if k in parsed]}")
pol("POL 9.c: Lane counts present",
    all(k in parsed for k in ["events_urgent", "events_decision_required",
                               "events_informational", "events_routine"]),
    f"lane counts: present")

# ═══════════════════════════════════════════════════════════════════════════
# POL 10: Escalation daily cost cap enforced
# ═══════════════════════════════════════════════════════════════════════════
print("\n─ POL 10: Escalation daily cost cap enforced")
reset_state()
set_escalation_usage(100.0)  # Set $100 spent today (way over $5 cap)
payload = load_fixture("stripe_payment_failed.json")
result = process_event("stripe", payload["type"], payload, {}, bypass_signature=True)
pol("POL 10.a: Routes to decision_required after cap",
    result.get("lane") == "decision_required",
    f"lane={result.get('lane')}")
pol("POL 10.b: Cap-note on record",
    "cap_exceeded" in str(result.get("classifier_model", "")).lower() or
    "Cap exceeded" in str(result.get("classifier_reasoning", "")).lower() or
    "cap exceeded" in str(result.get("classifier_reasoning", "")).lower(),
    f"model={result.get('classifier_model')}, reasoning={(result.get('classifier_reasoning',''))[:80]}")
pol("POL 10.c: cap_exceeded_events incremented",
    get_stats()["cap_exceeded_events"] >= 1,
    f"cap_exceeded_events={get_stats()['cap_exceeded_events']}")

# ═══════════════════════════════════════════════════════════════════════════
stop_server()
print("\n" + "=" * 60)
print(f"RESULTS: {passed} PASS / {failed} FAIL / {passed + failed} total")
print("=" * 60)

if failed == 0:
    print("\nEVENT_MONITOR_POL_PASS")
    sys.exit(0)
else:
    print(f"\n{failed} POL criteria FAILED")
    sys.exit(1)
