# scraper_mohw.py
"""衛福部「真相說明」爬蟲（https://www.mohw.gov.tw/lp-4343-1.html）。

這是跨機關的闢謠彙整頁，從 102 年累積至今，是台灣官方闢謠內容最完整的單一
入口。它本身不放內文，每一列連到發布機關自己的網站——因此這支爬蟲的主要工作
不是解析一種頁面，而是**依連結指向的網域分派給不同的明細頁解析器**。

2026-09-09 實測全部 54 頁、1,023 筆的組成：

| 網域 | 筆數 | 處理 |
| --- | --- | --- |
| `mohw.gov.tw/cp-4343-*` | 445 | 解析（衛福部真相說明） |
| `hpa.gov.tw/Pages/Detail.aspx` | 341 | 解析（國健署真相與闢謠） |
| `fda.gov.tw` | 199 | **排除**，見下方 |
| `cdc.gov.tw/Bulletin/Detail/*` | 24 | 解析（疾管署闢謠專區） |
| `nhi.gov.tw` | 14 | **排除**，見下方 |

為什麼排除 `fda.gov.tw`
-----------------------
不是因為「內容重複就算了」，而是因為**去重抓不到它**。ETL 以 url 字串為鍵，
而真相說明上的食藥署連結是 `http://` + 小寫 `/tc/` + utm 參數：

    http://www.fda.gov.tw/tc/newsContent.aspx?cid=5049&id=31601&utm_source=rss&...

`scraper_fda` 產生的是 `https://www.fda.gov.tw/TC/newsContent.aspx?cid=5049&id=31601`。
兩者不相等，跟著爬會讓同一篇在知識庫裡存兩份，各自佔用 embedding 額度、各自
出現在檢索結果裡。

為什麼排除 `nhi.gov.tw`
-----------------------
健保署站台對程式化請求回 **403（WAF）**，帶完整瀏覽器 headers（UA、Accept、
Accept-Language、Referer、Upgrade-Insecure-Requests）重試仍是 403。要繞過得
處理 session cookie 或改用瀏覽器驅動，成本與這 14 筆的價值不相稱。

**這是一個明確的取捨決定，不是遺漏。** 原始設計的網域表根本沒有列出 nhi
（表列 4 個網域共 1,009 筆，實測是 5 個網域共 1,023 筆，差的正好是這 14 筆），
若沿用「其他一律排除」的預設，這 14 筆會被靜默丟掉而沒有人知道。未知網域因此
一律**排除並記 log**：站方日後新增轉載機關時，log 是唯一會讓我們知道的訊號。

日期為什麼只從列表頁取
----------------------
列表頁已經提供標題、發布日期與連結三者，明細頁只用來取內文。理由是日期：三個
站的明細頁日期呈現各不相同（本部內文裡有多個民國日期——事件發生日、函文日、
公告日，抽到哪一個取決於正則寫法；國健署同時有發布與更新日期；疾管署又是另一
套）。**列表頁的日期是同一個欄位、同一種格式（民國 `115-09-01`），跨三個站
一致。** 用列表頁的日期，等於把「日期從哪來」收斂成一個地方，而不是三個各自
可能抽錯的解析器。

404 不得當成「文章已下架」
--------------------------
本專案在這批政府站台上已知的失效形式至少五種：`hpa` 的 SSLError／403／
ConnectTimeout、`fda` 在被打快時回的**假 404**（不是 429）、`cdc` 偶發 TLS
握手失敗、`mohw` 的 SSLError。結論是**任何單次探測都不足以判定一篇文章消失
了**，因此本爬蟲不實作「404 就標記下架」——抓不到就是本次跳過、記 log、下次
再試，既有文件不因抓取失敗而被刪除。
"""
import re
import time

import requests
from bs4 import BeautifulSoup

from ca_bundle import get_ca_bundle
from utils import clean_html, make_soup

BASE = "https://www.mohw.gov.tw"
# 第 n 頁；每頁 20 筆。第 1 頁 `lp-4343-1-1-20.html` 與 `lp-4343-1.html` 同內容。
LIST_URL_TEMPLATE = f"{BASE}/lp-4343-1-{{page}}-20.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# 列表列尾端的民國日期，例如「…不會產生危害 115-09-01」。
_LIST_DATE_RE = re.compile(r"(\d{2,3})-(\d{2})-(\d{2})\s*$")

# 網域 → (來源名, 標題選擇器, 內文選擇器)。三組選擇器都在 2026-09-09 對真實
# 頁面驗證過。`cdc` 的內文容器同時包住標題與頁面控制項，由 _parse_cdc 另外處理。
_DISPATCH = {
    "www.mohw.gov.tw": ("衛福部真相說明", "h2", ".cp"),
    "www.hpa.gov.tw": ("國健署真相與闢謠", "h3", ".htmlBlock"),
    "www.cdc.gov.tw": ("疾管署闢謠專區", "h2", ".news-v3-in"),
}

# 明確排除、且**不記為未知網域**的網域。與「未知網域」分開處理：未知網域的
# log 是要讓人去看的訊號，已決定排除的網域每天刷同一行只會讓那個訊號被埋掉。
_EXCLUDED = {
    "www.fda.gov.tw": "與 scraper_fda 重複，且 url 形式不同導致去重失效",
    "www.nhi.gov.tw": "健保署站台對程式化請求回 403（WAF），繞過成本不相稱",
}

# 疾管署內文容器裡的頁面控制項，取內文前先剝掉。
#
# 順序有意義：括號那串要先整個拿掉，否則先剝「回上一頁」會在原地留下
# `( alt + ← )` 這種殘骸——比沒剝還難看，而且會跟著進向量化的輸入。
_CDC_CHROME_RE = re.compile(r"\(\s*alt\s*\+\s*←[^)]*\)")
_CDC_CHROME = ("取得短網址", "回上一頁", "關閉", "複製")


def roc_to_gregorian(raw):
    """民國 `115-09-01` → 西元 `2026-09-01`。已是西元格式者原樣回傳。

    三位數（含）以下視為民國年：西元年不可能是三位數，因此這個判準沒有歧義。
    在爬蟲端轉換而不是留給下游，是因為既有三個來源寫進 `published_at` 的都是
    西元格式——Tier 2 選材會依這個欄位做字串排序，混入別種格式會讓排序失去
    意義（`-` 是 0x2D、`/` 是 0x2F，字串比大小時格式會蓋過日期本身）。
    """
    if not raw:
        return None
    parts = raw.split("-")
    if len(parts) != 3:
        return None
    year, month, day = parts
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return None
    if len(year) <= 3:
        year = str(int(year) + 1911)
    return f"{year}-{month}-{day}"


def _get(url, timeout=25):
    resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=get_ca_bundle())
    resp.raise_for_status()
    return resp


# 重試的等待秒數：第 1、2 次失敗後各等多久。共 3 次嘗試。
_RETRY_BACKOFFS = (2, 5)


def with_retries(fn, *, attempts=3, backoffs=_RETRY_BACKOFFS, sleep=time.sleep):
    """呼叫 fn，遇到暫時性失敗就退避重試。純函式（sleep 可注入），可直接測。

    為什麼需要：2026-09-13 GitHub Actions 那次，`mohw.gov.tw` 在第 5 頁列表直接
    斷線（`RemoteDisconnected`），同一輪另有 4 篇明細也是同樣的錯誤，其餘 67 篇
    正常——是間歇性的，不是整個被擋。當時沒有重試，一次斷線就讓那一輪只進 67 篇
    （本機實測可分派約 810 篇）。

    只重試「換個時間再來可能會好」的失敗：連線錯誤、逾時、429 與 5xx。其餘 4xx
    （包括 404）不重試——重打同一個 404 只是多等十幾秒，而且 404 本來就不代表
    下架（見模組 docstring）。
    """
    for attempt in range(attempts):
        try:
            return fn()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == attempts - 1:
                raise
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            retryable = status is not None and (status == 429 or status >= 500)
            if not retryable or attempt == attempts - 1:
                raise
        sleep(backoffs[min(attempt, len(backoffs) - 1)])


def _host(url):
    match = re.match(r"https?://([^/]+)", url or "")
    return match.group(1).lower() if match else ""


def _list_rows(page):
    """回傳某一頁列表的 (url, title, published_at) 三元組，保持頁面順序。"""
    url = LIST_URL_TEMPLATE.format(page=page)
    soup = make_soup(with_retries(lambda: _get(url)))
    rows = []
    for item in soup.select(".list li"):
        anchor = item.find("a", href=True)
        if anchor is None:
            continue
        text = item.get_text(" ", strip=True)
        match = _LIST_DATE_RE.search(text)
        if match is None:
            # 日期是必要欄位（Tier 1 的時效門檻靠它，抽不到一律排除）。這裡
            # 同樣不放行：沒有日期的列多半代表版面改了，該被看見而不是被塞
            # 一個猜測的日期進知識庫。
            print(f"  ⚠️ 列表列取不到日期，跳過: {text[:40]}")
            continue
        rows.append((
            anchor["href"],
            text[:match.start()].strip(),
            roc_to_gregorian(match.group(0)),
        ))
    return rows


def _parse_cdc(soup, body_selector):
    """疾管署的內文容器同時包住標題與頁面控制項，需要另外剝。"""
    body = soup.select_one(body_selector)
    if body is None:
        return ""
    for tag in body.find_all(["h1", "h2", "h3"]):
        tag.decompose()
    text = _CDC_CHROME_RE.sub(" ", clean_html(str(body)))
    for chrome in _CDC_CHROME:
        text = text.replace(chrome, " ")
    # 「發佈日期：2021-06-13」也在容器內，但日期一律以列表頁為準（見模組
    # docstring），這裡只是把它從內文裡拿掉，不拿它當日期用。
    text = re.sub(r"發佈日期[：:]\s*\d{4}-\d{2}-\d{2}", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_detail(url):
    """抓單篇明細頁。解析不出標題或內文就回 None（由呼叫端計數）。"""
    host = _host(url)
    source_name, title_selector, body_selector = _DISPATCH[host]
    soup = make_soup(with_retries(lambda: _get(url)))
    for tag in soup(["script", "style"]):
        tag.decompose()

    heading = soup.select_one(title_selector)
    title = heading.get_text(" ", strip=True) if heading else ""

    if host == "www.cdc.gov.tw":
        content = _parse_cdc(soup, body_selector)
    else:
        body = soup.select_one(body_selector)
        content = clean_html(str(body)) if body else ""

    if not title or not content:
        return None
    return {"title": title, "content": content, "source": source_name}


# 連續幾頁列表在重試後仍失敗，就判定站台這一輪不可用、停止翻頁。
_MAX_CONSECUTIVE_LIST_FAILURES = 3


def get_mohw_articles(test_mode=False, max_pages=200, sleep_seconds=0.4, *,
                      list_rows=None, parse_detail=None, sleep=time.sleep):
    """爬真相說明彙整頁，回傳與其他 scraper 相同格式的 dict list。

    :param test_mode: True 時只抓第一頁的前 3 篇。
    :param max_pages: 翻頁上限（防呆）。**不寫死 54**——那是 2026-09-09 的實測
        值，站方增刪內容時會變。正常會在連續兩頁沒有新連結時自己停。
    :param list_rows / parse_detail / sleep: 測試用的注入點，預設為正式的
        網路抓取與 time.sleep。
    """
    list_rows = list_rows or _list_rows
    parse_detail = parse_detail or _parse_detail
    print(f"\n[真相說明] 開始爬取列表: {LIST_URL_TEMPLATE.format(page=1)}")

    # 先蒐集全部列再抓內文，這樣「翻頁到底」的判斷不會跟內文失敗混在一起。
    # test_mode 只翻第一頁：否則「抓前 3 篇」還是要先跑完 54 頁的列表，
    # 冒煙測試就得等半分鐘。
    page_limit = 1 if test_mode else max_pages
    rows, seen_urls, empty_streak, failure_streak = [], set(), 0, 0
    for page in range(1, page_limit + 1):
        try:
            page_rows = list_rows(page)
        except Exception as exc:
            # 重試後仍失敗：跳過這一頁、繼續翻，**不整批中止**。以前這裡是
            # break，一次斷線就丟掉後面所有頁。連續失敗才判定站台這輪不可用——
            # 否則站台整個掛掉時，會對剩下的上百頁每頁各重試一輪。
            failure_streak += 1
            print(f"  第 {page} 頁列表重試後仍失敗，跳過（連續第 {failure_streak} 頁）: {exc}")
            if failure_streak >= _MAX_CONSECUTIVE_LIST_FAILURES:
                print("  連續多頁失敗，判定站台這一輪不可用，停止翻頁。")
                break
            continue
        failure_streak = 0

        new_rows = [r for r in page_rows if r[0] not in seen_urls]
        for row in new_rows:
            seen_urls.add(row[0])
        rows.extend(new_rows)

        if not new_rows:
            # 翻過頭時站方不會回 404，而是重複回同一頁，因此以「沒有新連結」
            # 為終止條件。要求連續兩頁，是因為單頁全部重複也可能只是站方在
            # 那一刻插入了新文章造成的位移。
            empty_streak += 1
            if empty_streak >= 2:
                print(f"  第 {page} 頁起連續兩頁沒有新連結，翻頁結束。")
                break
        else:
            empty_streak = 0
        sleep(sleep_seconds)

    print(f"  列表共 {len(rows)} 筆，開始分派")

    articles, skipped, failed = [], {}, 0
    for url, title, published_at in rows:
        host = _host(url)
        if host in _EXCLUDED or host not in _DISPATCH:
            skipped[host] = skipped.get(host, 0) + 1
            continue
        try:
            detail = parse_detail(url)
        except Exception as exc:
            failed += 1
            print(f"  ⚠️ 明細抓取失敗，本次跳過（不視為下架）: {url} —— {exc}")
            continue
        if detail is None:
            failed += 1
            print(f"  ⚠️ 明細解析不出標題或內文，跳過: {url}")
            continue

        articles.append({
            # 標題以列表頁為準：明細頁的標題偶有前後綴，列表頁的是站方自己的
            # 索引標題，跨三個站一致。明細頁解析出的標題只用來確認頁面對得上。
            "title": title or detail["title"],
            "content": detail["content"],
            "source": detail["source"],
            "url": url,
            "published_at": published_at,
            # 三個站的明細頁都沒有可靠的「修改日期」欄位，比照食藥署公告與 TFC
            # 視為不可偵測改版（ETL 對這類來源維持「已存在即跳過」）。
            "updated_at": None,
        })
        if test_mode and len(articles) >= 3:
            break
        sleep(sleep_seconds)

    for host, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
        reason = _EXCLUDED.get(host)
        if reason:
            print(f"  ⏭️  排除 {host}: {count} 筆（{reason}）")
        else:
            # 未知網域要吵。站方新增轉載機關時，這是唯一的訊號。
            print(f"  ❓ 未知網域 {host}: {count} 筆被跳過——請確認是否要新增解析器")

    print(f"[真相說明] 完成，取得 {len(articles)} 篇（失敗 {failed} 篇）")
    return articles


if __name__ == "__main__":
    for article in get_mohw_articles(test_mode=True):
        print(article["published_at"], article["source"], article["title"][:40])
