# fake_ardopc.py — Testing Guide (ARDOP Host/TNC Framing + Mesh Payload Simulation)

This document describes how to use `fake_ardopc.py` as a **local TCP test harness** for the ARDOP Host/TNC-style framing used by your `ardop_link.py` client, and how to simulate **loss, delay, jitter, reordering, and targeted sync behavior** while validating mesh/chat functionality.

## What fake_ardopc does

- Listens on TCP (default `127.0.0.1:8515`).
- Accepts one or more clients.
- Frames every outbound payload as:

```
[u16be_length][payload_bytes...]
```

- Can:
  - **echo** payloads back to the sender
  - **broadcast** payloads to all connected clients
  - inject **fake OGMs** (routing beacons)
  - apply **drop / delay / jitter / reorder** to outbound sends
  - accept **interactive injection** via stdin
  - generate **chat bursts** for gap and ordering tests
  - generate and observe **targeted sync (range-based) requests**

## Protocol notes (Chat v2 + targeted sync extension)

The mesh chat protocol uses stable message identity:

```
(origin_id, seqno)
```

and includes a sender-generated UTC creation timestamp.

Targeted sync is implemented as an **extension of the existing SYNC request**, using:

```
mode: "range"
```

No protocol version bump is required. Range-based sync requests and responses use the same framing and message types as existing inventory-based sync.

`fake_ardopc.py` imports protocol helpers from your local `chat_protocol.py`.  
Ensure all components (mesh nodes + test harness) are updated together.

## CLI options

Run:

```bash
python3 fake_ardopc.py --help
```

Common options:

- `--host` (default `127.0.0.1`)
- `--port` (default `8515`)
- `--echo` — echo received frames back to the same client
- `--broadcast` — broadcast received frames to all clients
- `--stdin-inject` — enable interactive injection via stdin

Impairments (applied to outbound sends):

- `--drop-rate FLOAT`
- `--delay-ms INT`
- `--jitter-ms INT`
- `--reorder-rate FLOAT`
- `--reorder-max-delay-ms INT`

Fake OGM injector:

- `--fake-ogm`
- `--fake-ogm-id CALLSIGN`
- `--fake-ogm-interval FLOAT`
- `--fake-ogm-ttl INT`
- `--fake-ogm-metric INT`

## Stdin injector mode

Start with `--stdin-inject` and type commands into the terminal.

### Inject arbitrary payloads (hex)

Paste a hex string (spaces allowed). Example:

```
01020304aabbccdd
```

### Burst helper (chat message generator)

```
burst <origin> <dest> <channel> <count> <text...>
```

Example:

```
burst NOCALL-1 KD9YQK-1 #general 10 test
```

Generates sequential messages useful for:
- dedupe verification
- ordering tests
- gap creation
- sync recovery testing

### Targeted sync injection (range requests)

```
sync_range <origin> <dest> <channel> <target_origin> <start> <end>
```

Example:

```
sync_range KD9YQK-1 NOCALL-1 #general KD9YQK-1 142 147
```

This sends a real `CHAT_TYPE_SYNC_REQUEST` with:

```json
{
  "mode": "range",
  "origin_id_hex": "...",
  "start": 142,
  "end": 147
}
```

Useful for:
- verifying range-based sync encoding
- observing responder behavior
- validating coalescing and rate limiting

## Example run configurations

### Basic server

```bash
python3 fake_ardopc.py
```

### Echo loopback

```bash
python3 fake_ardopc.py --echo
```

### Broadcast hub

```bash
python3 fake_ardopc.py --broadcast
```

### Loss simulation

```bash
python3 fake_ardopc.py --broadcast --drop-rate 0.20
```

### Delay + jitter

```bash
python3 fake_ardopc.py --broadcast --delay-ms 200 --jitter-ms 300
```

### Reordering simulation

```bash
python3 fake_ardopc.py --broadcast --reorder-rate 0.35 --reorder-max-delay-ms 1200
```

### Fake OGMs

```bash
python3 fake_ardopc.py --broadcast --fake-ogm --fake-ogm-id NOCALL-1 --fake-ogm-interval 5
```

### Full chaos mode

```bash
python3 fake_ardopc.py   --broadcast   --drop-rate 0.10   --delay-ms 100   --jitter-ms 300   --reorder-rate 0.25   --reorder-max-delay-ms 1500   --stdin-inject
```

## Troubleshooting

- No output: ensure `--echo` or `--broadcast` is enabled.
- Sync not triggered: confirm gaps are **confirmed**, not suspected.
- Unexpected ordering: ordering follows sender UTC creation timestamps.
