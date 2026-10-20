---
name: hello-world
description: Write a greeting file in the workspace and print it.
allowed-tools: bash Read
metadata:
  category: examples
  tags: [demo, workspace]
  requires_clis: [python3]
  jobable: true
---

# Hello World

Run `python3 /skills/hello-world/scripts/run.py` (or write the file with `bash`).

When invoked:
1. Write `/workspace/hello.txt` containing a short greeting and the current UTC time.
2. Print the file contents to stdout.
3. Stop.
