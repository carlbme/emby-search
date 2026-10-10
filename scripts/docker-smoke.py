"""Isolated Compose smoke test using synthetic credentials and no media downloads."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    root = Path(__file__).resolve().parents[1]
    run('docker', 'build', '-f', str(root / 'deploy/Dockerfile'), '-t', 'emby-search:smoke', str(root))
    # All disposable state lives outside the checkout; never replace a user's .env/data.
    with tempfile.TemporaryDirectory(prefix='emby-search-smoke-') as directory:
        path = Path(directory)
        key = subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'python', 'emby-search:smoke',
                                      '-c', 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'], text=True).strip()
        (path / '.env').write_text('\n'.join((
            'RADARR_URL=http://radarr.invalid:7878', 'SONARR_URL=http://sonarr.invalid:8989',
            'EMBY_URL=http://emby.invalid:8096', 'RADARR_API_KEY=synthetic-radarr',
            'SONARR_API_KEY=synthetic-sonarr', 'PORTAL_ENCRYPTION_KEY=' + key,
            'PORTAL_ORIGINS=http://127.0.0.1:8000', 'PORTAL_SECURE_COOKIE=false',
            'RADARR_PROFILE_ID=1', 'SONARR_PROFILE_ID=2', 'MOVIE_ROOT=/media/movies', 'TV_ROOT=/media/tv',
        )) + '\n')
        (path / '.env').chmod(0o600)
        (path / 'data').mkdir(mode=0o700)
        # Prepare permissions inside Docker; works without host root privileges.
        run('docker', 'run', '--rm', '--user', '0:0', '--entrypoint', 'python',
            '-v', str(path / 'data') + ':/data', 'emby-search:smoke', '-c',
            'import os; os.chown("/data",10001,10001); os.chmod("/data",0o700)')
        compose = (root / 'compose.yml').read_text().replace('emby-search:local', 'emby-search:smoke')
        compose = compose.replace('"${PORTAL_BIND_ADDRESS:-127.0.0.1}:${PORTAL_PORT:-8000}:8000"', '"127.0.0.1::8000"')
        (path / 'compose.yml').write_text(compose)
        command = ['docker', 'compose', '--project-name', 'emby-search-smoke', '-f', str(path / 'compose.yml')]
        try:
            run(*command, 'up', '-d', '--no-build')
            port = subprocess.check_output(command + ['port', 'portal', '8000'], text=True).strip().rsplit(':', 1)[1]
            url = 'http://127.0.0.1:' + port
            for _ in range(40):
                try:
                    with urllib.request.urlopen(url + '/healthz', timeout=2) as response:
                        assert json.load(response) == {'status': 'ok'}
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError('Container did not become healthy')
            with urllib.request.urlopen(url, timeout=2) as response:
                assert b'Emby Search' in response.read()
            run(*command, 'exec', '-T', 'portal', 'python', '-c',
                'import sqlite3; c=sqlite3.connect("/data/portal.sqlite3"); c.execute("INSERT INTO requests(id,kind,provider_id,title,folder,created,updated) VALUES(?,?,?,?,?,?,?)",("smoke","movie",1,"Synthetic","/media/movies",1,1)); c.commit()')
            run(*command, 'restart', 'portal')
            run(*command, 'exec', '-T', 'portal', 'python', '-c',
                'import sqlite3; c=sqlite3.connect("/data/portal.sqlite3"); assert c.execute("SELECT title FROM requests WHERE id=\'smoke\'").fetchone()[0]=="Synthetic"; assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"')
            print('Compose health, page, and persistence checks passed')
        finally:
            run(*command, 'down')
            run('docker', 'run', '--rm', '--user', '0:0', '--entrypoint', 'python',
                '-v', str(path / 'data') + ':/data', 'emby-search:smoke', '-c',
                'import os; os.chown("/data",'+str(__import__('os').getuid())+','+str(__import__('os').getgid())+'); os.chmod("/data",0o700)')


if __name__ == '__main__':
    main()
