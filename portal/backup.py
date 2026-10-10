"""Create a consistent SQLite backup, refusing to replace an existing file."""
import argparse
import os
from pathlib import Path
import sqlite3


def backup(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise RuntimeError('Source database does not exist')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dest:
            src.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    source = Path(os.environ.get('PORTAL_DATA_DIR', '/data')).resolve() / 'portal.sqlite3'
    backup(source, args.destination.resolve())
    print('Verified backup:', args.destination)


if __name__ == '__main__':
    main()
