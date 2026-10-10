import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.fernet import Fernet


def validate_url(value, label, origin=False):
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and
                 not parsed.username and not parsed.password and not parsed.query and
                 not parsed.fragment and not any(c.isspace() or ord(c) < 32 for c in value))
        if origin and parsed.path not in ('', '/'):
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError('Invalid ' + label + '; use an HTTP(S) URL without credentials')
    return value.rstrip('/')


@dataclass(frozen=True)
class Settings:
    radarr_url: str
    sonarr_url: str
    emby_url: str
    radarr_key: str
    sonarr_key: str
    encryption_key: str
    data_dir: Path
    origins: tuple[str, ...] = ('http://localhost:8000',)
    secure_cookie: bool = True
    poll_seconds: int = 15
    session_seconds: int = 86400
    movie_root: str = '/media/movies'
    tv_root: str = '/media/tv'
    anime_root: str = '/media/anime'
    cartoons_root: str = '/media/cartoons'
    radarr_profile_id: int = 1
    sonarr_profile_id: int = 1
    radarr_host_header: str = ''
    sonarr_host_header: str = ''
    emby_public_url: str = ''
    trusted_proxy_ips: tuple[str, ...] = ()

    def __post_init__(self):
        for label in ('radarr_url', 'sonarr_url', 'emby_url'):
            object.__setattr__(self, label, validate_url(getattr(self, label), label))
        object.__setattr__(self, 'origins', tuple(validate_url(x, 'PORTAL_ORIGINS', origin=True) for x in self.origins))
        if not self.origins:
            raise RuntimeError('PORTAL_ORIGINS must contain at least one origin')
        if self.emby_public_url:
            object.__setattr__(self, 'emby_public_url', validate_url(self.emby_public_url, 'EMBY_PUBLIC_URL'))
        if not self.radarr_key or not self.sonarr_key:
            raise RuntimeError('Service API keys are required')
        try:
            Fernet(self.encryption_key.encode())
        except (ValueError, TypeError):
            raise RuntimeError('PORTAL_ENCRYPTION_KEY must be a generated Fernet key') from None
        for label in ('radarr_profile_id', 'sonarr_profile_id', 'poll_seconds', 'session_seconds'):
            if not isinstance(getattr(self, label), int) or getattr(self, label) < 1:
                raise RuntimeError(label + ' must be a positive integer')
        for label in ('movie_root', 'tv_root', 'anime_root', 'cartoons_root'):
            value = getattr(self, label)
            if not value or not value.startswith('/') or any(ord(c) < 32 for c in value):
                raise RuntimeError(label + ' must be an absolute path in the media service')
        for label in ('radarr_host_header', 'sonarr_host_header'):
            if getattr(self, label) and not re.fullmatch(r'[A-Za-z0-9.\-:\[\]]+', getattr(self, label)):
                raise RuntimeError('Invalid ' + label)
        try:
            object.__setattr__(self, 'trusted_proxy_ips', tuple(str(ipaddress.ip_address(x)) for x in self.trusted_proxy_ips))
        except ValueError:
            raise RuntimeError('PORTAL_TRUSTED_PROXY_IPS must contain IP addresses') from None

    @classmethod
    def from_env(cls):
        names = ('RADARR_URL', 'SONARR_URL', 'EMBY_URL', 'RADARR_API_KEY', 'SONARR_API_KEY',
                 'PORTAL_ENCRYPTION_KEY', 'PORTAL_ORIGINS', 'RADARR_PROFILE_ID', 'SONARR_PROFILE_ID',
                 'MOVIE_ROOT', 'TV_ROOT')
        missing = [name for name in names if not os.environ.get(name, '').strip()]
        if missing:
            raise RuntimeError('Missing required settings: ' + ', '.join(missing))
        cookie = os.environ.get('PORTAL_SECURE_COOKIE', 'true').lower()
        if cookie not in ('true', 'false'):
            raise RuntimeError('PORTAL_SECURE_COOKIE must be true or false')
        try:
            radarr_profile = int(os.environ['RADARR_PROFILE_ID'])
            sonarr_profile = int(os.environ['SONARR_PROFILE_ID'])
        except ValueError:
            raise RuntimeError('Profile IDs must be positive integers') from None
        return cls(
            radarr_url=os.environ['RADARR_URL'], sonarr_url=os.environ['SONARR_URL'],
            emby_url=os.environ['EMBY_URL'], radarr_key=os.environ['RADARR_API_KEY'],
            sonarr_key=os.environ['SONARR_API_KEY'], encryption_key=os.environ['PORTAL_ENCRYPTION_KEY'],
            data_dir=Path(os.environ.get('PORTAL_DATA_DIR', '/data')),
            origins=tuple(x.strip() for x in os.environ['PORTAL_ORIGINS'].split(',') if x.strip()),
            secure_cookie=cookie == 'true', movie_root=os.environ['MOVIE_ROOT'], tv_root=os.environ['TV_ROOT'],
            anime_root=os.environ.get('ANIME_ROOT') or os.environ['TV_ROOT'],
            cartoons_root=os.environ.get('CARTOONS_ROOT') or os.environ['TV_ROOT'],
            radarr_profile_id=radarr_profile, sonarr_profile_id=sonarr_profile,
            radarr_host_header=os.environ.get('RADARR_HOST_HEADER', ''),
            sonarr_host_header=os.environ.get('SONARR_HOST_HEADER', ''),
            emby_public_url=os.environ.get('EMBY_PUBLIC_URL', ''),
            trusted_proxy_ips=tuple(x.strip() for x in os.environ.get('PORTAL_TRUSTED_PROXY_IPS', '').split(',') if x.strip()),
        )
