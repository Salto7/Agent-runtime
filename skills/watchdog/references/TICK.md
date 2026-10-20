# watchdog tick reference

## Paths
- Tick state / raw samples → `workspace/cache/` (or run dir)
- Alerts / curated hits → `findings/` (engagement discoveries with evidence;
  use `record_finding` only for those — not tick heartbeats)

## In-script
- `stream("log", …)` for UI
- Prefer **script** mode (no LLM per tick); `mode="agent"` only when reasoning is required each interval

```python
from orchestrator_tools import stream
stream("log", "tick ok")
```
