# fake_ardopc.py — Testing Guide (ARDOP Host/TNC Framing + Mesh Payload Simulation)

This document describes how to use `fake_ardopc.py` as a **local TCP test harness** for the ARDOP Host/TNC-style framing used by your `ardop_link.py` client, and how to simulate **loss, delay, jitter, and reordering** while you validate mesh/chat behavior.

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
  - apply **drop/delay/jitter/reorder** to outbound sends
  - accept **interactive injection** via stdin (hex payloads + a built-in `burst` helper)

## Protocol version note (Chat v2 “created timestamp”)

Your chat protocol v2 carries a **sender-generated creation timestamp** inside the chat message payload so messages can be ordered by *creation time* instead of *receive time*.

`fake_ardopc.py` imports `ChatMessage` / `encode_chat_message()` from your local `chat_protocol.py`.  
So: **make sure your checkout is running the v2 code** (the same version your mesh/chat nodes use).

If you see errors about missing fields, you likely have a version mismatch between `fake_ardopc.py` and `chat_protocol.py`.

## CLI options

Run:

```bash
python3 fake_ardopc.py --help
```

Common options:

- `--host` (default `127.0.0.1`)
- `--port` (default `8515`)
- `--echo` — echo received frames back to the *same* client
- `--broadcast` — broadcast received frames to *all* clients
- `--stdin-inject` — enable interactive injection via stdin

Impairments (applied to outbound sends):

- `--drop-rate FLOAT` — probability in `[0.0 .. 1.0]` to drop a frame
- `--delay-ms INT` — base delay (milliseconds) added to every send
- `--jitter-ms INT` — additional random delay (0..N ms) added per send
- `--reorder-rate FLOAT` — probability to add *extra* delay to induce reordering
- `--reorder-max-delay-ms INT` — maximum extra delay when reorder triggers

Fake OGM injector:

- `--fake-ogm` — periodically inject a fake OGM (mesh_node-compatible)
- `--fake-ogm-id CALLSIGN` — ASCII callsign-like ID (padded/truncated to 8 bytes)
- `--fake-ogm-interval FLOAT` — seconds between OGMs (default 5.0)
- `--fake-ogm-ttl INT` — TTL byte (0–255)
- `--fake-ogm-metric INT` — link metric byte (0–255)

## Stdin injector mode

Start with `--stdin-inject` and then type commands into the terminal.

### Inject an arbitrary payload (hex)

Paste a hex string (no `0x`, spaces allowed if your terminal preserves them). Example:

```text
01020304aabbccdd
```

`fake_ardopc.py` will unhex it, then send it to connected client(s) using the configured echo/broadcast behavior and impairments.

### Burst helper (chat message generator)

Command format:

```text
burst <origin> <dest> <channel> <count> <text...>
```

Example:

```text
burst NOCALL-1 KD9YQK-1 #general 10 test
```

This generates `<count>` chat messages with incrementing per-origin sequence numbers and text like:

- `test 1/10`
- `test 2/10`
- ...
- `test 10/10`

Useful for:
- verifying **dedupe**
- verifying **ordering**
- verifying **gap detection** (later)
- stressing the GUI append path

## Example run configurations

### 1) Basic “server up” (no echo, no broadcast)

Useful to verify your client connects and framing is correct.

```bash
python3 fake_ardopc.py
```

### 2) Echo loopback (single client)

Useful for verifying the *local stack* sees what it sends.

```bash
python3 fake_ardopc.py --echo
```

### 3) Broadcast hub (multi-client)

Run two mesh nodes/clients against the same fake server and have all payloads repeated.

```bash
python3 fake_ardopc.py --broadcast
```

### 4) Loss simulation (drop 20%)

```bash
python3 fake_ardopc.py --broadcast --drop-rate 0.20
```

### 5) Delay + jitter (RF-ish latency)

Adds 200 ms base delay plus up to 300 ms random jitter on every outbound send.

```bash
python3 fake_ardopc.py --broadcast --delay-ms 200 --jitter-ms 300
```

### 6) Reordering simulation (out-of-sequence delivery)

Adds an additional random delay to some frames to encourage out-of-order arrival.

```bash
python3 fake_ardopc.py --broadcast --reorder-rate 0.35 --reorder-max-delay-ms 1200
```

Tip: combine with jitter for more chaotic ordering:

```bash
python3 fake_ardopc.py --broadcast --jitter-ms 250 --reorder-rate 0.35 --reorder-max-delay-ms 1200
```

### 7) Fake OGMs (routing beacon testing)

Injects routing beacons periodically (mesh-node compatible OGM format), useful for GUI node list updates and routing-state churn testing.

```bash
python3 fake_ardopc.py --broadcast --fake-ogm --fake-ogm-id NOCALL-1 --fake-ogm-interval 5
```

### 8) Full chaos mode (loss + jitter + reorder + stdin injection)

```bash
python3 fake_ardopc.py \
  --broadcast \
  --drop-rate 0.10 \
  --delay-ms 100 \
  --jitter-ms 300 \
  --reorder-rate 0.25 \
  --reorder-max-delay-ms 1500 \
  --stdin-inject
```

Then, in the same terminal:

```text
burst NOCALL-1 KD9YQK-1 #general 10 test
burst KD9YQK-1 NOCALL-1 #general 10 reply
```

## Troubleshooting

### “Nothing happens”
- If you did not pass `--echo` or `--broadcast`, `fake_ardopc.py` may receive frames but not forward them anywhere.
- Confirm your client is connecting to `--host/--port`.

### Version mismatch errors
- Ensure `fake_ardopc.py` and your running mesh/chat code are importing the same `chat_protocol.py` version.
- If you recently upgraded the chat protocol (e.g., v2 timestamps), update both sides together.

### GUI ordering looks wrong
- With chat v2 created timestamps, ordering should track the sender’s creation time.
- If a sender clock is badly wrong, ordering and displayed time will reflect that (by design, for now).
