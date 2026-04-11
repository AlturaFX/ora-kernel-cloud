"""Diagnose the stuck sub-session directly via API."""
import time
from anthropic import Anthropic
from orchestrator.config import load_config, get_api_key

SUB_SESSION_ID = "sesn_011CZwEgCWQMbqBEtfT9adku"

client = Anthropic(api_key=get_api_key(load_config()))

# Check status
try:
    sess = client.beta.sessions.retrieve(SUB_SESSION_ID)
    print(f"Session status: {sess.status}")
    print(f"Session id: {sess.id}")
except Exception as exc:
    print(f"Retrieve failed: {exc}")

# Try streaming with a hard timeout
print("\nAttempting to stream events (max 10s)...")
t0 = time.time()
event_count = 0
try:
    with client.beta.sessions.events.stream(SUB_SESSION_ID) as stream:
        for event in stream:
            event_count += 1
            et = getattr(event, "type", "?")
            print(f"  [{time.time()-t0:.2f}s] {et}")
            if et == "agent.message":
                for block in getattr(event, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        print(f"    text[:200]: {text[:200]!r}")
            if time.time() - t0 > 10:
                print("  (10s elapsed, breaking)")
                break
            if et in ("session.status_idle", "session.status_terminated"):
                print("  (session finalized)")
                break
except Exception as exc:
    print(f"Stream error: {type(exc).__name__}: {exc}")

print(f"\nTotal events seen: {event_count}")
