# chatlogs_export.py — README

`chatlogs_export.py` exports **one file per channel/DM** from the mesh chat SQLite database.

It is designed to be used in two ways:

1. **Imported as a module** (for GUI integration or automation)
2. **Run as a standalone script** (CLI)

Exports are written into a timestamped folder under a base directory:

```
../logs/<UTC_YYYYmmdd-HHMMSS>/
```

Each channel/DM becomes its own file (CSV or TXT) inside that folder.

---

## Output format

### CSV (default / recommended)
One row per message with the following columns:

- `created_ts_iso_utc` — ISO 8601 UTC timestamp (human friendly)
- `created_ts_unix` — Unix seconds (numeric)
- `nick` — sender nick/callsign
- `channel` — channel name (e.g. `#general`) or DM key (e.g. `@KD9YQK-1`)
- `text` — message text

### TXT
A readable transcript-style export. The exact line format is implemented in the exporter and may be tuned over time.

---

## CLI usage

From your project folder:

### Export using config.yaml (recommended)

```bash
python3 chatlogs_export.py --config config.yaml
```

### Choose format (csv or txt)

```bash
python3 chatlogs_export.py --config config.yaml --format csv
python3 chatlogs_export.py --config config.yaml --format txt
```

### Change base output directory

```bash
python3 chatlogs_export.py --config config.yaml --out-base ../logs
```

### Export directly from a DB path (bypass config.yaml)

```bash
python3 chatlogs_export.py --db /path/to/chat.db --out-base ../logs --format csv
```

---

## Module usage (import)

Example:

```python
from chatlogs_export import export_all_channels

export_dir = export_all_channels(
    db_path="path/to/chat.db",
    out_base_dir="../logs",
    fmt="csv",
)
print("Exported to:", export_dir)
```

This is how `chatlogs_gui.py` uses the exporter.

---

## File naming rules

Channel names are sanitized into safe filenames.

Common examples:
- `#general` → `general.csv`
- `@KD9YQK-1` → `DM_KD9YQK-1.csv`

---

## Notes / guarantees

- Export uses **only SQLite DB data** (no UI transcript buffers).
- Message ordering is based on **created timestamp** (the sender-generated timestamp carried in chat protocol v2).
- If a sender’s clock is wrong, the exported created times reflect that (by design).

---

## Troubleshooting

### No files exported
- Confirm your DB actually contains messages.
- Confirm your `config.yaml` points at the correct DB path (`chat.db_path`), or pass `--db` explicitly.

### Permission errors
- Ensure the process has permission to create the `../logs/<timestamp>/` folder.
- Try setting `--out-base` to a writable directory.

### Version mismatch / missing columns
If you upgraded the database schema recently (e.g., added `created_ts`), run your main app once to allow schema migration to complete before exporting.
