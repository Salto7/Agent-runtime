---
name: skill-writer
description: Draft a Claude Agent Skill (SKILL.md + a normal script) from an operator prompt. The pack must pass skills-ref so it can run in Claude and in the platform. Use when creating or scaffolding a new skill.
allowed-tools: Bash Read
---

## Goal
Produce a **skills-ref-clean** Claude Agent Skill under `workspace/learn/skill/<name>/`:

- SKILL.md — Agent Skills frontmatter (name, description, allowed-tools Bash and Read)
- a bash entry with a shebang that execs the required CLI
- a Run section that calls `run_skill_script`, not `bash scripts/run.sh`
- optional reference docs when the body links them

Do not emit native-only helpers or `metadata.jobable`. Role backstory already injects collaboration and memory tools.

**Collaboration & memory are runtime, not authored.** The runtime injects the A2A
(`send_message`/`check_inbox`/`go_idle`) and memory (`recall_memory`/`save_memory`)
contract through the role at run time, so a portable pack must **not** hardcode
those tool names. Describe reporting findings up / consuming upstream results in
plain prose only; the standard "Collaboration & memory" block belongs in
the platform-native packs, not skills-ref-clean ones.

## Scripts
- `scripts/run.sh` — authoring entry (argv = skill request)

## Lint requirements (must pass)
- Frontmatter name + description; directory name == name
- `allowed-tools` uses Claude names (`Bash`, `Read`, `Write`, `Edit`)
- Scripts are normal executables with a shebang
- Body file refs must exist

See [prompt](references/PROMPT.md) for the JSON contract.

## Run
```
run_skill_script("skill-writer", "scripts/run.sh", args='["Draft a skill that …"]')
```
