import asyncio
import json
import logging
from datetime import datetime, timezone
import httpx
from urllib.parse import quote

log = logging.getLogger(__name__)


class UpstreamError(Exception):
    def __init__(self, service, status=None):
        self.service, self.status = service, status
        super().__init__(service + " unavailable")


class Services:
    def __init__(self, settings, store, transport=None):
        self.settings, self.store = settings, store
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=4), trust_env=False,
                                      follow_redirects=False, transport=transport)
        self.submission_lock = asyncio.Lock()

    async def call(self, service, method, path, *, token=None, **kwargs):
        s = self.settings
        if service == "emby":
            url, headers = s.emby_url, {
                "X-Emby-Authorization": 'Emby Client="Media Search", Device="Request portal", DeviceId="emby-search", Version="1.0"',
            }
            if token:
                headers["X-Emby-Token"] = token
        else:
            url = s.radarr_url if service == "radarr" else s.sonarr_url
            headers = {"X-Api-Key": s.radarr_key if service == "radarr" else s.sonarr_key}
            host = s.radarr_host_header if service == "radarr" else s.sonarr_host_header
            if host:
                headers["Host"] = host
            path = "/api/v3" + path
        try:
            response = await self.http.request(method, url + path, headers=headers, **kwargs)
            if not 200 <= response.status_code < 300:
                raise UpstreamError(service, response.status_code)
            return response.json() if response.content else None
        except (httpx.HTTPError, ValueError):
            raise UpstreamError(service) from None

    async def authenticate(self, name, password):
        data = await self.call("emby", "POST", "/Users/AuthenticateByName",
                               json={"Username": name, "Pw": password})
        user = data.get("User", {})
        if user.get("Policy", {}).get("IsDisabled") or not user.get("Id") or not data.get("AccessToken"):
            raise UpstreamError("emby", 401)
        return user, data["AccessToken"]

    async def validate_session(self, session):
        user = await self.call("emby", "GET", "/Users/" + session["user_id"], token=session["token"])
        if user.get("Policy", {}).get("IsDisabled"):
            raise UpstreamError("emby", 401)

    async def search(self, kind, query):
        service = "radarr" if kind == "movie" else "sonarr"
        path = "/movie/lookup" if kind == "movie" else "/series/lookup"
        items = await self.call(service, "GET", path, params={"term": query})
        field = "tmdbId" if kind == "movie" else "tvdbId"
        seen, result = set(), []
        for item in items:
            pid = item.get(field)
            if not isinstance(pid, int) or pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            title = item.get("title")
            overview = item.get("overview")
            result.append({"provider_id": pid, "title": title[:200] if isinstance(title, str) else "Untitled",
                           "year": item.get("year"), "overview": overview[:500] if isinstance(overview, str) else ""})
            if len(result) == 30:
                break
        return result

    async def submit(self, kind, provider_id, user_id):
        async with self.submission_lock:
            media_kind = "movie" if kind == "movie" else "series"
            service, path, field = ("radarr", "/movie", "tmdbId") if media_kind == "movie" else ("sonarr", "/series", "tvdbId")
            lookup = await self.call(service, "GET", path + "/lookup", params={"term": ("tmdb:" if media_kind == "movie" else "tvdb:") + str(provider_id)})
            selected = next((item for item in lookup if item.get(field) == provider_id), None)
            if selected is None:
                raise UpstreamError(service, 404)
            folder = {"movie": self.settings.movie_root, "tv": self.settings.tv_root,
                      "anime": self.settings.anime_root, "cartoons": self.settings.cartoons_root}[kind]
            row = self.store.add_request(media_kind, provider_id, selected["title"], selected.get("year"), folder, user_id, series_type="anime" if kind == "anime" else "standard")
            if row["upstream_id"] is None or row["state"] == "failed":
                try:
                    await self.ensure_added(row, selected, service, path, field, kind)
                except UpstreamError:
                    self.store.update(row["id"], state="requested", detail="Waiting for the media service. Your request is saved.")
            return self.store.get(row["id"])

    async def ensure_added(self, row, selected=None, service=None, path=None, field=None, kind=None):
        movie = row["kind"] == "movie"
        service, path, field = ("radarr", "/movie", "tmdbId") if movie else ("sonarr", "/series", "tvdbId")
        managed = await self.call(service, "GET", path, params={field: row["provider_id"]})
        existing = next((item for item in managed if item.get(field) == row["provider_id"]), None)
        new = existing is None
        if new:
            if selected is None:
                lookup = await self.call(service, "GET", path + "/lookup", params={"term": ("tmdb:" if movie else "tvdb:") + str(row["provider_id"])})
                selected = next((x for x in lookup if x.get(field) == row["provider_id"]), None)
                if selected is None:
                    raise UpstreamError(service, 404)
            body = {**selected, "qualityProfileId": (self.settings.radarr_profile_id if movie else self.settings.sonarr_profile_id), "rootFolderPath": row["folder"], "monitored": True}
            body.pop("id", None)
            if movie:
                body.update(minimumAvailability="released", addOptions={"searchForMovie": True})
            else:
                body.update(seasonFolder=True, seriesType=row["series_type"],
                            monitorNewItems="all", addOptions={"monitor": "all", "searchForMissingEpisodes": True})
                body["seasons"] = [{**s, "monitored": s["seasonNumber"] > 0} for s in selected.get("seasons", [])]
            existing = await self.call(service, "POST", path, json=body)
        else:
            body = {**existing, "monitored": True}
            if not movie:
                body["monitorNewItems"] = "all"
                body["seasons"] = [{**s, "monitored": s["seasonNumber"] > 0} for s in existing.get("seasons", [])]
            if body != existing:
                existing = await self.call(service, "PUT", path + "/" + str(existing["id"]), json=body)
        # Persist identity before initiating a search: recovery never blindly re-adds media.
        self.store.update(row["id"], upstream_id=existing["id"], state="searching", detail="Searching for a suitable release.")
        if not new and not (movie and existing.get("hasFile")):
            await self.call(service, "POST", "/command", json={"name": "MoviesSearch" if movie else "SeriesSearch",
                            **({"movieIds": [existing["id"]]} if movie else {"seriesId": existing["id"]})})

    async def queue(self, service, upstream_id, movie):
        matches = []
        page = 1
        while True:
            data = await self.call(service, "GET", "/queue", params={"page": page, "pageSize": 100})
            records = data.get("records", [])
            matches.extend(x for x in records if x.get("movieId" if movie else "seriesId") == upstream_id)
            if not records or page * 100 >= data.get("totalRecords", len(records)):
                return matches
            page += 1
            if page > 100:
                raise UpstreamError(service)

    async def emby_availability(self, row, session):
        movie = row["kind"] == "movie"
        provider = "Tmdb" if movie else "Tvdb"
        # Provider ID matching avoids title/year collisions, and queries retain user permissions.
        data = await self.call("emby", "GET", "/Users/" + session["user_id"] + "/Items", token=session["token"],
                               params={"Recursive": "true", "IncludeItemTypes": "Movie" if movie else "Series",
                                       "AnyProviderIdEquals": provider + "." + str(row["provider_id"]),
                                       "Fields": "ProviderIds,MediaSources", "Limit": 100})
        items = [x for x in data.get("Items", []) if str(x.get("ProviderIds", {}).get(provider, "")) == str(row["provider_id"])]
        if not items:
            return 0, None
        if movie:
            return (1, items[0]["Id"]) if items[0].get("MediaSources") or items[0].get("Path") else (0, None)
        episodes = await self.call("emby", "GET", "/Users/" + session["user_id"] + "/Items", token=session["token"],
                                  params={"Recursive": "true", "ParentId": items[0]["Id"], "IncludeItemTypes": "Episode",
                                          "IsPlaceHolder": "false", "IsUnaired": "false", "Fields": "MediaSources,Path", "Limit": 10000})
        numbers = set()
        for item in episodes.get("Items", []):
            season, first = item.get("ParentIndexNumber", 0), item.get("IndexNumber")
            if season <= 0 or not isinstance(first, int) or not (item.get("MediaSources") or item.get("Path")):
                continue
            last = item.get("IndexNumberEnd") or first
            if not isinstance(last, int) or not first <= last < first + 100:
                last = first
            numbers.update((season, number) for number in range(first, last + 1))
        count = len(numbers)
        return count, items[0]["Id"]

    async def refresh(self, row):
        movie = row["kind"] == "movie"
        service, path = ("radarr", "/movie/") if movie else ("sonarr", "/series/")
        if row["upstream_id"] is None or row["state"] == "requested":
            async with self.submission_lock:
                row = self.store.get(row["id"])
                if row["upstream_id"] is None or row["state"] == "requested":
                    await self.ensure_added(row)
            row = self.store.get(row["id"])
        media = await self.call(service, "GET", path + str(row["upstream_id"]))
        records = await self.queue(service, row["upstream_id"], movie)
        total, imported = 1, int(bool(media.get("hasFile")))
        if not movie:
            episodes = await self.call(service, "GET", "/episode", params={"seriesId": row["upstream_id"]})
            now = datetime.now(timezone.utc)
            aired = []
            for episode in episodes:
                if episode.get("seasonNumber", 0) <= 0 or not episode.get("airDateUtc"):
                    continue
                try:
                    air = datetime.fromisoformat(episode["airDateUtc"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                if air <= now:
                    aired.append(episode)
            total, imported = len(aired), sum(bool(e.get("hasFile")) for e in aired)
        state, detail, progress = "waiting", "Waiting for a suitable release.", None
        snapshot = {"total": total, "imported": imported, "available": 0}
        if imported:
            state, detail = "importing", "Imported; waiting for Emby to detect the media."
            session = self.store.request_session(row["id"])
            if session:
                available, item_id = await self.emby_availability(row, session)
                snapshot.update(available=available, emby_item_id=item_id)
                if available:
                    state = "available" if movie or (total > 0 and available >= total) else "partial"
                    detail = "Available in Emby." if movie else f"{available} of {total} aired episodes available in Emby. Future episodes remain monitored."
            else:
                detail = "Imported. Sign in again to verify availability in Emby."
        if records:
            size = sum(float(x.get("size", 0) or 0) for x in records)
            left = sum(float(x.get("sizeleft", 0) or 0) for x in records)
            progress = max(0, min(100, round(100 * (1 - left / size)))) if size else None
            states = {str(x.get("status", "")).lower() for x in records}
            tracked = {str(x.get("trackedDownloadState", "")).lower() for x in records}
            if any(x.get("trackedDownloadStatus") in ("warning", "error") for x in records):
                state, detail = "attention", "Download or import needs attention in " + service.title() + "."
            elif "importing" in tracked or states & {"completed", "importpending", "importing"}:
                state, detail = "importing", "Download complete; importing into the media library."
            elif "downloading" in states:
                state, detail = "downloading", "Downloading" + (f" · {progress}%" if progress is not None else "")
            elif "failed" in states:
                state, detail = "attention", "Download failed; the media service may try another release."
            else:
                state, detail = "queued", "Queued for download."
            if not movie and snapshot["available"]:
                detail += f" · {snapshot['available']} of {total} aired episodes in Emby."
        elif row["state"] == "searching" and not imported and row["created"] > datetime.now().timestamp() - 90:
            state, detail = "searching", "Searching for a suitable release."
        self.store.update(row["id"], state=state, detail=detail, progress=progress, snapshot=json.dumps(snapshot))

    async def worker(self):
        while True:
            for row in self.store.requests():
                try:
                    await self.refresh(row)
                except UpstreamError as exc:
                    log.warning("Media service unavailable: %s status=%s", exc.service, exc.status)
                    self.store.update(row["id"], detail="Media service temporarily unavailable. Your request is saved.")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.error("Could not update a media request")
                    self.store.update(row["id"], detail="Status temporarily unavailable. Your request is saved.")
            await asyncio.sleep(self.settings.poll_seconds)


def public_request(row, emby_public_url=None):
    snapshot = json.loads(row["snapshot"])
    return {key: row[key] for key in ("id", "kind", "title", "year", "state", "detail", "progress", "created", "updated")} | {
        "episodes": snapshot if row["kind"] == "series" else None,
        "emby_url": (emby_public_url.rstrip("/") + "/web/index.html#!/item?id=" + quote(str(snapshot["emby_item_id"]), safe="")) if emby_public_url and snapshot.get("emby_item_id") else None,
    }
