# Container Restore State Snapshot
Generated: 2026-05-15

## What survives a `docker-compose` recreate
- `/workspace` — entire project source, scripts, knowledge (named volume)
- `/home/ubuntu/.claude` — Claude Code auth and config (named volume)

## What will be LOST (container writable layer)

| Directory | Size | Notes |
|-----------|------|-------|
| `~/esp-idf` | 2.1 GB | ESP-IDF v6.0.1 clone |
| `~/.espressif` | 8.3 GB | Xtensa/RISC-V toolchains, IDF Python envs |
| `~/.local` | 745 MB | pip user packages + binaries |
| `~/.vscode-server` | 1018 MB | VSCode server + extensions |
| `~/.oh-my-zsh` | ~50 MB | Oh-My-Zsh + powerlevel10k theme |
| `~/.p10k.zsh` | <1 MB | Powerlevel10k prompt config (**not restored — manual**) |
| `/usr/local/bin/init-firewall.sh` | <1 MB | Firewall helper (embedded in restore script) |
| **Total at risk** | **~12.2 GB** | |

## What the restore script covers

### Apt packages
All manually-installed packages reinstalled (pinned to current Ubuntu repo):
bison, build-essential, ca-certificates, ccache, cmake, curl, dnsutils, flex,
fzf, gh, git, git-delta, gnupg2, gperf, iproute2, ipset, iptables, jq, less,
libffi-dev, libssl-dev, libusb-1.0-0-dev, nano, ninja-build, nodejs,
openssh-client, poppler-utils, python3, python3-pip, python3-serial,
python3-venv, unzip, vim, wget, zsh, aggregate

### pip (user, --break-system-packages)
- esptool==5.2.0
- git-filter-repo==2.47.0
- psycopg2-binary==2.9.12
- python-dotenv==1.2.2
- SQLAlchemy==2.0.49

### npm global
- @anthropic-ai/claude-code@2.1.114

### ESP-IDF
- Version: v6.0.1 (confirmed active version)
- Target: esp32s3
- Install path: `~/esp-idf` + `~/.espressif`
- **Time estimate: ~1 hour** (clone + toolchain download)
- Note: Old idf5.4 Python env (`idf5.4_py3.13_env`) is NOT restored — was
  from the pre-migration period, no longer needed

### Shell / zsh
- Oh-My-Zsh installed unattended
- Powerlevel10k theme cloned
- `.zshrc` rewritten with all custom config (locale, PATH, fzf bindings,
  persistent history, p10k theme settings)
- Default shell set to zsh

### /usr/local/bin/init-firewall.sh
- Full script embedded as heredoc — restored verbatim

### Git config
- `user.name = development`
- `user.email = pmsfriend@googlemail.com`
- `core.autocrlf = input`
- `gh` credential helper for github.com and gist.github.com

## Manual steps required after restore

1. **`gh auth login`** — GitHub CLI authentication is not persisted
2. **Claude Code** — `~/.claude` survives, so this should auto-authenticate
3. **Powerlevel10k config** — `~/.p10k.zsh` is lost; run `p10k configure`
   to regenerate, or restore from backup
4. **VSCode** — reconnect to container; `.vscode-server` reinstalls extensions
   automatically (~1 GB download)
5. **Verify ESP-IDF** — `source ~/esp-idf/export.sh && idf.py --version`

## How to run
```bash
bash /workspace/container-restore.sh 2>&1 | tee /workspace/container-restore.log
```
