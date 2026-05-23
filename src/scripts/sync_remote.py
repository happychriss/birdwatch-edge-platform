#!/usr/bin/env python3
"""
sync_remote.py — Download a remote directory tree via rsync over SSH.

Usage:
    python3 sync_remote.py [--config path/to/sync_remote.ini] [--dry-run]

Config file defaults to sync_remote.ini next to this script.
SSH host can be any alias defined in ~/.ssh/config or a plain hostname/IP.
"""

import argparse
import configparser
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).parent / "sync_remote.ini"


def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not path.exists():
        sys.exit(f"Config file not found: {path}")
    cfg.read(path)
    return cfg


def build_rsync_cmd(host: str, source_dir: str, target_dir: str,
                    skip_existing: bool, verbose: bool, dry_run: bool) -> list[str]:
    # Ensure source ends with / so rsync copies the *contents*, not the folder itself.
    # Remove trailing slash first, then re-add to be safe.
    remote = f"{host}:{source_dir.rstrip('/')}/"
    local = str(Path(target_dir).expanduser().resolve()) + "/"

    cmd = [
        "rsync",
        "--archive",          # preserves permissions, timestamps, symlinks, etc.
        "--compress",         # compress during transfer
        "--human-readable",   # human-readable sizes
        "-e", "ssh",          # force SSH transport (honours ~/.ssh/config)
    ]

    if skip_existing:
        cmd.append("--ignore-existing")

    if verbose:
        cmd.append("--verbose")
        cmd.append("--progress")

    if dry_run:
        cmd.append("--dry-run")

    cmd += [remote, local]
    return cmd


def main():
    parser = argparse.ArgumentParser(description="Sync a remote folder to local via rsync/SSH.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"Path to config file (default: {DEFAULT_CONFIG})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be transferred without doing it")
    args = parser.parse_args()

    cfg = load_config(args.config)

    host       = cfg.get("remote", "host").strip()
    source_dir = cfg.get("remote", "source_dir").strip()
    target_dir = cfg.get("local",  "target_dir").strip()

    skip_existing = cfg.getboolean("options", "skip_existing", fallback=True)
    verbose       = cfg.getboolean("options", "verbose",       fallback=True)
    dry_run       = cfg.getboolean("options", "dry_run",       fallback=False) or args.dry_run

    target_path = Path(target_dir).expanduser().resolve()
    target_path.mkdir(parents=True, exist_ok=True)

    cmd = build_rsync_cmd(host, source_dir, str(target_path), skip_existing, verbose, dry_run)

    if dry_run:
        print("[DRY RUN] Would execute:")
    else:
        print("Syncing:")
    print(f"  {host}:{source_dir}  →  {target_path}\n")
    print("rsync command:", " ".join(cmd), "\n")

    result = subprocess.run(cmd)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
