# scraper_media.py
"""健康媒體爬蟲（目前只有 udn 元氣網），寫入**獨立的** `daily_health_news` collection。

為什麼要有這支
--------------
每日推播要「每天都有當天的內容」，官方來源做不到。2026-09-09～09-12 實測：
政府與 TFC 語料加起來約 2.2 篇／天，以「今天或昨天發布」為準，30 天裡有 10 天
一篇都沒有；衛福部即時新聞雖是官方最高頻，也只有 2.00 篇／天、30 天裡 20 天有
發文。元氣網的 Google News sitemap 在 09-10、09-11 各有 13、17 篇，單一來源就
蓋得住每一天。

為什麼不寫進 `health_articles_chunks`
-------------------------------------
那個 collection 同時是 RAG 的檢索範圍，而 CARE 的 `retriever.py` 做
`$vectorSearch` 時**沒有任何 filter**——寫進去的東西都會變成回答與查核判定卡
的引用來源。一個闢謠 bot 拿媒體健康版當闢謠依據，與產品目的直接衝突。
分開存之後：推播讀得到，RAG 碰不到。這也是這批資料**不做向量化**的原因——
它不給檢索用，不需要花 Gemini embedding 的每日額度。

為什麼不在這裡過濾
------------------
這支只存原始欄位（含站方自己的分類 `category` / `subcategory`），過濾放在 CARE
的 `relevance.is_allowed_media_article`。理由與 `medical-news-tier2-quality`
決策 2 相同：過濾條件要調整時只改一處、立即生效，不必重跑 ETL。

資料從哪來
----------
`https://health.udn.com/robots.txt` 宣告的 Google News sitemap
（`/sitemap/gnews/1005`，約 48 小時窗）。每筆自帶標題、發布時間與
`<!-- cate：焦點 | sub：用藥停看聽 -->` 分類註解，**不必解析列表頁**。摘要取
文章頁的 `og:description`——站方自己寫的摘要，比截內文前 120 字乾淨。

robots.txt（2026-09-12 查）只 Disallow `/api/*`、`/rss/*`、各種 preview 等路徑，
文章頁 `/health/story/` 與 sitemap 皆允許。
"""
import re
import time
from datetime import datetime, timezone
from html import unescape

import requests
from bs4 import BeautifulSoup
from utils import make_soup

from ca_bundle import get_ca_bundle

GNEWS_INDEX_URL = "https://health.udn.com/sitemap/gnews/1005"
DEFAULT_SOURCE_NAME = "udn 元氣網"
COLLECTION_NAME = "daily_health_news"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
_CATEGORY_RE = re.compile(r"cate：(.*?)\s*\|\s*sub：(.*?)\s*-->")
_CHANNEL_RE = re.compile(r"/story/(\d+)/")


def _get(url, timeout=25):
    resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=get_ca_bundle())
    resp.raise_for_status()
    return resp


def _tag(block, name):
    match = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    if match is None:
        return ""
    return unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", match.group(1))).strip()


def parse_gnews_entries(xml):
    """解析 Google News sitemap 的一頁，回傳原始條目 dict list。純函式，可直接測。

    發布時間只取日期部分當 `published_at`：站方給的是 `+08:00` 的台北時間，
    日期部分就是台北日期，與官方來源的 `YYYY-MM-DD` 同一種格式——Tier 2 的
    時效判斷與排序都依賴這個欄位格式一致。
    """
    entries = []
    for block in _URL_BLOCK_RE.findall(xml):
        url = _tag(block, "loc")
        published_raw = _tag(block, "news:publication_date")
        title = _tag(block, "news:title")
        if not url or not published_raw or not title:
            continue
        category = _CATEGORY_RE.search(block)
        channel = _CHANNEL_RE.search(url)
        entries.append({
            "url": url,
            "title": title,
            "source_name": _tag(block, "news:name") or DEFAULT_SOURCE_NAME,
            "published_at": published_raw[:10],
            "published_at_raw": published_raw,
            "category": category.group(1).strip() if category else None,
            "subcategory": category.group(2).strip() if category else None,
            "channel": channel.group(1) if channel else None,
        })
    return entries


def _excerpt(url):
    """文章頁的 og:description。抓不到回空字串，不讓一篇的失敗擋住整批。"""
    soup = make_soup(_get(url))
    meta = soup.find("meta", {"property": "og:description"}) or soup.find(
        "meta", {"name": "description"})
    return (meta.get("content") or "").strip() if meta else ""


def get_media_articles(sleep_seconds=0.4):
    """抓元氣網 Google News sitemap 的全部條目，並補上摘要。"""
    print(f"\n[健康媒體] 開始抓取: {GNEWS_INDEX_URL}")
    index = _get(GNEWS_INDEX_URL).text
    pages = re.findall(r"<sitemap>\s*<loc>(.*?)</loc>", index)

    entries, seen = [], set()
    for page in pages:
        for entry in parse_gnews_entries(_get(page).text):
            if entry["url"] not in seen:
                seen.add(entry["url"])
                entries.append(entry)

    failed = 0
    for entry in entries:
        try:
            entry["excerpt"] = _excerpt(entry["url"])
        except Exception as exc:
            entry["excerpt"] = ""
            failed += 1
            print(f"  ⚠️ 摘要抓取失敗（仍保留條目）: {entry['url']} —— {exc}")
        time.sleep(sleep_seconds)

    print(f"[健康媒體] 完成，{len(entries)} 篇（摘要失敗 {failed} 篇）")
    return entries


def upsert_media_articles(articles, collection):
    """以 url 為鍵 upsert。回傳新增篇數。

    重複執行是安全的：sitemap 是 48 小時窗，同一篇會連續出現在兩天的抓取裡，
    第二次只更新欄位（站方可能改標題或分類），`first_seen_at` 不動。
    """
    collection.create_index("url", unique=True)
    now = datetime.now(timezone.utc)
    inserted = 0
    for article in articles:
        result = collection.update_one(
            {"url": article["url"]},
            {"$set": {**article, "fetched_at": now},
             "$setOnInsert": {"first_seen_at": now}},
            upsert=True,
        )
        if result.upserted_id is not None:
            inserted += 1
    return inserted


def media_job(*, fetch=None, collection_factory=None):
    """一次媒體 ETL。回傳 0 正常、1 異常。

    異常的定義與 `main_pipeline.find_missing_sources` 同一個判斷：**一篇都沒抓到
    就是故障**。sitemap 是 48 小時窗、這個頻道一天十幾篇，窗口裡是空的幾乎只可能
    是站方改版或網路／憑證問題。刻意不設數量門檻——自然波動會產生假警報。
    """
    fetch = fetch or get_media_articles
    try:
        articles = fetch()
    except Exception as exc:
        print(f"❌ 嚴重：健康媒體抓取失敗: {exc}")
        return 1
    if not articles:
        print("❌ 嚴重：健康媒體本次一篇都沒有取得。站方可能改版，請檢查 sitemap 格式。")
        return 1
    try:
        collection = collection_factory()
        inserted = upsert_media_articles(articles, collection)
    except Exception as exc:
        print(f"❌ 嚴重：健康媒體寫入失敗: {exc}")
        return 1
    print(f"[健康媒體] 寫入完成：新增 {inserted} 篇、更新 {len(articles) - inserted} 篇")
    return 0


if __name__ == "__main__":
    for a in get_media_articles()[:5]:
        print(a["published_at"], f"[{a['category']}/{a['subcategory']}]", a["title"][:40])
