# AEB MQTT Bridge — Interface Spec v0.3

A standalone protocol spec for the **AndroidEdgeBridge (AEB) MQTT bridge**. Keep this next to your code when writing a SmartThings Edge driver against the bridge.

> **Since:** AEB 1.1.5 · **Spec version:** v0.3 · **Default port:** 8088 · **Required header:** `X-AEB-Api-Version: 1`

---

## 1. Role & scope

AEB's MQTT bridge is a **general-purpose, receive-only (SUBSCRIBER) bridge**.

- It connects to an external MQTT broker over **mTLS**, **subscribes** to topics, and **HTTP-forwards** each received message to a SmartThings Hub Edge driver on the same LAN.
- It is **vendor-agnostic** — it knows nothing about LG/ThinQ/AWS. You hand it a certificate, topics, and a clientId.

**Does:** connect & subscribe (mTLS); forward received messages untouched; own the long-lived connection, retry, and buffering an Edge driver cannot.

**Does NOT:** publish (one-way, receive-only); parse/transform payloads; sign or issue certificates (it only produces a CSR — an external CA signs it).

**Why it exists:** a SmartThings Edge driver runs in a hub Lua sandbox and cannot hold a long-lived MQTT connection to the internet. AEB holds it instead and pushes messages to the hub over LAN HTTP.

---

## 2. Architecture / data flow

```
MQTT Broker (AWS IoT / LG ThinQ / plain MQTTS, mTLS :8883)
   │  subscribe + receive (mTLS)
   ▼
AEB (Android)  — keeps subscription, queues (200-msg ring), at-least-once forward
   │  HTTP POST /aeb/ingest
   ▼
SmartThings Hub Edge Driver  — receive → de-dup by seq → emit capability events
```

- **Control plane** (create session, connect, register forward): driver → AEB over LAN HTTP (`/mqtt/*`).
- **Data plane** (messages): AEB → hub (`/aeb/ingest`). One way only. AEB never publishes.

---

## 3. Conventions

- Base URL: `http://<aeb-ip>:8088`
- Every request requires header `X-AEB-Api-Version: 1`.
- Request/response bodies are JSON (`Content-Type: application/json; charset=utf-8`).
- Error shape:

```json
{ "error": { "code": "SESSION_NOT_FOUND", "message": "human-readable detail" } }
```

---

## 4. REST API

| Method | Path | Role |
|--------|------|------|
| POST | `/mqtt/sessions` | Create session + RSA2048 keypair + PKCS#10 CSR (PEM) |
| POST | `/mqtt/sessions/{id}/connect` | mTLS connect to broker + subscribe |
| PUT | `/mqtt/sessions/{id}/forward` | Register forward target (idempotent) |
| GET | `/mqtt/sessions/{id}/status` | Read state + diagnostics |
| DELETE | `/mqtt/sessions/{id}` | Terminate session + erase keys/certs |
| GET | `/mqtt/sessions/{id}/messages?since=` | (optional) Catch-up polling of buffered messages |

### 4.1 POST /mqtt/sessions

Request body optional. The private key is generated and stored encrypted on-device and is **never returned** — only the CSR is.

```jsonc
// request (optional)
{ "keyType": "RSA2048", "subjectCN": "AWS IoT Certificate" }

// 201 response
{
  "sessionId": "sess_ab12cd34ef56",
  "csrPem": "-----BEGIN CERTIFICATE REQUEST-----\n...",
  "state": "CREATED"
}
```

### 4.2 POST /mqtt/sessions/{id}/connect

`qos` is `0` or `1` only (QoS 2 is unsupported and rejected). `topics` requires ≥1 entry; wildcards `+` and `#` are allowed. If `caPem` is omitted, the system trust store is used. If `clientId` is omitted, `aeb-<sessionId>` is used.

```jsonc
// request
{
  "certPem":      "-----BEGIN CERTIFICATE-----\n...",
  "caPem":        "-----BEGIN CERTIFICATE-----\n...",   // optional
  "endpoint":     "xxxx-ats.iot.ap-northeast-2.amazonaws.com",
  "port":         8883,
  "topics":       ["device/+/state", "device/status"],
  "qos":          1,
  "keepAliveSec": 60,
  "clientId":     "my-device-id"                         // optional; AWS IoT/ThinQ require a policy-matching value
}

// 200 response
{
  "sessionId": "sess_ab12cd34ef56",
  "state": "CONNECTING",
  "subscribedTopics": ["device/+/state", "device/status"]
}
```

### 4.3 PUT /mqtt/sessions/{id}/forward

Idempotent. If `hubAddress` is omitted, AEB derives it from the request source IP (= the hub). `path` defaults to `/aeb/ingest`.

```jsonc
// request
{ "hubPort": 12345, "path": "/aeb/ingest", "hubAddress": null }

// 200 response
{ "sessionId": "sess_ab12cd34ef56", "forwardTarget": "http://192.168.0.20:12345/aeb/ingest" }
```

### 4.4 GET /mqtt/sessions/{id}/status

All fields are always present (e.g. `pendingForwardCount: 0`, `lastError: null`), except `effectiveClientId`/`liveClientIdConnections` which appear once there is a live connection.

```jsonc
{
  "sessionId":              "sess_ab12cd34ef56",
  "state":                  "CONNECTED",
  "subscribedTopics":       ["device/+/state", "device/status"],
  "forwardTarget":          "http://192.168.0.20:12345/aeb/ingest",
  "pendingForwardCount":    0,
  "lastConnectedTs":        1717689874000,
  "lastForwardOkTs":        1717689900000,
  "lastError":              null,
  "effectiveClientId":      "my-device-id",   // actual CONNECT clientId (or fallback aeb-<id>)
  "liveClientIdConnections": 1                 // live sessions sharing this clientId; 1 = healthy, ≥2 = overlap
}
```

### 4.5 DELETE /mqtt/sessions/{id}

```jsonc
{ "sessionId": "sess_ab12cd34ef56", "deleted": true }
```

### 4.6 GET /mqtt/sessions/{id}/messages?since= (optional)

```jsonc
{ "messages": [ /* MessageObject[] (see §5) */ ], "cursor": "opaque-cursor" }
```

---

## 5. Message forward object

While `CONNECTED`, each received MQTT message is `POST`ed to the forward target as:

```jsonc
{
  "sessionId":       "sess_ab12cd34ef56",
  "seq":             1,
  "topic":           "device/abc/state",
  "payload":         "{\"power\":\"on\"}",
  "payloadEncoding": "utf8",          // "utf8" | "base64"
  "ts":              1717689900000
}
```

- **payload** — the raw MQTT payload. AEB never parses/transforms it. UTF-8-decodable → forwarded as-is (`utf8`); otherwise base64-encoded (`base64`).
- **seq** — per-session monotonic sequence (starts at 1, survives reconnects). Consumers use it as an idempotency / de-dup key.
- **Delivery** — at-least-once. A non-2xx response or timeout triggers exponential-backoff retry (up to 4 attempts, 500 ms → 4 s). After 4 failures the message is dropped and recorded in `lastError`.
- **Buffering** — when the forward target is unregistered or Wi-Fi is down, messages are held in a ring buffer (max 200) and FIFO-flushed on target registration / network recovery. Overflow drops the oldest (recoverable via `/messages`).

---

## 6. Session state machine

```
CREATED → CONNECTING → CONNECTED → DISCONNECTED | ERROR
```

| State | Meaning |
|-------|---------|
| `CREATED` | Session/key/CSR created, not yet connected. |
| `CONNECTING` | `connect` accepted; mTLS handshake + awaiting CONNACK. |
| `CONNECTED` | Subscribed; receiving and forwarding. |
| `DISCONNECTED` | Disconnected, retriable — the caller re-issues `connect`. |
| `ERROR` | Unrecoverable failure; cause in `lastError`. |

---

## 7. Security / mTLS

- **Private key never leaves the bridge** — RSA2048 keypair generated and stored encrypted on-device at session creation; in no API response. Drivers only ever receive a CSR.
- **CA pinning** — supply `caPem` to trust only that CA; omit to use the system trust store.
- **SNI required** — the mTLS SNI is derived from the broker hostname (`endpoint`). Without SNI, AWS IoT completes TLS but never sends CONNACK.
- **clientId policy** — AWS IoT/ThinQ require a clientId bound to the IoT policy; a mismatch is rejected or times out at CONNACK.

---

## 8. Reference driver

- **Edge driver:** [WooBooung/EdgeBridgeBaseDriver](https://github.com/WooBooung/EdgeBridgeBaseDriver) — MQTT flow in `src/mqtt_test.lua` (`run_flow` / `do_connect` / `register_forward` / `on_ingest`); ingest route `/aeb/ingest` in `src/setup_server.lua`. Fork it and replace the message-interpretation logic for your service.
