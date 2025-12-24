# Plugins and Event Hooks

This project supports an **optional, local-only plugin mechanism** for observing (and optionally reacting to) internal events **without changing the RF protocol or routing behavior**.

Plugins are **disabled by default**: if no plugins are present in `./plugins/`, nothing is loaded and the mesh runs normally.

---

## Where plugins live

Place plugin files in:

- `./plugins/`

Any file matching `*.py` will be considered a plugin **except**:

- files starting with `_` (example: `_notes.py`)
- files starting with `__` (example: `__init__.py`)

Plugins are loaded from file paths using `importlib`, so they do not need to be installed as packages.

---

## How plugins are loaded and run

Plugins are managed by `plugin_manager.py`:

- `PluginManager` scans the directory at startup
- events are delivered on a **background worker thread**
- plugin exceptions are **isolated** (caught and logged); plugins cannot crash the mesh
- event delivery is **non-blocking** for RF-critical paths:
  - events are queued
  - if the queue is full, the event is dropped (to protect mesh operation)

---

## Plugin interface

A plugin module can receive events in either of two ways:

### Option A: per-event function (preferred)

Define a function whose name matches the event name:

```python
def on_message_received(event: dict) -> None:
    ...
```

### Option B: catch-all handler

Define a handler that receives all events:

```python
def handle_event(name: str, event: dict) -> None:
    ...
```

If an event-specific function exists, it is used. Otherwise, `handle_event()` is used if present.

---

## Event payload format

When a plugin is called, it receives a dict with this structure:

```json
{
  "name": "<event name>",
  "ts": 1700000000.123,
  "data": { ... event-specific fields ... }
}
```

- `name` — the event name string
- `ts` — event timestamp (float seconds since epoch)
- `data` — a dict containing **only existing internal data** available at the hook point

---

## Events currently emitted

These events are currently produced by the system.

### `on_message_sent`

**When:** after the mesh successfully returns a `seqno` from sending application data for a chat message.

**data fields:**
- `dest_node_id_hex` — destination node id, hex string
- `channel` — channel string
- `nick` — local nickname used
- `text` — message text
- `origin_id_hex` — local node id, hex string
- `seqno` — local send seqno (int)
- `created_ts` — sender-created timestamp (int unix seconds)

---

### `on_message_received`

**When:** when an incoming application payload is decoded as a chat message and accepted for processing.

**data fields:**
- `origin_id_hex` — sender/origin node id, hex string
- `seqno` — received seqno (int)
- `channel` — channel string
- `nick` — sender nickname
- `text` — message text
- `created_ts` — sender-created timestamp (int unix seconds)

---

### `on_message_stored`

**When:** after a chat message is inserted into SQLite (INSERT OR IGNORE), **only if** the insert actually occurred (not a dedup IGNORE).

**data fields:**
- `origin_id_hex` — origin node id, hex string
- `seqno` — message seqno (int)
- `channel` — channel string
- `nick` — sender nickname
- `text` — message text
- `ts` — local insert time (float seconds since epoch)
- `created_ts` — sender-created timestamp (int unix seconds)

---

### `on_gap_detected`

**When:** when a **confirmed** gap report is produced by the existing gap detection logic (local-only).

**data fields:**
- `text` — the existing gap report line (string)

Notes:
- Only confirmed gaps trigger this event (the existing logic uses the text marker `" (confirmed)"`).

---

### `on_sync_applied`

**When:** when a SYNC_RESPONSE has been applied to the local DB.

**data fields:**
- `channel` — channel string
- `applied_count` — number of newly applied messages (int)

---

### `on_prune_executed`

**When:** after a prune operation deletes messages locally (manual prune or retention-driven prune).

**data fields (manual keep-last-n prune):**
- `mode` — `"keep_last_n_per_channel"`
- `deleted` — number of rows deleted (int)
- `keep_last_n` — parameter used (int)

**data fields (retention prune by age):**
- `mode` — `"older_than_seconds"`
- `deleted` — number of rows deleted (int)
- `days` — retention days configured (int)

---

### `on_link_state_change`

**When:** when a link transitions connected/disconnected, derived from existing per-link metrics snapshots.

**data fields:**
- `name` — link name/type label as reported by metrics
- `connected` — new connection state (bool)

Notes:
- This is derived from already-reported metrics; no link-layer protocol changes are involved.

---

## Safety and best practices for plugin authors

- Keep handlers fast; do not do long-running work inline.
- If you need slow work, spawn your own thread/process from the plugin.
- Be careful writing to disk on low-power devices; prefer buffered logging.
- Plugins should **never** call into routing/protocol internals directly.
- Treat `event["data"]` as untrusted input (it may contain missing keys in future versions).

---

## Minimal example plugin

Create `./plugins/logger.py`:

```python
import json
import time

def handle_event(name: str, event: dict) -> None:
    with open("./plugin_events.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": event.get("ts", time.time()), "name": name, "data": event.get("data", {})}) + "\n")
```

Start the GUI or daemon and check `./plugin_events.log`.

---

## Troubleshooting

- If plugins fail to load, errors are logged by the plugin manager.
- If no plugins exist, the plugin manager is a no-op (normal behavior).
- If a plugin throws, it is caught and logged; the mesh continues running.
