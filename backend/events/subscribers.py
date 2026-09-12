"""
Default subscribers wired up at app startup. Each function here reacts
to an agent-completion event without the orchestrator or the agent
itself needing to know this code exists — that's the whole point of
routing through the bus instead of a direct function call.

Currently just a print-based audit log (visible in server logs / Render
console). Swap the body of `_audit_log` for a DB insert or external
logging service without touching app.py or the orchestrator.
"""
import datetime


def _audit_log(event_name, payload):
    ts = datetime.datetime.now().isoformat(timespec='seconds')
    status = 'ok' if payload.get('success', True) else 'FAILED'
    print(f"[{ts}] event={event_name} status={status}")


def register_default_subscribers(event_bus):
    event_bus.subscribe_all(_audit_log)
    return event_bus
