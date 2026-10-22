"""Where a node really is (docs/07 §2.7): looked up from the address its host agent connects from, never typed by the miner.

Backends: an offline MaxMind GeoLite2 City/Country database when configured (no external calls; for production), otherwise
ip-api.com as the testnet fallback. Results are cached per address. The region code is `<continent>-<country>` in lowercase
(`eu-de`, `na-us`, `as-sg`): coarse on purpose, since GeoIP is reliable to the country, rarely to the datacenter.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("racksdock.marketplace.geo")


@dataclass
class GeoInfo:
    ip: str
    country: str
    country_code: str
    continent_code: str
    city: str = ""
    lat: float | None = None
    lon: float | None = None
    source: str = ""

    @property
    def region(self) -> str:
        return f"{self.continent_code.lower()}-{self.country_code.lower()}"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["region"] = self.region
        return d


def is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified)


class Geo:
    def __init__(self, mmdb_path: Path | None = None, http_fallback: bool = True, ttl_s: float = 86400.0, resolver=None):
        self.mmdb_path = mmdb_path if (mmdb_path and Path(mmdb_path).exists()) else None
        self.http_fallback = http_fallback
        self.ttl_s = ttl_s
        self.resolver = resolver  # tests: ip -> GeoInfo | None
        self._cache: dict[str, tuple[float, GeoInfo | None]] = {}
        self._lock = threading.Lock()
        self._reader = None

    # ---- backends
    def _mmdb(self, ip: str) -> GeoInfo | None:
        try:
            import geoip2.database  # type: ignore
        except ImportError:
            return None
        if self._reader is None:
            self._reader = geoip2.database.Reader(str(self.mmdb_path))
        try:
            r = self._reader.city(ip)
        except Exception:
            return None
        return GeoInfo(ip=ip, country=r.country.name or "", country_code=r.country.iso_code or "", continent_code=r.continent.code or "",
                       city=(r.city.name or ""), lat=r.location.latitude, lon=r.location.longitude, source="geolite2")

    def _http(self, ip: str) -> GeoInfo | None:
        url = f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,continentCode,city,lat,lon"
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                d = json.loads(r.read().decode())
        except Exception as e:
            log.warning("geoip lookup of %s failed: %s", ip, str(e)[:120])
            return None
        if d.get("status") != "success" or not d.get("countryCode"):
            return None
        return GeoInfo(ip=ip, country=d.get("country", ""), country_code=d["countryCode"], continent_code=d.get("continentCode", ""), city=d.get("city", ""),
                       lat=d.get("lat"), lon=d.get("lon"), source="ip-api")

    # ---- public
    def lookup(self, ip: str | None) -> GeoInfo | None:
        """Blocking; call through asyncio.to_thread from the event loop."""
        if not ip or not is_public(ip):
            return None
        with self._lock:
            hit = self._cache.get(ip)
            if hit and time.time() - hit[0] < (self.ttl_s if hit[1] else 600):
                return hit[1]
        info = self.resolver(ip) if self.resolver else None
        if info is None and self.resolver is None:
            if self.mmdb_path:
                info = self._mmdb(ip)
            if info is None and self.http_fallback:
                info = self._http(ip)
        with self._lock:
            self._cache[ip] = (time.time(), info)
        return info
