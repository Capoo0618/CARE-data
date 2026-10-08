"""疾管署疾病介紹：從官方傳染病索引探索疾病的中文介紹與預防衛教。

只跟隨疾病專頁「疾病資訊」區的疾病介紹卡片。Category/Page 的側欄包含其他
疾病、新聞與教材，掃描整頁連結會混收。正文、標題、日期是同一 col-md-9 下的
兄弟節點，不能沿用 scraper_mohw 的 Bulletin/Detail（闢謠專區）解析器。

沿用既有文章 dict 與 updated_at 改版判定；不推測發布日期、不以抓取失敗判定
下架。沒有日期的介紹仍有衛教價值，比照既有來源保留 None。此來源不提供查核
判定，也不在 claim_tagger 的來源白名單內。
"""
import re
import time
from datetime import date
from urllib.parse import urljoin, urlsplit

import requests

from ca_bundle import get_ca_bundle
from scraper_mohw import HEADERS, with_retries
from utils import clean_html, make_soup

SOURCE_NAME = "疾管署疾病介紹"
BASE = "https://www.cdc.gov.tw"
INDEX_URL = f"{BASE}/Disease/Index"


def _official_url(raw, path_prefix):
    """只接受指定類型的中文官方頁；追蹤參數與 fragment 不參與去重。"""
    try:
        parts = urlsplit(urljoin(BASE + "/", raw or ""))
        if (parts.scheme not in ("https", "http")
                or parts.hostname not in ("www.cdc.gov.tw", "cdc.gov.tw")
                or parts.username or parts.password or parts.port is not None
                or not re.fullmatch(re.escape(path_prefix) + r"[A-Za-z0-9_-]+/?", parts.path)):
            return None
    except ValueError:
        return None
    return BASE + parts.path.rstrip("/")


def parse_disease_index(soup):
    """回傳 (疾病專頁網址, 疾病名稱)，分類／注音索引重複時保留第一筆。"""
    rows, seen = [], set()
    for anchor in soup.select(".infectious_disease_ul a[href]"):
        url = _official_url(anchor["href"], "/Disease/SubIndex/")
        name = anchor.get_text(" ", strip=True)
        if url and name and url not in seen:
            seen.add(url)
            rows.append((url, name))
    return rows


def parse_intro_links(soup):
    """只取疾病資訊區的「疾病介紹」，不跟隨側欄、新聞、教材或附件。"""
    urls, seen = [], set()
    for anchor in soup.select(".infectious_disease_box .disease .disease-heading a[href]"):
        if anchor.get_text(strip=True) != "疾病介紹":
            continue
        url = _official_url(anchor["href"], "/Category/Page/")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _date_for(text, label):
    """僅解析標示的文章日期；民國／西元、斜線／連字號皆轉成 ISO 日期。"""
    match = re.search(label + r"\s*[：:]?\s*(\d{2,4})[/-](\d{1,2})[/-](\d{1,2})(?!\d)", text)
    if match is None:
        return None
    year, month, day = map(int, match.groups())
    if len(match.group(1)) <= 3:
        year += 1911
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _body_text(body):
    """保留段落／列表邊界與表格文字，不改動其他來源的共用清洗規則。"""
    for tag in body.select("script, style, noscript, .social-all, .modal, button"):
        tag.decompose()
    for br in body.find_all("br"):
        br.replace_with("\n")
    for cell in body.find_all(["td", "th"]):
        cell.append(" | ")
    for block in body.find_all(["p", "li", "tr", "div", "h3", "h4", "h5", "h6"]):
        block.append("\n")
    # get_text 不在每個 inline tag 間塞空白，避免拆散疾病名與數字。
    return "\n".join(line for raw in body.get_text().splitlines() if (line := clean_html(raw)))


def parse_disease_detail(soup, disease_name, url):
    """回傳可交給既有 ETL 的文章；版面或正文不符時回 None，絕不抓整頁補救。"""
    canonical = _official_url(url, "/Category/Page/")
    heading = soup.select_one(".col-md-9 > .news-v3 > h2.con-title")
    if (not canonical or not disease_name.strip() or heading is None
            or heading.get_text(strip=True) != "疾病介紹"):
        return None
    root = heading.parent.parent
    body = root.select_one(".m-t-30.m-b-30")
    if body is None:
        return None
    content = _body_text(body)
    if not content:
        return None
    dates = " ".join(node.get_text(" ", strip=True) for node in root.select(".date"))
    return {
        "title": f"{disease_name.strip()}－疾病介紹",
        "content": content,
        "source": SOURCE_NAME,
        "url": canonical,
        "published_at": _date_for(dates, r"(?:發布|發佈)日期"),
        "updated_at": _date_for(dates, r"(?:最後)?更新日期"),
    }


def _get(url):
    return requests.get(url, headers=HEADERS, timeout=25, verify=get_ca_bundle())


def get_cdc_articles(test_mode=False, sleep_seconds=0.4, *, get=None, sleep=time.sleep):
    """每日重抓索引與介紹；未改版由既有 ETL 跳過，不重複呼叫 embedding。

    get／sleep 為離線測試注入點。重試沿用真相說明爬蟲的 3 次與 2／5 秒退避；
    單疾病／明細失敗仍處理其餘疾病，整個來源無產出由 EXPECTED_SOURCES 報錯。
    test_mode 最多取得 3 篇成功文章，不做向量化或資料庫寫入。
    """
    get = get or _get

    def fetch_soup(url):
        sleep(sleep_seconds)

        def fetch():
            response = get(url)
            response.raise_for_status()
            return make_soup(response)

        return with_retries(fetch, sleep=sleep)

    print(f"\n[{SOURCE_NAME}] 開始爬取索引: {INDEX_URL}")
    try:
        diseases = parse_disease_index(fetch_soup(INDEX_URL))
    except Exception as exc:
        print(f"  ❌ 疾病索引抓取失敗: {type(exc).__name__}: {exc}")
        return []

    articles, seen, failed, skipped = [], set(), 0, 0
    for subindex_url, disease_name in diseases:
        try:
            urls = parse_intro_links(fetch_soup(subindex_url))
        except Exception as exc:
            failed += 1
            print(f"  ⚠️ 疾病專頁抓取失敗，本次跳過: {subindex_url} —— {exc}")
            continue
        if not urls:
            skipped += 1
            print(f"  ⚠️ 找不到中文疾病介紹入口，本次跳過: {disease_name} {subindex_url}")
        for url in urls:
            if url in seen:
                continue
            # 成功才記去重；不同專頁共用介紹且首次抓失敗時，仍能再次嘗試。
            try:
                article = parse_disease_detail(fetch_soup(url), disease_name, url)
                if article is None:
                    raise ValueError("疾病介紹標題或正文解析失敗")
            except Exception as exc:
                failed += 1
                print(f"  ⚠️ 明細抓取／解析失敗，本次跳過（不視為下架）: {url} —— {exc}")
                continue
            seen.add(url)
            articles.append(article)
            if test_mode and len(articles) >= 3:
                break
        if test_mode and len(articles) >= 3:
            break

    print(f"[{SOURCE_NAME}] 完成，索引 {len(diseases)} 個疾病，取得 {len(articles)} 篇"
          f"（失敗 {failed} 筆、無介紹入口 {skipped} 筆）")
    return articles


if __name__ == "__main__":
    for article in get_cdc_articles(test_mode=True):
        print(article["title"], article["updated_at"], article["url"])
