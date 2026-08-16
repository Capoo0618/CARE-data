# scraper_fda.py
"""食藥署「食藥闢謠專區」爬蟲（https://www.fda.gov.tw/TC/news.aspx?cid=5049）。

為什麼不用 API：食藥署的 `DataAction` 端點回傳的是**全站新聞稿** feed，
不是闢謠專區。它的欄位只有 標題／內容／附檔連結／發布日期，既沒有文章網址，
內容也混入大量與衛教無關的行政公告（優良廚師表揚、HACCP 評鑑、研討會）。
闢謠專區只能從網頁列表爬，但換來三件 API 給不了的東西：

1. **每篇都有可點的網址** —— 闢謠 bot 要讓使用者能自己查證，這是必要的。
2. **維護日期** —— 詳細頁同時有發布與維護日期，食藥署因此也能偵測改版，
   不必再退回「已存在即跳過」。
3. **內容真的是闢謠** —— 來源與 source_name 標示終於一致。
"""
import re
import time

import requests
from bs4 import BeautifulSoup

from ca_bundle import get_ca_bundle
from utils import clean_html

SOURCE_NAME = "食藥署闢謠專區"
BASE = "https://www.fda.gov.tw/TC"
LIST_URL = f"{BASE}/news.aspx?cid=5049"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
}

# h3 內是「標題| 發布日期：YYYY-MM-DD　| 維護日期：YYYY-MM-DD」
_PUBLISHED_RE = re.compile(r"發布日期[：:]\s*(\d{4}-\d{2}-\d{2})")
_UPDATED_RE = re.compile(r"維護日期[：:]\s*(\d{4}-\d{2}-\d{2})")
_LINK_RE = re.compile(r"newsContent\.aspx\?cid=5049&id=(\d+)")


def _get(url, timeout=20):
    resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=get_ca_bundle())
    resp.raise_for_status()
    return resp


def _article_ids(page: int) -> list[str]:
    """回傳某一頁列表上的文章 id（保持頁面順序、去重）。"""
    resp = _get(f"{LIST_URL}&pn={page}")
    ids, seen = [], set()
    for aid in _LINK_RE.findall(resp.text):
        if aid not in seen:
            seen.add(aid)
            ids.append(aid)
    return ids


def _parse_detail(article_id: str) -> dict | None:
    """抓單篇詳細頁。解析不出標題或內容就回 None（由呼叫端計數）。"""
    url = f"{BASE}/newsContent.aspx?cid=5049&id={article_id}"
    soup = BeautifulSoup(_get(url).content, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    # 標題與日期同在 h3，格式「標題| 發布日期：…　| 維護日期：…」
    heading = soup.find("h3")
    if heading is None:
        return None
    raw_heading = heading.get_text(" ", strip=True)
    title = raw_heading.split("|")[0].strip()

    published = _PUBLISHED_RE.search(raw_heading)
    updated = _UPDATED_RE.search(raw_heading)

    body = soup.select_one(".marginBot")
    content = clean_html(str(body)) if body else ""

    if not title or not content:
        return None

    return {
        "title": title,
        "content": content,
        "source": SOURCE_NAME,
        "url": url,
        "published_at": published.group(1) if published else None,
        "updated_at": updated.group(1) if updated else None,
    }


def get_fda_articles(test_mode=False, max_pages=200, sleep_seconds=0.4):
    """爬闢謠專區。

    :param test_mode: True 時只抓第一頁的前 3 篇。
    :param max_pages: 翻頁上限（防呆；正常會在連續 3 頁沒有新 id 時自己停）。
    :return: 與其他 scraper 相同格式的 dict list。
    """
    print(f"\n[{SOURCE_NAME}] 開始爬取列表: {LIST_URL}")

    # 先蒐集全部 id 再抓內文，這樣「翻頁到底」的判斷不會跟內文失敗混在一起。
    ids: list[str] = []
    seen: set[str] = set()
    empty_streak = 0
    for page in range(1, max_pages + 1):
        try:
            page_ids = _article_ids(page)
        except Exception as exc:
            print(f"  第 {page} 頁列表抓取失敗: {exc}")
            break

        new = [i for i in page_ids if i not in seen]
        seen.update(new)
        ids.extend(new)

        # 這個網站對超出範圍的 pn 會一直回同一頁，不會 404，
        # 所以用「連續數頁都沒有新 id」當終止條件。
        empty_streak = empty_streak + 1 if not new else 0
        if empty_streak >= 3:
            break
        if test_mode and len(ids) >= 3:
            break
        time.sleep(sleep_seconds)

    if test_mode:
        ids = ids[:3]
    print(f"  -> 列表共取得 {len(ids)} 篇文章 id")

    articles, failed = [], 0
    for idx, aid in enumerate(ids, start=1):
        try:
            art = _parse_detail(aid)
        except Exception as exc:
            failed += 1
            print(f"  [{idx}/{len(ids)}] id={aid} 抓取失敗: {exc}")
            continue
        if art is None:
            failed += 1
            print(f"  [{idx}/{len(ids)}] id={aid} 解析不出標題或內容，略過")
            continue
        articles.append(art)
        if idx % 50 == 0:
            print(f"  [{idx}/{len(ids)}] 已取得 {len(articles)} 篇")
        time.sleep(sleep_seconds)

    print(f"  -> 成功清洗 {len(articles)} 篇資料（失敗 {failed} 篇）")
    return articles


# ==========================================
# 本地測試區塊
# ==========================================
if __name__ == "__main__":
    print("=== 測試 食藥署闢謠專區 爬蟲 ===")
    arts = get_fda_articles(test_mode=True)
    print(f"\n=== 取得 {len(arts)} 篇 ===")
    for i, a in enumerate(arts, 1):
        print(f"\n[{i}] {a['title']}")
        print(f"    url          : {a['url']}")
        print(f"    published_at : {a['published_at']}")
        print(f"    updated_at   : {a['updated_at']}")
        print(f"    content({len(a['content'])}字): {a['content'][:120]}…")
