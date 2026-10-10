import asyncio
import json
import sqlite3
from dataclasses import replace
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest

from portal.main import create_app
from portal.settings import Settings
from portal.store import Store
from portal.services import Services, UpstreamError


@pytest.fixture
def settings(tmp_path):
    return Settings("http://radarr", "http://sonarr", "http://emby", "RADARR_SECRET", "SONARR_SECRET",
                    Fernet.generate_key().decode(), tmp_path, origins=("http://testserver",), secure_cookie=False, radarr_profile_id=5, sonarr_profile_id=5, radarr_host_header="radarr.example.com", sonarr_host_header="sonarr.example.com")


class Fake:
    def __init__(self):
        self.calls = []
        self.movies, self.series = [], []
        self.movie = {"tmdbId": 123, "title": "A movie", "year": 2024, "titleSlug": "a-movie-123", "images": []}
        self.show = {"tvdbId": 456, "title": "A show", "year": 2023, "titleSlug": "a-show", "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}]}
        self.queue_records = []
        self.episodes = []
        self.emby_items = []
        self.emby_episodes = []
        self.disabled = False
        self.offline = False
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.calls.append(request)
        host, path = request.url.host, request.url.path
        if host == "emby":
            if path == "/Users/AuthenticateByName":
                body = json.loads(request.content)
                if body["Pw"] != "correct":
                    return httpx.Response(401, json={"detail": "secret should not leak"})
                return httpx.Response(200, json={"User": {"Id": "user-one", "Name": "User", "Policy": {"IsDisabled": self.disabled}}, "AccessToken": "EMBY_SECRET"})
            if path == "/Users/user-one":
                return httpx.Response(200, json={"Policy": {"IsDisabled": self.disabled}})
            if path == "/Users/user-one/Items":
                return httpx.Response(200, json={"Items": self.emby_episodes if request.url.params.get("ParentId") else self.emby_items})
        if self.offline:
            return httpx.Response(503, json={"key": "upstream-secret"})
        movie = host == "radarr"
        base = "/api/v3/movie" if movie else "/api/v3/series"
        media = self.movies if movie else self.series
        if path == base + "/lookup":
            return httpx.Response(200, json=[self.movie if movie else self.show])
        if path == base and request.method == "GET":
            return httpx.Response(200, json=media)
        if path == base and request.method == "POST":
            body = json.loads(request.content) | {"id": 9}
            media.append(body)
            return httpx.Response(201, json=body)
        if path == base + "/9":
            if request.method == "PUT":
                media[0] = json.loads(request.content)
            return httpx.Response(200, json=media[0])
        if path == "/api/v3/command":
            return httpx.Response(201, json={"id": 50})
        if path == "/api/v3/queue":
            return httpx.Response(200, json={"records": self.queue_records, "totalRecords": len(self.queue_records)})
        if path == "/api/v3/episode":
            return httpx.Response(200, json=self.episodes)
        raise AssertionError((host, path, request.method))


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def client(settings, fake):
    with TestClient(create_app(settings, fake.transport, run_worker=False)) as client:
        yield client


def login(client):
    r = client.post("/api/login", headers={"Origin": "http://testserver"}, json={"username": "User", "password": "correct"})
    assert r.status_code == 200
    return {"Origin": "http://testserver", "X-CSRF-Token": r.json()["csrf"]}


def test_login_and_secret_redaction(client, fake):
    assert client.get("/api/search?kind=movie&q=hello").status_code == 401
    wrong = client.post("/api/login", headers={"Origin": "http://testserver"}, json={"username": "User", "password": "bad"})
    assert wrong.status_code == 401
    assert "secret" not in wrong.text
    headers = login(client)
    assert client.get("/api/me").json()["name"] == "User"
    assert "EMBY_SECRET" not in client.get("/api/me").text
    assert "HttpOnly" in client.cookies.get("media_search_session") or len(client.cookies.get("media_search_session")) >= 32
    assert client.post("/api/logout", headers=headers, json={}).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_secure_cookie(settings, fake):
    with TestClient(create_app(replace(settings, secure_cookie=True), fake.transport, run_worker=False)) as c:
        response = c.post("/api/login", headers={"Origin": "http://testserver"}, json={"username": "User", "password": "correct"})
        assert "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]


def test_csrf_origin_body_and_disabled_user(client, fake):
    assert client.post("/api/login", headers={"Origin": "http://evil"}, json={"username": "User", "password": "correct"}).status_code == 403
    headers = login(client)
    assert client.post("/api/requests", headers={"Origin": "http://testserver"}, json={"kind": "movie", "provider_id": 123}).status_code == 403
    r = client.post("/api/requests", headers=headers, json={"kind": "movie", "provider_id": 123, "key": "do-not-echo"})
    assert r.status_code == 422 and "do-not-echo" not in r.text
    assert client.post("/api/login", content=b"x" * 9000).status_code == 413
    fake.disabled = True
    assert client.get("/api/me").status_code == 401


def test_search_uses_title_lookup_and_keys_stay_server_side(client, fake):
    login(client)
    r = client.get("/api/search?kind=movie&q=movie%20title")
    assert r.json()["results"][0]["provider_id"] == 123
    sent = fake.calls[-1]
    assert sent.url.params["term"] == "movie title"
    assert sent.headers["x-api-key"] == "RADARR_SECRET"
    assert sent.headers["host"] == "radarr.example.com"
    assert "SECRET" not in r.text


def test_sonarr_preserves_valid_hostname_on_private_connection(client, fake):
    login(client)
    assert client.get("/api/search?kind=tv&q=show").status_code == 200
    assert fake.calls[-1].url.host == "sonarr"
    assert fake.calls[-1].headers["host"] == "sonarr.example.com"


def test_search_accepts_missing_overview(client, fake):
    login(client)
    fake.movie["overview"] = None
    assert client.get("/api/search?kind=movie&q=movie").json()["results"][0]["overview"] == ""


@pytest.mark.asyncio
async def test_multi_episode_file_counts_both_episodes(settings, fake):
    store = Store(settings)
    sid, _ = store.new_session("user-one", "User", "EMBY_SECRET", 86400)
    service = Services(settings, store, fake.transport)
    fake.emby_items = [{"Id": "series", "ProviderIds": {"Tvdb": "456"}}]
    fake.emby_episodes = [{"ParentIndexNumber": 1, "IndexNumber": 1, "IndexNumberEnd": 2, "MediaSources": [{"Id": "file"}]}]
    assert await service.emby_availability({"kind": "series", "provider_id": 456}, store.session(sid)) == (2, "series")
    await service.http.aclose()


@pytest.mark.parametrize("kind,folder,series_type", [("tv", "/media/tv", "standard"), ("anime", "/media/anime", "anime"), ("cartoons", "/media/cartoons", "standard")])
def test_show_requests_entire_series_with_correct_folder(client, fake, kind, folder, series_type):
    headers = login(client)
    r = client.post("/api/requests", headers=headers, json={"kind": kind, "provider_id": 456})
    assert r.status_code == 202
    show = fake.series[0]
    assert show["rootFolderPath"] == folder and show["seriesType"] == series_type
    assert show["qualityProfileId"] == 5 and show["monitorNewItems"] == "all"
    assert show["seasons"][0]["monitored"] is False and show["seasons"][1]["monitored"] is True


def test_duplicate_requests_reuse_managed_movie(client, fake):
    headers = login(client)
    first = client.post("/api/requests", headers=headers, json={"kind": "movie", "provider_id": 123})
    second = client.post("/api/requests", headers=headers, json={"kind": "movie", "provider_id": 123})
    assert first.json()["id"] == second.json()["id"]
    assert len(fake.movies) == 1
    assert fake.movies[0]["rootFolderPath"] == "/media/movies"
    assert len(client.get("/api/requests").json()["requests"]) == 1


def test_other_user_cannot_read_request(settings, fake, client):
    h = login(client)
    r = client.post("/api/requests", headers=h, json={"kind": "movie", "provider_id": 123}).json()
    assert client.get("/api/requests/" + r["id"]).status_code == 200
    store = client.app.state.store
    sid, _ = store.new_session("other-user", "Other", "other-token", 86400)
    # Session validation is independent of ownership; the store must enforce isolation.
    assert store.get(r["id"], "other-user") is None
    assert store.requests("other-user") == []


def test_session_tokens_are_encrypted_and_requests_survive_restart(settings, client):
    h = login(client)
    rid = client.post("/api/requests", headers=h, json={"kind": "movie", "provider_id": 123}).json()["id"]
    store = Store(settings)
    assert store.get(rid, "user-one")["upstream_id"] == 9
    with store.connection() as db:
        assert b"EMBY_SECRET" not in bytes(db.execute("SELECT token FROM sessions").fetchone()[0])


@pytest.mark.asyncio
async def test_status_download_import_and_emby(settings, fake):
    store = Store(settings)
    store.new_session("user-one", "User", "EMBY_SECRET", 86400)
    service = Services(settings, store, fake.transport)
    row = await service.submit("movie", 123, "user-one")
    fake.queue_records = [{"movieId": 9, "status": "downloading", "size": 100, "sizeleft": 40}]
    await service.refresh(row)
    updated = store.get(row["id"])
    assert updated["state"] == "downloading" and updated["progress"] == 60
    fake.movies[0]["hasFile"] = True
    fake.queue_records = []
    await service.refresh(updated)
    assert store.get(row["id"])["state"] == "importing"
    fake.emby_items = [{"Id": "emby-movie", "ProviderIds": {"Tmdb": "123"}, "MediaSources": [{"Id": "file"}]}]
    await service.refresh(store.get(row["id"]))
    assert store.get(row["id"])["state"] == "available"
    await service.http.aclose()


@pytest.mark.asyncio
async def test_partial_tv_availability(settings, fake):
    store = Store(settings)
    store.new_session("user-one", "User", "EMBY_SECRET", 86400)
    service = Services(settings, store, fake.transport)
    row = await service.submit("tv", 456, "user-one")
    fake.episodes = [{"seasonNumber": 1, "airDateUtc": "2020-01-01T00:00:00Z", "hasFile": True}, {"seasonNumber": 1, "airDateUtc": "2020-01-02T00:00:00Z", "hasFile": False}]
    fake.emby_items = [{"Id": "emby-series", "ProviderIds": {"Tvdb": "456"}}]
    fake.emby_episodes = [{"ParentIndexNumber": 1, "IndexNumber": 1, "MediaSources": [{"Id": "file"}]}]
    await service.refresh(row)
    updated = store.get(row["id"])
    assert updated["state"] == "partial" and "1 of 2" in updated["detail"]
    await service.http.aclose()


@pytest.mark.asyncio
async def test_recovery_does_not_readd_upstream_media(settings, fake):
    store = Store(settings)
    row = store.add_request("movie", 123, "A movie", 2024, "/media/movies", "user-one")
    fake.movies = [fake.movie | {"id": 9, "hasFile": True, "monitored": True}]
    service = Services(settings, store, fake.transport)
    await service.refresh(row)
    assert store.get(row["id"])["upstream_id"] == 9
    assert not any(x.method == "POST" and x.url.path == "/api/v3/movie" for x in fake.calls)
    await service.http.aclose()


@pytest.mark.asyncio
async def test_search_command_retries_after_upstream_failure(settings, fake):
    store = Store(settings)
    row = store.add_request("movie", 123, "A movie", 2024, "/media/movies", "user-one")
    store.update(row["id"], upstream_id=9, state="requested")
    fake.movies = [fake.movie | {"id": 9, "hasFile": False, "monitored": True}]
    service = Services(settings, store, fake.transport)
    await service.refresh(store.get(row["id"]))
    assert any(x.url.path == "/api/v3/command" for x in fake.calls)
    assert len(fake.movies) == 1
    await service.http.aclose()


@pytest.mark.asyncio
async def test_queue_pagination(settings):
    pages = []
    def handler(req):
        page = int(req.url.params["page"]); pages.append(page)
        return httpx.Response(200, json={"totalRecords": 101, "records": [{"movieId": 1}] * 100 if page == 1 else [{"movieId": 9}]})
    services = Services(settings, Store(settings), httpx.MockTransport(handler))
    assert await services.queue("radarr", 9, True) == [{"movieId": 9}]
    assert pages == [1, 2]
    await services.http.aclose()


def test_upstream_errors_do_not_leak(client, fake):
    login(client); fake.offline = True
    r = client.get("/api/search?kind=movie&q=hello")
    assert r.status_code == 503 and "secret" not in r.text


def test_public_assets_security_headers_and_no_api_docs(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "unsafe-inline" not in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    for path in ("/assets/portal.css", "/assets/portal.js"):
        assert client.get(path).status_code == 200
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
