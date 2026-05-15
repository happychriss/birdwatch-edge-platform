---
name: prepare-rebuild
description: Snapshot every container-local customization (apt, pip, npm, ESP-IDF, custom files in /home/ubuntu) and write a restore script to /workspace/container-restore.sh so the container can be safely recreated by docker-compose without losing setup.
---

# Prepare for Container Rebuild

Triggered when the user says things like "prepare for rebuild", "snapshot the container", "before docker compose recreate", etc.

## Why this exists

Only `/workspace` and `/home/ubuntu/.claude` survive a container recreate. Everything else under `/home/ubuntu` (esp-idf ~2GB, .espressif ~8GB, .local, .vscode-server) and any apt-installed packages are in the container's writable layer and will be **lost** on recreate. This skill captures the state needed to rebuild it.

## Steps

1. **Run the inventory** — gather everything that lives outside named volumes:
   - `apt-mark showmanual` — manually-installed apt packages
   - `pip3 list --not-required --format=freeze` — top-level pip packages (system / --user)
   - `npm list -g --depth=0 --json` — global npm packages
   - `ls /home/ubuntu/.local/bin/` — user-installed binaries
   - `ls /home/ubuntu/esp-idf/.git/refs/tags/ 2>/dev/null | tail -5` and check `git -C /home/ubuntu/esp-idf describe --tags` for ESP-IDF version
   - `ls /home/ubuntu/.espressif/python_env/` — IDF Python env names tell you which IDF versions were installed
   - `ls /usr/local/bin/` (custom) — anything not from a base package; read and embed each custom file as a heredoc in the restore script so it is fully self-contained
   - Read `/home/ubuntu/.zshrc` and `/home/ubuntu/.bashrc` — capture custom PATH / source lines
   - Check `~/.gitconfig` for important user.name/user.email values
   - Check for `~/.p10k.zsh` — it's lost on rebuild and cannot be auto-restored; flag it as a manual step in the final notes
   - List any venvs under `/workspace` (those are safe — note them but don't include in restore)

2. **Diff against base image (best effort)** — try `docker inspect $(hostname) 2>/dev/null` to find the image; if accessible, compare the apt list against what the image ships. If not accessible (typical from inside the container), include the full manual-apt list.

3. **Write the restore script** to `/workspace/container-restore.sh`:
   - Make it idempotent (use `apt-get install -y` which is safe to re-run, check `[ -d ~/esp-idf ] ||` before cloning, etc.)
   - Group steps with clear `echo "=== … ==="` banners
   - Pin versions for pip/ESP-IDF (use what's currently installed)
   - Include zsh PATH/source lines from `.zshrc` that aren't already in the base image
   - End with a final note line telling the user what manual steps remain (e.g. authenticate `gh`, re-login `claude`)
   - `chmod +x` it

4. **Write a state snapshot** alongside the script: `/workspace/container-restore.state.md` — a human-readable checklist showing what was captured, sizes of dirs that will be lost, and any caveats (e.g. "ESP-IDF reinstall takes ~1 hour").

5. **Report to the user**: confirm the script is written, mention total size of data at risk, and remind them that running `/restore-container` after recreate will replay the script.

## Output expectations

- Script must be runnable end-to-end without prompts (use `-y` flags, accept defaults)
- Pin every version where a version was detected
- Don't include anything from named volumes (`/workspace`, `/home/ubuntu/.claude`) — those survive
- Keep the script readable; it's also documentation of what's installed
