"""疾管署來源的 URL、robots、重新導向與請求預算；不載入任何入庫依賴。"""
import re
import time
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests

from ca_bundle import get_ca_bundle
from scraper_mohw import with_retries

BASE = "https://www.cdc.gov.tw"
ROBOTS_URL = BASE + "/robots.txt"
LICENSE_PATH = "/Category/FPage/TxkBIR9agw_IBRRmvn9TcQ"
USER_AGENT = "CARE-data/0.1 (+https://github.com/Capoo0618/CARE-data)"
TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "fbclid", "gclid"}
SECRET_KEYS = {"token", "access_token", "api_key", "apikey", "password", "secret", "authorization"}


class AccessDenied(ValueError):
    pass


class RequestLimit(AccessDenied):
    pass


def normalize_url(raw, path_prefix=None):
    """保留未知 query 與其原始順序／編碼，只移除列明的追蹤鍵和 fragment。"""
    try:
        p = urlsplit(urljoin(BASE + "/", raw or ""))
        if (p.scheme not in {"https", "http"} or p.hostname not in {"www.cdc.gov.tw", "cdc.gov.tw"}
                or p.username or p.password or p.port is not None):
            return None
        path = p.path.rstrip("/")
        if path_prefix:
            allowed = re.fullmatch(re.escape(path_prefix) + r"[A-Za-z0-9_-]+", path)
        else:
            allowed = path in {"/robots.txt", "/Disease/Index", LICENSE_PATH} or re.fullmatch(
                r"/(?:Disease/SubIndex|Category/Page)/[A-Za-z0-9_-]+", path)
        if not allowed:
            return None
        query = []
        for part in p.query.split("&") if p.query else []:
            key = unquote_plus(part.split("=", 1)[0]).lower()
            if key in SECRET_KEYS:
                return None
            if key not in TRACKING_KEYS:
                query.append(part)
        return urlunsplit(("https", "www.cdc.gov.tw", path, "&".join(query), ""))
    except ValueError:
        return None


def _get(url):
    # 必須自行檢查每一跳，不能讓 requests 先抓完被禁止的目標才驗證最終 URL。
    return requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-TW"},
                        timeout=25, verify=get_ca_bundle(), allow_redirects=False)


class CDCAccess:
    def __init__(self, *, get=None, sleep=time.sleep, sleep_seconds=0.4, max_requests=500,
                 normalizer=normalize_url, robots_url=ROBOTS_URL):
        if not 1 <= max_requests <= 500 or sleep_seconds < 0:
            raise ValueError("max_requests 必須為 1..500，節流秒數不能為負")
        self.transport = get or _get
        self.sleep = sleep
        self.sleep_seconds = sleep_seconds
        self.max_requests = max_requests
        self.requests = 0
        self.robots = None
        self.normalize = normalizer
        self.robots_url = robots_url

    def load_robots(self):
        response = self._request(self.robots_url, bootstrap=True)
        if "html" in response.headers.get("content-type", "").lower():
            raise AccessDenied("robots.txt 回傳 HTML，停止此來源")
        robots = RobotFileParser(self.robots_url)
        robots.parse(response.text.splitlines())
        self.robots = robots

    def get(self, url):
        return self._request(url)

    def _request(self, url, *, bootstrap=False):
        canonical = self.normalize(url)
        if not canonical:
            raise AccessDenied("網域、路徑或敏感 query 不在允許範圍")

        def chain():
            current = canonical
            for _ in range(4):
                if bootstrap:
                    if current != self.robots_url:
                        raise AccessDenied("robots.txt 重新導向至其他路徑，停止此來源")
                elif self.robots is None or not self.robots.can_fetch(USER_AGENT, current):
                    raise AccessDenied("robots 未確認或禁止此路徑")
                if self.requests >= self.max_requests:
                    raise RequestLimit("已達本輪請求上限（包含重試與重新導向）")
                self.sleep(self.sleep_seconds)
                self.requests += 1
                response = self.transport(current)
                if response.history:
                    raise AccessDenied("HTTP transport 自動跟隨重新導向，拒絕使用回應")
                if response.url and self.normalize(response.url) != current:
                    raise AccessDenied("回應 URL 與檢查的目標不符")
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    following = self.normalize(urljoin(current, location or ""))
                    if not location or not following:
                        raise AccessDenied("重新導向目標不在允許範圍")
                    current = following
                    continue
                response.raise_for_status()
                return response
            raise AccessDenied("已達重新導向上限")

        return with_retries(chain, sleep=self.sleep)
