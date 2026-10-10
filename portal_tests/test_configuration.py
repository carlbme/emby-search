from dataclasses import replace
import sqlite3
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from portal.settings import Settings
from portal.main import create_app
from portal.services import Services, public_request
from portal.store import Store
from portal.backup import backup
from portal_tests.test_portal import settings, fake, client, login


@pytest.fixture
def environment(monkeypatch, tmp_path):
    for key in ('RADARR_URL', 'SONARR_URL', 'EMBY_URL', 'RADARR_API_KEY', 'SONARR_API_KEY',
                'PORTAL_ENCRYPTION_KEY', 'PORTAL_ORIGINS', 'RADARR_PROFILE_ID', 'SONARR_PROFILE_ID',
                'MOVIE_ROOT', 'TV_ROOT', 'ANIME_ROOT', 'CARTOONS_ROOT', 'RADARR_HOST_HEADER',
                'SONARR_HOST_HEADER', 'EMBY_PUBLIC_URL', 'PORTAL_TRUSTED_PROXY_IPS', 'PORTAL_SECURE_COOKIE'):
        monkeypatch.delenv(key, raising=False)
    values = dict(RADARR_URL='http://radarr.example.com:7878/base', SONARR_URL='http://sonarr.example.com:8989',
                  EMBY_URL='http://emby.example.com:8096', RADARR_API_KEY='synthetic-radarr', SONARR_API_KEY='synthetic-sonarr',
                  PORTAL_ENCRYPTION_KEY=Fernet.generate_key().decode(), PORTAL_ORIGINS='https://requests.example.com',
                  RADARR_PROFILE_ID='3', SONARR_PROFILE_ID='7', MOVIE_ROOT='/media/movies', TV_ROOT='/media/tv',
                  PORTAL_DATA_DIR=str(tmp_path))
    for key, value in values.items():monkeypatch.setenv(key, value)
    return values


def test_env_requires_configuration_and_falls_back_to_tv(environment, monkeypatch):
    s = Settings.from_env()
    assert s.radarr_profile_id == 3 and s.sonarr_profile_id == 7
    assert s.anime_root == s.cartoons_root == s.tv_root
    assert s.secure_cookie and not s.trusted_proxy_ips
    monkeypatch.delenv('RADARR_API_KEY')
    with pytest.raises(RuntimeError, match='RADARR_API_KEY'):Settings.from_env()


@pytest.mark.parametrize('key,value', [('RADARR_URL','http://user:password@example.com'),
                                     ('EMBY_PUBLIC_URL','javascript:alert(1)'), ('SONARR_HOST_HEADER','bad\r\nheader'),
                                     ('PORTAL_ORIGINS','https://example.com/path'), ('RADARR_PROFILE_ID','0'),
                                     ('PORTAL_TRUSTED_PROXY_IPS','not-an-ip'), ('PORTAL_SECURE_COOKIE','maybe')])
def test_invalid_settings_fail_without_echoing_values(environment, monkeypatch, key, value):
    monkeypatch.setenv(key,value)
    with pytest.raises(RuntimeError) as error:Settings.from_env()
    assert value not in str(error.value)


def test_host_restriction_applies_to_environment_app(environment):
    with TestClient(create_app(run_worker=False), base_url='https://requests.example.com') as c:
        assert c.get('/healthz').status_code == 200
        assert c.get('/healthz',headers={'Host':'evil.example.com'}).status_code == 400


@pytest.mark.asyncio
async def test_endpoint_base_path_and_default_host(settings):
    seen=[]
    def handler(req):seen.append(req);return httpx.Response(200,json=[])
    s=replace(settings,radarr_url='http://radarr.example.com:7878/base',radarr_host_header='')
    service=Services(s,Store(s),httpx.MockTransport(handler))
    await service.call('radarr','GET','/movie')
    assert seen[0].url.path == '/base/api/v3/movie'
    assert seen[0].headers['host'] == 'radarr.example.com:7878'
    await service.http.aclose()


@pytest.mark.parametrize('kind,expected',[('tv','standard'),('anime','anime'),('cartoons','standard')])
@pytest.mark.asyncio
async def test_shared_folder_preserves_type_across_restart(settings, fake, kind, expected):
    s=replace(settings,anime_root=settings.tv_root,cartoons_root=settings.tv_root,radarr_profile_id=3,sonarr_profile_id=7)
    store=Store(s)
    row=store.add_request('series',456,'A show',2023,s.tv_root,'user-one',series_type=expected)
    restarted=Store(s)
    service=Services(s,restarted,fake.transport)
    await service.refresh(restarted.get(row['id']))
    assert fake.series[0]['seriesType'] == expected and fake.series[0]['qualityProfileId'] == 7
    await service.http.aclose()


@pytest.mark.asyncio
async def test_movie_uses_separate_profile(settings, fake):
    s=replace(settings,radarr_profile_id=3,sonarr_profile_id=7)
    service=Services(s,Store(s),fake.transport)
    await service.submit('movie',123,'user-one')
    assert fake.movies[0]['qualityProfileId'] == 3
    await service.http.aclose()


def test_public_emby_link(settings):
    store=Store(settings)
    row=store.add_request('movie',123,'A movie',2024,settings.movie_root,'user-one')
    store.update(row['id'],snapshot='{"emby_item_id":"item&special"}')
    row=store.get(row['id'])
    assert public_request(row)['emby_url'] is None
    assert public_request(row,'https://watch.example.com/emby')['emby_url']=='https://watch.example.com/emby/web/index.html#!/item?id=item%26special'


@pytest.mark.parametrize('trusted',[False,True])
def test_client_ip_trust(settings, fake, trusted):
    s=replace(settings,trusted_proxy_ips=('192.0.2.10',) if trusted else ())
    with TestClient(create_app(s,fake.transport,run_worker=False),client=('192.0.2.10',12345)) as c:
        statuses=[]
        for n in range(11):
            statuses.append(c.post('/api/login',headers={'Origin':'http://testserver','CF-Connecting-IP':f'198.51.100.{n+1}'},json={'username':'User','password':'correct'}).status_code)
        assert statuses[-1] == (200 if trusted else 429)


def test_backup_contains_wal_updates_and_refuses_overwrite(settings,tmp_path):
    store=Store(settings)
    row=store.add_request('movie',123,'A movie',2024,settings.movie_root,'user-one')
    with store.connection() as db:
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('UPDATE requests SET detail=? WHERE id=?',('Latest status',row['id']))
        db.commit()
        destination=tmp_path/'backups'/'snapshot.sqlite3'
        backup(store.path,destination)
        with sqlite3.connect(destination) as restored:
            assert restored.execute('SELECT detail FROM requests').fetchone()[0]=='Latest status'
        assert destination.stat().st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):backup(store.path,destination)


@pytest.mark.parametrize('shared',[False,True])
def test_legacy_schema_migration_retains_requests(settings, shared):
    s=replace(settings,anime_root=settings.tv_root) if shared else settings
    s.data_dir.mkdir(exist_ok=True)
    with sqlite3.connect(s.data_dir/'portal.sqlite3') as db:
        db.execute("CREATE TABLE requests (id TEXT PRIMARY KEY, kind TEXT, provider_id INTEGER, title TEXT, year INTEGER, folder TEXT, upstream_id INTEGER, state TEXT, detail TEXT, progress INTEGER, snapshot TEXT, created INTEGER, updated INTEGER, UNIQUE(kind,provider_id))")
        db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",('legacy','series',456,'A show',2023,s.anime_root,9,'requested','',None,'{}',1,1))
    store=Store(s)
    row=store.get('legacy')
    assert row['upstream_id']==9 and row['series_type']==('standard' if shared else 'anime')
    assert Store(s).get('legacy')==row
