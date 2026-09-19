#!/usr/bin/env python3
"""MyGoPen（麥擱騙）：從官方 feed 取判定與主張。

MyGoPen 是台灣第二個通過 IFCN 認證的查核組織（2020 首次，2026-07 第五度），
2015 年由工程師創立，起因就是想幫家裡長輩闢謠——跟 CARE 想解決的是同一件事。
站上 4,964 篇文章的標題自帶判定前綴（【錯誤】【部分錯誤】【易生誤解】…），
與 TFC 舊站同一種慣例，所以判定不必讀內文就抽得出來。

### 只存標題與連結，不存內文

**這是授權考量，不是技術取捨。** MyGoPen 由識實數據有限公司經營，站上沒有
任何開放授權聲明（2026-09-19 查過 robots.txt 與 25 個靜態頁，都沒有使用條款
或授權頁），與 Cofacts 的 CC BY-SA 完全不同。在拿到明確授權之前：

- 只取**標題**（判定＋主張）與**原文連結**，不抓、不存內文。
- 判定卡上一律附連結，把讀者帶回他們的網站。

所以這個來源的 `content` 就是那一句主張本身。對查核比對夠用——比對打的正是
主張的向量；使用者要看理由就點連結。

### 取得方式

走 Blogspot 官方 feed（`/feeds/posts/default`），不解析網頁。robots.txt 只擋
`/search` 與 `/share-widget`，feed 不在其中。

### 還沒接進 ETL

`main_pipeline` 目前**沒有**呼叫這支。要不要收、以及要不要去信取得授權，是
James 的決定（2026-09-19 待確認）。確認後在 `job()` 的 fetchers 加一行、
並把 `MyGoPen` 列進 `EXPECTED_SOURCES` 即可。
"""
import re
import time

import requests


SOURCE_NAME = "MyGoPen"
FEED_URL = "https://www.mygopen.com/feeds/posts/default"
PAGE_SIZE = 150

# 標題前綴 → 判定。對照表與 scraper_tfc.LEGACY_PREFIX_VERDICT 同一套詞彙
# （CARE 的 matcher 只認那五個值），MyGoPen 特有的前綴依語意歸併：
#   易生誤解／部分錯誤 → 部分錯誤
#   詐騙／假借冠名     → 錯誤（訊息本身即為捏造）
#   事實釐清／背景補充 → 事實釐清
PREFIX_VERDICT = {
    "錯誤": ("錯誤", "incorrect"),
    "假訊息": ("錯誤", "incorrect"),
    "詐騙": ("錯誤", "incorrect"),
    "假借冠名": ("錯誤", "incorrect"),
    "部分錯誤": ("部分錯誤", "partially-incorrect"),
    "易生誤解": ("部分錯誤", "partially-incorrect"),
    "正確": ("正確", "correct"),
    "事實釐清": ("事實釐清", "fact-clarification"),
    "背景補充": ("事實釐清", "fact-clarification"),
    "證據不足": ("證據不足", "insufficient-evidence"),
}

_PREFIX_RE = re.compile(r"^【([^】]{2,6})】\s*")
# 主張句尾常見的問號與「？」；留著會讓向量多一個無意義的共同尾綴。
_TAIL_RE = re.compile(r"[?？!！\s]+$")

_HEADERS = {
    "User-Agent": "CARE-health-assistant/1.0 (+https://github.com/Yanagi-0912/CARE)",
}


def parse_title(title: str):
    """把「【錯誤】網傳⋯？」拆成 (判定, slug, 主張)；認不得的前綴回 None。"""
    matched = _PREFIX_RE.match(title or "")
    if not matched:
        return None
    verdict = PREFIX_VERDICT.get(matched.group(1).strip())
    if verdict is None:
        return None
    claim = _TAIL_RE.sub("", title[matched.end():]).strip()
    return (*verdict, claim) if claim else None


def _entry_to_article(entry):
    parsed = parse_title(entry.get("title", {}).get("$t", ""))
    if parsed is None:
        return None
    verdict, slug, claim = parsed
    url = next(
        (l["href"] for l in entry.get("link", []) if l.get("rel") == "alternate"), ""
    )
    if not url:
        return None
    return {
        "title": claim[:120],
        # 只放主張，不放內文——授權考量，見模組說明。
        "content": claim,
        "source": SOURCE_NAME,
        "url": url,
        "published_at": (entry.get("published", {}).get("$t") or "")[:10] or None,
        "updated_at": None,
        "verdict": verdict,
        "verdict_slug": slug,
        "claim": claim,
    }


def get_mygopen_articles(*, test_mode=False, max_pages=50, sleep=time.sleep, fetch=None):
    """從官方 feed 取全部可辨識判定的文章。`fetch` 是測試用的注入點。"""
    fetch = fetch or _fetch_page
    articles, seen = [], set()

    for page in range(max_pages):
        start = page * PAGE_SIZE + 1
        try:
            feed = fetch(start)
        except Exception as exc:  # noqa: BLE001 - 單頁失敗就停，不讓整條 ETL 掛掉
            print(f"  ⚠️ MyGoPen feed 第 {start} 筆起失敗，停止翻頁：{exc}")
            break
        entries = feed.get("entry") or []
        if not entries:
            break
        for entry in entries:
            article = _entry_to_article(entry)
            if article and article["url"] not in seen:
                seen.add(article["url"])
                articles.append(article)
        if test_mode and articles:
            return articles[:3]
        sleep(0.5)

    print(f"[MyGoPen] 完成，取得 {len(articles)} 篇（只取標題與連結，不取內文）")
    return articles


def _fetch_page(start_index):
    response = requests.get(
        FEED_URL,
        params={"alt": "json", "max-results": PAGE_SIZE, "start-index": start_index},
        headers=_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("feed", {})


if __name__ == "__main__":
    rows = get_mygopen_articles(test_mode=True)
    for row in rows:
        print(f"[{row['verdict']}] {row['claim'][:60]}")
        print(f"    {row['url']}")
