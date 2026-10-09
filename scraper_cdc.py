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
import hashlib
import json
from datetime import datetime, timezone
from datetime import date
from dataclasses import dataclass
import argparse
import sys

import requests

from scraper_mohw import HEADERS
from utils import clean_html, make_soup
from bs4 import BeautifulSoup
from cdc_access import CDCAccess, AccessDenied, RequestLimit, normalize_url, _get

SOURCE_NAME = "疾管署疾病介紹"
BASE = "https://www.cdc.gov.tw"
INDEX_URL = f"{BASE}/Disease/Index"
LICENSE_NAME = "政府網站資料開放宣告"
LICENSE_URL = f"{BASE}/Category/FPage/TxkBIR9agw_IBRRmvn9TcQ"
LICENSE_BODY_SHA256 = "7677901e46ee2e1ecd1bd82ce60c550cac74a7998cdfd9de55ed3cf4937aafb3"


def content_hash(title, content):
    """只雜湊已清理的標題／正文，時間、授權及其他中繼資料不參與更新判定。"""
    payload = json.dumps([title, content], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _official_url(raw, path_prefix):
    return normalize_url(raw, path_prefix)


def _soup(html):
    return BeautifulSoup(html, "html.parser") if isinstance(html, (str, bytes)) else html


class RestrictedContent(AccessDenied):
    pass


def parse_disease_index(soup):
    """回傳 (疾病專頁網址, 疾病名稱)，分類／注音索引重複時保留第一筆。"""
    soup = _soup(soup)
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
    soup = _soup(soup)
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
    soup = _soup(soup)
    canonical = _official_url(url, "/Category/Page/")
    heading = soup.select_one(".col-md-9 > .news-v3 > h2.con-title")
    if (not canonical or not disease_name.strip() or heading is None
            or heading.get_text(strip=True) != "疾病介紹"):
        return None
    root = heading.parent.parent
    body = root.select_one(".m-t-30.m-b-30")
    if body is None:
        return None
    # 只判斷正文／文章範圍的明確權利提示，不以全站頁尾 Copyright 排除官方原文。
    notices = body.get_text(" ", strip=True) + " " + " ".join(
        n.get_text(" ", strip=True) for n in root.select(".copyright, .license, [data-license]"))
    if re.search(r"(?:本文|本文章|本篇|本圖|本表|本素材).{0,25}(?:轉載自|著作權歸|版權歸)|"
                 r"未經.{0,15}(?:不得|禁止)(?:轉載|重製)|禁止轉載|第三方授權|專人專案撰文|"
                 r"須經(?:本署|本機關|著作權人|權利人).{0,10}同意方可使用|(?:禁止|不得)(?:轉載|重製|改作)|"
                 r"(?:本文|本篇|本素材)(?:版權所有)?[，,:；。\s]*(?:禁止|不得)使用", notices):
        raise RestrictedContent("正文有另行授權／第三方權利提示，待人工確認後才可收錄")
    content = _body_text(BeautifulSoup(str(body), "html.parser"))
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
        "content_type": "health_education",
        "language": "zh-TW",
        "jurisdiction": "臺灣",
        "license": LICENSE_NAME,
        "license_url": LICENSE_URL,
        "attribution": f"資料來源：衛生福利部疾病管制署；原文：{canonical}。CARE 擷取、清洗與切片。",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash(f"{disease_name.strip()}－疾病介紹", content),
    }


@dataclass
class CrawlResult:
    articles: list
    stats: dict


def crawl_cdc(*, max_articles=200, start_at=0, max_requests=500, sleep_seconds=0.4,
              get=None, sleep=time.sleep):
    """只抓公開 HTML；每輪確認 robots／授權，再在預算內探索中文疾病介紹。"""
    if not 1 <= max_articles <= 200 or start_at < 0:
        raise ValueError("max_articles 必須為 1..200，start_at 必須非負")
    access = CDCAccess(get=get, sleep=sleep, sleep_seconds=sleep_seconds, max_requests=max_requests)
    articles, seen = [], set()
    stats = {"success": 0, "excluded": 0, "failed": 0, "requests": 0,
             "catalog_diseases": 0, "next_offset": start_at, "limited": False, "exhausted": False, "exclusions": []}

    def fetch_soup(url):
        return make_soup(access.get(url))

    def excluded(url, reason):
        stats["excluded"] += 1
        stats["exclusions"].append({"url": url, "reason": reason})
        print(f"  ⏭️ 排除 {url}：{reason}", file=sys.stderr)

    try:
        access.load_robots()
        policy = fetch_soup(LICENSE_URL).select_one('.m-t-30.m-b-30')
        license_text = ''.join(policy.get_text().split()) if policy else ''
        if hashlib.sha256(license_text.encode('utf-8')).hexdigest() != LICENSE_BODY_SHA256:
            raise AccessDenied("授權必要條款無法確認，來源維持停用並待重新審查")
        diseases = parse_disease_index(fetch_soup(INDEX_URL))
        if not diseases:
            raise ValueError("疾病索引沒有有效入口，請確認官方版面")
        stats["catalog_diseases"] = len(diseases)
        stats["exhausted"] = start_at >= len(diseases)
        for offset, (subindex_url, disease_name) in enumerate(diseases[start_at:], start=start_at):
            try:
                urls = parse_intro_links(fetch_soup(subindex_url))
            except RequestLimit:
                stats["limited"] = True
                break
            except AccessDenied as exc:
                excluded(subindex_url, str(exc))
                stats["next_offset"] = offset + 1
                continue
            except Exception as exc:
                stats["failed"] += 1
                print(f"  ⚠️ 疾病專頁抓取失敗：{subindex_url}（{type(exc).__name__}）", file=sys.stderr)
                stats["next_offset"] = offset + 1
                continue
            if not urls:
                excluded(subindex_url, "沒有允許的中文疾病介紹入口")
            complete = True
            for intro_index, url in enumerate(urls):
                if url in seen:
                    continue
                try:
                    response = access.get(url)
                    article = parse_disease_detail(make_soup(response), disease_name, response.url or url)
                    if article is None:
                        raise ValueError("標題或正文解析失敗")
                except RequestLimit:
                    stats["limited"] = True
                    complete = False
                    break
                except AccessDenied as exc:
                    excluded(url, str(exc))
                    continue
                except Exception as exc:
                    stats["failed"] += 1
                    print(f"  ⚠️ 明細抓取／解析失敗：{url}（{type(exc).__name__}）", file=sys.stderr)
                    continue
                seen.add(url)
                if article["url"] in seen and article["url"] != url:
                    continue
                seen.add(article["url"])
                articles.append(article)
                if len(articles) >= max_articles:
                    complete = all(remaining in seen for remaining in urls[intro_index + 1:])
                    stats["limited"] = not complete or offset + 1 < len(diseases)
                    break
            stats["next_offset"] = offset + 1 if complete else offset
            if len(articles) >= max_articles or not complete:
                break
    except Exception as exc:
        stats["failed"] += 1
        print(f"  ❌ 來源前置檢查／索引失敗（{type(exc).__name__}）：{exc}", file=sys.stderr)
    stats["success"] = len(articles)
    stats["requests"] = access.requests
    print(f"[{SOURCE_NAME}] {json.dumps(stats, ensure_ascii=False)}", file=sys.stderr)
    return CrawlResult(articles, stats)


def get_cdc_articles(test_mode=False, sleep_seconds=0.4, *, get=None, sleep=time.sleep,
                     max_articles=200, start_at=0, max_requests=500):
    """每日重抓索引與介紹；未改版由既有 ETL 跳過，不重複呼叫 embedding。

    get／sleep 為離線測試注入點。重試沿用真相說明爬蟲的 3 次與 2／5 秒退避；
    單疾病／明細失敗仍處理其餘疾病，整個來源無產出由 EXPECTED_SOURCES 報錯。
    test_mode 最多取得 3 篇成功文章，不做向量化或資料庫寫入。
    """
    return crawl_cdc(max_articles=min(max_articles, 3) if test_mode else max_articles,
                     start_at=start_at, max_requests=max_requests, sleep_seconds=sleep_seconds,
                     get=get, sleep=sleep).articles


def main(argv=None, *, get=None, sleep=time.sleep, stdout=None):
    """預設即為小量預覽，永遠不載入 main_pipeline、DB client 或 embedding。"""
    parser = argparse.ArgumentParser(description="疾管署衛教 JSON 預覽（不向量化、不入庫）")
    parser.add_argument("--preview", action="store_true", help="輸出小量公開文章及授權 metadata")
    parser.add_argument("--limit", type=int, default=3, help="最多 1..5 篇，預設 3")
    parser.add_argument("--offset", type=int, default=0, help="疾病索引起點，供分批預覽")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 5 or args.offset < 0:
        parser.error("limit 必須為 1..5，offset 必須非負")
    result = crawl_cdc(max_articles=args.limit, start_at=args.offset, get=get, sleep=sleep)
    output = {"stats": result.stats, "articles": [{**a, "content_length": len(a["content"])} for a in result.articles]}
    print(json.dumps(output, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    return 0 if result.articles or result.stats["exhausted"] else 1


if __name__ == "__main__":
    sys.exit(main())
