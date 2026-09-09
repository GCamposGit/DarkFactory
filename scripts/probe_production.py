import httpx
import json

base = "https://darkhub.ggcampos.com"

print("=" * 60)
print(f"PROBING LIVE PRODUCTION: {base}")
print("=" * 60)

# 1. Cloud Status
try:
    r = httpx.get(f"{base}/api/cloud/status", timeout=10.0)
    print("\n1. /api/cloud/status:")
    print(f"   HTTP {r.status_code}")
    print(f"   Response: {json.dumps(r.json(), indent=2)}")
except Exception as e:
    print(f"   ERROR: {e}")

# 2. Simulate Webhook
try:
    r = httpx.post(
        f"{base}/api/webhooks/test",
        json={"event_type": "ping", "payload": {"zen": "Dark Factory 24/7 is fully online in the cloud."}},
        timeout=10.0
    )
    print("\n2. /api/webhooks/test (POST simulation):")
    print(f"   HTTP {r.status_code}")
    print(f"   Response: {json.dumps(r.json(), indent=2)}")
except Exception as e:
    print(f"   ERROR: {e}")

# 3. Signed HMAC GitHub Webhook test
try:
    import hmac, hashlib
    secret = "darkfac_gh_webhook_secret_change_me"
    payload = {
        "ref": "refs/heads/main",
        "repository": {"full_name": "GCamposGit/DarkFactory"},
        "head_commit": {"id": "1f931fc", "message": "feat(prod): live verification probe"}
    }
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    sig = "sha256=" + hmac.new(secret.encode('utf-8'), raw, hashlib.sha256).hexdigest()
    
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "live-delivery-probe-001",
        "X-Hub-Signature-256": sig
    }
    r = httpx.post(f"{base}/api/webhooks/github", content=raw, headers=headers, timeout=10.0)
    print("\n3. /api/webhooks/github (Signed push event):")
    print(f"   HTTP {r.status_code}")
    print(f"   Response: {json.dumps(r.json(), indent=2)}")
except Exception as e:
    print(f"   ERROR: {e}")

# 4. List Webhook Events
try:
    r = httpx.get(f"{base}/api/webhooks/events", timeout=10.0)
    print("\n4. /api/webhooks/events (GET audit trail):")
    print(f"   HTTP {r.status_code}")
    events = r.json()
    print(f"   Events count: {len(events)}")
    for ev in events:
        print(f"   - [{ev.get('timestamp')}] {ev.get('event_type')} (id: {ev.get('delivery_id')}) -> status: {ev.get('status')}")
except Exception as e:
    print(f"   ERROR: {e}")

# 4. Status again (check metrics counter increment)
try:
    r = httpx.get(f"{base}/api/cloud/status", timeout=10.0)
    status_data = r.json()
    print("\n4. /api/cloud/status (after webhook event):")
    print(f"   total_events_received: {status_data.get('total_events_received')}")
    print(f"   last_event_type: {status_data.get('last_event_type')}")
    print(f"   last_event_at: {status_data.get('last_event_at')}")
    print(f"   uptime_seconds: {status_data.get('uptime_seconds')}")
except Exception as e:
    print(f"   ERROR: {e}")

# 5. Frontend Root (UI)
try:
    r = httpx.get(f"{base}/", timeout=10.0)
    print("\n5. Frontend Root (/):")
    print(f"   HTTP {r.status_code}")
    print(f"   Content-Type: {r.headers.get('content-type')}")
    print(f"   Body snippet: {r.text[:150]}...")
except Exception as e:
    print(f"   ERROR: {e}")

# 6. Static assets (UI JS & CSS)
try:
    r = httpx.get(f"{base}/static/infra.js", timeout=10.0)
    print("\n6. Static Asset (/static/infra.js):")
    print(f"   HTTP {r.status_code}")
    print(f"   Contains CloudGateway: {'CloudGateway' in r.text}")
except Exception as e:
    print(f"   ERROR: {e}")

print("\n" + "=" * 60)
print("PRODUCTION VERIFICATION COMPLETE!")
print("=" * 60)
