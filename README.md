# ARDOP Mesh Chat

A Python-based RF mesh chat system designed for real-world HF and VHF operation using ARDOP, with optional TCP mesh links for testing, bridging, or backbone connectivity.

This project is already functional. It prioritizes correctness, RF constraints, and operational realism over experimental features or speculative designs.

---

## Quick Start (Read This First)

### Requirements
- Python 3.10+
- An ARDOP TNC or modem exposing a TCP interface (or `fake_ardopc.py` for testing)
- For GUI use: `wxPython`
- Optional: `cryptography` (only if encryption is explicitly enabled)

Install Python dependencies:
```bash
pip install pyyaml wxPython cryptography
```

### Minimal Startup (Local Test)

1. Start a local ARDOP test harness:
```bash
python fake_ardopc.py --broadcast
```

2. Edit `config.yaml`:
- Set your `mesh.callsign`
- Configure at least one chat peer
- Leave encryption disabled (default)

3. Start the GUI:
```bash
python GUI.py
```

Or start a headless node:
```bash
python chat_daemon.py --config config.yaml
```

At this point, nodes will discover each other, exchange OGMs, and sync chat state.

---

## What This Project Is

This is a **hybrid RF mesh chat system** built for unreliable, low-bandwidth radio links.

It combines:
- **Broadcast-based mesh discovery and routing** (OGMs)
- **Hop-by-hop data forwarding**
- **Application-layer reliability** (sync, retries, backoff)
- **Multiple interchangeable link layers**

The result is a system that behaves sensibly on HF/VHF while remaining debuggable, testable, and extensible.

This is **not**:
- A generic IP mesh
- A flooding chat toy
- A future roadmap or research prototype

Everything described here exists in the code.

---

## High-Level Architecture

```
+--------------------+
| GUI / Daemon       |
|  (GUI.py,          |
|   chat_daemon.py)  |
+----------+---------+
           |
           v
+--------------------+
| MeshChatBackend    |
| (mesh_backend.py)  |
+----------+---------+
           |
           v
+--------------------+
| MeshChatClient     |
| (chat_client.py)   |
+----------+---------+
           |
           v
+--------------------+
| MeshNode           |
| (mesh_node.py)     |
+----------+---------+
           |
           v
+--------------------+
| Link Clients       |
| ARDOP / TCP        |
+--------------------+
```

Each layer has a single responsibility and clean boundaries.

---

## The Hybrid Mesh Model

### Discovery and Routing
- Nodes periodically broadcast **OGMs (Originator Messages)**
- OGMs establish neighbors and best-next-hop routes
- Routing is BATMAN-lite style
- TTL limits propagation

### Data Transport
- Chat messages are sent as mesh DATA frames
- Frames are forwarded hop-by-hop
- Duplicate suppression uses full `(origin_id, seqno)` packet identity
- Compression is automatic when beneficial

### Reliability (Application Layer)
- Mesh does not guarantee delivery
- The chat layer detects gaps
- Sync requests repair missing messages
- Retries use bounded exponential backoff
- Targeted range-based sync is supported

This separation keeps the mesh simple and RF-friendly.

---

## Supported Link Layers

### ARDOP (Primary RF Transport)
- Implemented in `ardop_link.py`
- TCP connection to an ARDOP TNC/modem
- Raw mesh frames (no AX.25, no KISS)
- Automatic reconnect with backoff

### TCP Mesh Links (Optional)
- Implemented in `tcp_link.py`
- Client and server modes
- Password-authenticated handshake
- Uses identical framing to ARDOP
- Intended for:
  - LAN/WAN backbones
  - RF-to-RF bridging
  - Local testing

### Multi-Link Operation
- `MultiplexLinkClient` fans out transmissions
- MeshNode remains unaware of link count
- RX frames from all links feed the same mesh

No protocol changes are introduced by TCP links.

---

## Chat Layer

- SQLite-backed persistence (`chat_store.py`)
- Stable message identity `(origin_id, seqno)`
- Supports:
  - Channels (e.g. `#general`)
  - Direct messages (`@CALLSIGN`)
- Gap detection with confirmed vs suspected gaps
- Sync modes:
  - Last-N inventory sync
  - Targeted range-based sync

Ordering follows sender timestamps, not arrival order.

---

## GUI and Daemon Usage

### GUI Node
- Entry point: `GUI.py`
- wxPython-based
- Shows:
  - Discovered nodes
  - Channels and DMs
  - Live status
- Includes tools for:
  - Config editing
  - DB pruning
  - Chat log export

### Headless / Daemon Node
- Entry point: `chat_daemon.py`
- No UI, no input
- Acts as:
  - Normal mesh participant
  - Relay / repeater
  - Bridge node

See: `chat_daemon.md`

---

## Configuration Overview

All configuration lives in a single YAML file.

Key sections:
- `mesh`: callsign, routing, security
- `ardop`: ARDOP TCP connection
- `tcp_mesh`: optional TCP server/links
- `chat`: database, peers, sync policy
- `gui`: optional theme and font settings

The GUI reads identity and theme only; backend behavior requires restart.

See: `config.yaml` and `config_loader.py`

---

## Testing and Simulation

A full ARDOP Host/TNC-compatible test harness is included:

- `fake_ardopc.py`
- Simulates:
  - Loss
  - Delay
  - Jitter
  - Reordering
  - OGMs
  - Sync traffic

See: `fake_ardopc.md`

---

## Log Export

Chat history can be exported per channel/DM.

- CLI and GUI supported
- Files grouped by timestamped folders

See: `chatlogs_export.md`

---

## Recent Feature Additions

The following features are **implemented and stable**. They improve observability and operational flexibility **without altering mesh protocol behavior**.

### Feature #1 — Per-Link Metrics

Each active link (ARDOP, TCP mesh, etc.) maintains independent health and performance metrics:

- Bytes transmitted / received
- Packet counts
- Duplicate suppression statistics
- Recent activity timestamps
- Link uptime

Metrics are tracked **per link**, not per node, allowing accurate diagnosis of asymmetric paths, RF degradation, or failing backbone links.

These metrics:
- Do **not** affect routing decisions
- Do **not** alter protocol behavior
- Are strictly observational

They are surfaced through backend status events and structured diagnostics output.

---

### Feature #2 — Structured Diagnostics Output

The backend emits **structured, machine-parseable diagnostics snapshots**.

Characteristics:
- Stable output format intended for logging and tooling
- Includes:
  - Node identity
  - Discovered peers
  - Routing state
  - Per-link metrics
  - Chat-layer state (queues, DB counts, sync status)
- Emitted periodically and on key state transitions

This enables:
- Headless monitoring
- External dashboards
- Offline log analysis
- Debugging without a GUI

Diagnostics output is **read-only** and has no effect on mesh behavior.

---

### Feature #3 — Role-Based Node Modes

Nodes can explicitly declare a **role** that controls how they participate in the mesh using existing mechanisms.

Configured via:
- `chat.node_mode` in `config.yaml`, or
- `--mode` when running `chat_daemon.py`

Supported modes:

#### `full` (default)
- Normal chat participant
- Can originate chat messages
- Stores chat history in SQLite
- Fully participates in sync (gap detection, retries, targeted sync)

This is the existing behavior and remains the default.

#### `relay`
- Mesh repeater / router
- Participates in OGMs, routing, and forwarding
- Does **not** originate chat messages
- Does **not** store chat history
- Does **not** participate in chat sync

Intended for RF repeaters, bridge nodes, and infrastructure roles.

#### `monitor`
- Passive diagnostics / observation node
- Participates in mesh discovery and forwarding
- Emits diagnostics and link metrics
- Does **not** originate chat
- Does **not** store chat history
- Does **not** participate in sync

Useful for monitoring stations and network observation.

**Important:**  
Role-based modes introduce **no protocol changes**, **no routing changes**, and **no implicit behavior**.  
If a mode is not explicitly set, node behavior is unchanged.

---


---

## Design Principles

- RF-first, not IP-first
- Conservative defaults
- No silent magic
- No speculative features
- Explicit failure handling
- Everything restart-safe

If it’s described here, it’s implemented.

---

## License / Usage

This project is intended for experimentation, learning, and amateur radio use.

Encryption is disabled by default and must remain disabled for on-air amateur operation.

---

