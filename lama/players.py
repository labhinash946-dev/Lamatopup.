"""Free Fire nickname lookup. Convenience only: never gate payment on it."""
import re
import threading
import time

import requests

GOPAY_FF_URL = "https://gopay.co.id/games/v1/order/prepare/FREEFIRE"
NAME_KEYS = ["nickname", "nickName", "name", "username", "player_name", "account_name"]
REGION_KEYS = ["region", "server", "country"]


def find_field(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            found = find_field(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_field(v, keys)
            if found:
                return found
    return None


def extract_player(data):
    """Pull (name, region) from a lookup response. FreeFireInfoSite nests both in basicInfo."""
    basic = data.get("basicInfo") if isinstance(data, dict) else None
    if isinstance(basic, dict):
        name = find_field(basic, NAME_KEYS)
        if name:
            return name, (find_field(basic, REGION_KEYS) or "").upper()[:10]
    return find_field(data, NAME_KEYS), (find_field(data, REGION_KEYS) or "").upper()[:10]


def _get_json(url, params=None, headers=None, timeout=10, retries=1):
    last = None
    for _ in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code >= 500:
                last = RuntimeError("server error")
                continue
            try:
                return r.status_code, r.json()
            except ValueError:
                if r.status_code == 404:
                    return 404, None
                last = RuntimeError("invalid response")
        except requests.RequestException as exc:
            last = exc
    raise RuntimeError("unavailable") from last


class PlayerLookup:
    def __init__(self, cfg):
        self.cfg = cfg
        self._cache = {}
        self._lock = threading.Lock()

    @property
    def custom_configured(self):
        u = self.cfg.player_lookup_url
        return u.lower() != "off" and u.startswith(("https://", "http://")) and "{uid}" in u

    def _lookup(self, uid):
        headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
        reached = False
        if self.custom_configured:
            h = dict(headers)
            if self.cfg.player_lookup_api_key:
                h["x-api-key"] = self.cfg.player_lookup_api_key
            template = self.cfg.player_lookup_url
            urls = [template.replace("{uid}", uid).replace("{region}", self.cfg.player_lookup_region)]
            if "{region}" in template:
                # Second attempt lets the lookup service auto-detect the region.
                bare = re.sub(r"[?&]region=\{region\}", "", template).replace("{uid}", uid)
                if bare not in urls:
                    urls.append(bare)
            not_found = False
            for url in urls:
                try:
                    status, data = _get_json(url, headers=h, timeout=self.cfg.player_lookup_timeout)
                    reached = True
                except RuntimeError:
                    break  # lookup service down/slow: don't wait again, go to the fallback
                name, region = extract_player(data) if data is not None else (None, "")
                if name and status < 400:
                    if self.cfg.player_lookup_strict and region and region != self.cfg.player_lookup_region:
                        return None, region
                    return name[:60], region or self.cfg.player_lookup_region
                if status in (400, 404):
                    not_found = True
            if not_found:
                return None, ""
        try:
            _, data = _get_json(GOPAY_FF_URL, params={"userId": uid}, headers=headers,
                                timeout=10, retries=0)
            reached = True
        except RuntimeError:
            if reached:
                return None, ""
            raise
        name = data.get("data") if isinstance(data, dict) else None
        if isinstance(name, str) and name.strip():
            return name.strip()[:60], ""
        return None, ""

    def check(self, uid):
        """Returns (name, region) or (None, ''). Raises RuntimeError if all sources are down."""
        now = time.time()
        with self._lock:
            hit = self._cache.get(uid)
            if hit and hit[0] > now:
                return hit[1], hit[2]
        name, region = self._lookup(uid)
        if name:
            with self._lock:
                if len(self._cache) > 2000:
                    self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
                self._cache[uid] = (now + 600, name, region)
        return name, region
