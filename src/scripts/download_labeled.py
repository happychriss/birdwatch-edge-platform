#!/usr/bin/env python3
"""download_labeled.py — Pull labeled BirdWatch frames from the server.

The server exposes /admin/export/<label> (JSON list) and serves images at
/static/<filename>.  This script fetches the list, downloads each file, and
marks it as downloaded via POST /frame/<id>/downloaded.

Usage:
    python download_labeled.py --root ~/birdwatch-data
    python download_labeled.py --root ~/birdwatch-data --label bird
    python download_labeled.py --root ~/birdwatch-data --server http://192.168.1.110:8000
    python download_labeled.py --root ~/birdwatch-data --include-downloaded

Folder structure created under ROOT:
    all_images/
    bird_images/
    ignore_images/
    special_images/

Options:
    --server URL            Server base URL (default: http://192.168.1.110:8000)
    --root DIR              Local root folder (required)
    --label LABEL           Only download this label: bird, ignore, special
                            (default: download all three)
    --include-downloaded    Re-download files already marked on server
    --no-mark               Do not mark frames as downloaded on server
    --dry-run               Print what would be downloaded without saving files
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests


DEFAULT_SERVER = 'http://192.168.1.110:8000'
ALL_LABELS = ['bird', 'ignore', 'special']


def fetch_list(server: str, label: str, include_downloaded: bool) -> list[dict]:
    url = f'{server}/admin/export/{label}'
    params = {}
    if include_downloaded:
        params['include_downloaded'] = 'true'
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get('frames', [])


def download_file(server: str, filename: str, dest: Path) -> bool:
    """Download a single JPEG from /static/<filename> to dest. Returns True on success."""
    url = f'{server}/static/{filename}'
    resp = requests.get(url, timeout=60, stream=True)
    if resp.status_code != 200:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
    return True


def mark_downloaded(server: str, frame_id: int) -> None:
    url = f'{server}/frame/{frame_id}/downloaded'
    requests.post(url, timeout=10)


def run(server: str, root: Path, labels: list[str], include_downloaded: bool,
        no_mark: bool, dry_run: bool) -> None:

    all_dir = root / 'all_images'
    stats = {'downloaded': 0, 'skipped': 0, 'failed': 0}

    for label in labels:
        frames = fetch_list(server, label, include_downloaded)
        label_dir = root / f'{label}_images'
        print(f'\n[{label}] {len(frames)} frame(s) pending')

        for frame in frames:
            filename = frame['filename']
            fid      = frame['id']
            ts       = (frame.get('captured_at') or '')[:16]

            label_dest = label_dir / filename
            all_dest   = all_dir / filename

            already_local = label_dest.exists()
            if already_local and not include_downloaded:
                print(f'  SKIP  {filename}  {ts}  (already on disk)')
                stats['skipped'] += 1
                continue

            print(f'  {"DRY " if dry_run else "GET "}  {filename}  {ts}  label={label}')

            if dry_run:
                stats['downloaded'] += 1
                continue

            ok = download_file(server, filename, label_dest)
            if not ok:
                print(f'         !! download failed')
                stats['failed'] += 1
                continue

            # Hard-link to all_images to avoid storing twice; fall back to copy
            if not all_dest.exists():
                try:
                    all_dest.parent.mkdir(parents=True, exist_ok=True)
                    all_dest.hardlink_to(label_dest)
                except OSError:
                    import shutil
                    shutil.copy2(label_dest, all_dest)

            if not no_mark:
                mark_downloaded(server, fid)

            stats['downloaded'] += 1

    print(f'\nDone: {stats["downloaded"]} downloaded, '
          f'{stats["skipped"]} skipped, {stats["failed"]} failed')
    if dry_run:
        print('(dry-run — no files written)')


def main() -> None:
    ap = argparse.ArgumentParser(description='Download labeled BirdWatch frames from server')
    ap.add_argument('--server', default=DEFAULT_SERVER,
                    help=f'Server base URL (default: {DEFAULT_SERVER})')
    ap.add_argument('--root', required=True,
                    help='Local root folder for downloaded images')
    ap.add_argument('--label', choices=ALL_LABELS,
                    help='Only download this label (default: all labels)')
    ap.add_argument('--include-downloaded', action='store_true',
                    help='Re-download files already marked as downloaded on server')
    ap.add_argument('--no-mark', action='store_true',
                    help='Do not mark frames as downloaded on server')
    ap.add_argument('--dry-run', action='store_true',
                    help='Print what would be downloaded without saving files')
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    labels = [args.label] if args.label else ALL_LABELS

    print(f'Server : {args.server}')
    print(f'Root   : {root}')
    print(f'Labels : {labels}')
    if args.dry_run:
        print('Mode   : DRY RUN')

    run(
        server=args.server,
        root=root,
        labels=labels,
        include_downloaded=args.include_downloaded,
        no_mark=args.no_mark,
        dry_run=args.dry_run,
    )


if __name__ == '__main__':
    main()
