---
name: restore-container
description: Apply /workspace/container-restore.sh after a docker-compose recreate to reinstall apt packages, pip tools, ESP-IDF and any other container-local customizations captured by the prepare-rebuild skill.
---

# Restore Container After Rebuild

Triggered when the user says things like "restore the container", "after docker compose up", "rebuild restore", etc.

## Why this exists

After `docker-compose up` recreates the container, ~10GB of tooling under `/home/ubuntu` (esp-idf, .espressif, .local, .vscode-server) and all manually-installed apt packages are gone. The `prepare-rebuild` skill captured them in `/workspace/container-restore.sh`. This skill replays it.

## Steps

1. **Sanity checks** before doing anything:
   - Confirm `/workspace/container-restore.sh` exists. If missing, tell the user to run `/prepare-rebuild` first (but they obviously can't if the container was already rebuilt without snapshotting — in that case offer to reconstruct as best as possible from `git log`, `requirements.md`, and the project's needs).
   - Read `/workspace/container-restore.state.md` if present and summarize what's about to happen (apt count, pip count, ESP-IDF version, expected runtime).
   - Check what's actually missing — e.g. `command -v idf.py`, `[ -d ~/esp-idf ]`, etc. Skip steps that already succeeded if the user is re-running after a partial restore.

2. **Confirm with the user** before executing — especially the ESP-IDF install (it's 1-2 hours and ~10GB). Offer:
   - Run everything
   - Run only fast steps (apt + pip + npm), skip ESP-IDF
   - Run only ESP-IDF
   - Cancel

3. **Execute** the chosen subset of `/workspace/container-restore.sh`:
   - Stream output so the user sees progress
   - On any failure, stop and report exactly which step failed and the error
   - For ESP-IDF, run in foreground (don't background — the user may want to interrupt)

4. **Post-restore verification**:
   - `apt list --installed 2>/dev/null | wc -l` (rough count sanity)
   - `idf.py --version` if ESP-IDF was restored
   - `pip3 list --user` shows expected packages
   - Open a fresh shell or `source ~/.zshrc` to pick up PATH changes
   - Note any manual steps still required (e.g. `gh auth login`, `claude` re-auth, secrets that aren't in the script)

5. **Report**: brief summary of what was restored, what was skipped, and what manual steps remain.

## Output expectations

- Don't blindly re-run if things already work — check first
- Always confirm before the long-running ESP-IDF install
- If the script is stale (older than the last `prepare-rebuild` run, or references things that no longer exist), warn the user and offer to regenerate
