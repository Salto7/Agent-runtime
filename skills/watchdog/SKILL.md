---
name: watchdog
description: Watchdog — periodic tick scripts in the Docker sandbox. One-shot script per interval; no while-True loops. Use for continuous scheduled checks.
allowed-tools: Bash Read
---

## Goal
Run a **saved tick script** on a fixed interval inside the sandbox. Prefer one check
per tick (no `while True` / long `time.sleep` inside the script). Obey RoE when under
a project.

## Setup
1. Write a tick script under `/workspace/cache/tick.py`.
2. Smoke it once: `bash("python3 /workspace/cache/tick.py")`.
3. Schedule repeats with `run_periodic(command=…, interval_seconds=N, duration_seconds=M)`.

See [tick reference](references/TICK.md) for paths and streaming notes.
