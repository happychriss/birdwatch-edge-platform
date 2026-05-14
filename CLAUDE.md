# CLAUDE.md

## Session Bootstrap

At the start of every session:
1. Read `/workspace/requirements.md` — project type, what is being built, current status. It links to the `skills/<type>-setup.md` that defines the working conventions for this project type.
2. Read all `.md` files in `/workspace/skills/` — active skills and conventions, including the setup file named in `requirements.md`
3. Read all `.md` files in `/workspace/knowledge/` — confirmed component and technology config

## File Ownership

- `CLAUDE.md` — for you: bootstrap and runtime context. This file.
- `requirements.md` — for you: what is being built. Start here for every task.
- `memory.md` — for you: live session memory. Keep it concise.
- `skills/` — for you: working conventions loaded each session.
- `knowledge/` — for you: confirmed config and integration notes.
- `external-docs/` — for you (read-only): raw reference material.
- `src/` — project source code.

## Runtime Environment

You are running inside a **Docker dev container** (Ubuntu, non-root user `ubuntu`):
- You have direct access to the filesystem and shell

