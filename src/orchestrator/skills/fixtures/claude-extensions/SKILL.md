---
name: claude-extensions
description: Fixture preserving Claude Code frontmatter fields
allowed-tools: Bash Read
when_to_use: When testing Claude extensions
argument-hint: "[target]"
arguments:
  - target
disable-model-invocation: false
user-invocable: true
model: sonnet
context: fork
agent: Explore
background: true
paths:
  - "**/*.py"
shell: bash
disallowed-tools: Write
compatibility: Python 3.10+
metadata:
  author: peon-fixture
---

Claude extensions body.
