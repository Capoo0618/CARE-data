# scraper_tfc.py
"""台灣事實查核中心「健康」專區查核報告爬蟲。

這是四個來源裡唯一本來就在做查核的——其他三個是政府機關發衛教資訊與新聞稿，
TFC 是專職判斷「這則謠言是真是假」。因此除了內文，這裡還額外抓三樣別的來源
給不了的東西：

1. **verdict**（錯誤／部分錯誤／正確／事實釐清／證據不足）
   由專業查核組織標註，直接可用，不需要自己標也不需要 LLM 猜。取自分類連結的
   **slug**（`/fact-check-report-classification/incorrect/`）而非顯示文字：
   slug 是機器可讀的識別碼，站方改文案時不會跟著變。
2. **claim**——被查核的主張本身（頁面上「網傳『⋯』？」那一句）。
3. **published_at / updated_at**——取自 JSON-LD，TFC 因此也能偵測改版。
"""
import json
import re
import time

import requests
from bs4 import BeautifulSoup

from ca_bundle import get_ca_bundle
from utils import clean_html

SOURCE_NAME = "台灣事實查核中心"
BASE_URL = "https://tfc-taiwan.org.tw"
LIST_URL = f"{BASE_URL}/fact-check-report-type/health"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
}

REPORT_LINK_RE = re.compile(r"https://tfc-taiwan\.org\.tw/fact-check-reports/[a-z0-9\-]+")
CLASSIFICATION_RE = re.compile(r"/fact-check-report-classification/([a-z0-9\-]+)/")
CLAIM_RE = re.compile(r"^(?:網傳|傳言|網路流傳|媒體報導)")

# 分類 slug → TFC 官方中文名稱。以站方的查核指標說明為準：
# https://tfc-taiwan.org.tw/fact-check-checking-indicators-explanation/
VERDICT_BY_SLUG = {
    "incorrect": "錯誤",
    "partially-incorrect": "部分錯誤",
    "correct": "正確",
    "fact-clarification": "事實釐清",
    "insufficient-evidence": "證據不足",
}

# TFC 舊站遷移過來的文章（網址形如 /fact-check-reports/migration-11252）沒有分類
# 連結，判定改以標題前綴「【XXX】」標示。實測 102 篇無分類連結的文章 100% 都有
# 前綴，因此這是完整的第二條抽取路徑，不是盡力而為的補救。
#
# 前四個是官方五分類的直接對應；後三個是舊站用過、現行分類表沒有的標籤，
# 依 TFC 官方定義歸併：
#   假借冠名 → 錯誤    官方「錯誤」的定義明列「假借冠名的言論」
#   詐騙     → 錯誤    詐騙訊息本身即為捏造
#   易生誤解 → 部分錯誤 官方「部分錯誤」涵蓋「內容為真實但為片面事實、脈絡有誤」
# 最後三者是歸併判斷而非站方明示，若日後 TFC 恢復這些標籤應重新檢視。
LEGACY_PREFIX_VERDICT = {
    "錯誤": "錯誤",
    "部分錯誤": "部分錯誤",
    "正確": "正確",
    "事實釐清": "事實釐清",
    "證據不足": "證據不足",
    "假借冠名": "錯誤",
    "詐騙": "錯誤",
    "易生誤解": "部分錯誤",
}

TITLE_PREFIX_RE = re.compile(r"^【([^】]{1,10})】")

# 查核結論的特徵。用來判斷內文抽到的是主張還是結論——舊站文章的內文只有結論，
# 存進 claim 會讓下游的主張同一性驗證拿使用者的主張去比一句沒有主題的結論句。
CONCLUSION_RE = re.compile(r"因此|為「?(錯誤|部分錯誤|事實釐清|證據不足)」?訊息")

# 內文只取這兩節。頁面其餘部分是導航、頁尾、相關文章、募款區塊與「關於我們」，
# 舊版把所有長度 >20 的 <p> 全部串起來，那些雜訊會一起進向量庫。
CONTENT_HEADINGS = ("背景", "查核")


def _get(url, timeout=20):
    resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=get_ca_bundle())
    resp.raise_for_status()
    return resp


def _report_links(page: int) -> list:
    """某一頁列表上的報告網址（保持順序、去重）。"""
    url = f"{LIST_URL}/" if page == 1 else f"{LIST_URL}/page/{page}/"
    resp = _get(url)
    links, seen = [], set()
    for link in REPORT_LINK_RE.findall(resp.text):
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links


def _extract_verdict(soup, title: str = "") -> tuple:
    """回傳 (slug, 中文名)。兩條路徑都失敗才回 (None, None)。

    先認分類連結——那是站方的機器可讀識別碼。刻意不改以頁面文字比對：頁尾有
    一個列出全部五種分類的說明區塊，用文字比對會誤抓成本篇的判定。

    分類連結不存在時（舊站遷移的文章）退而讀標題前綴「【XXX】」。這條路徑沒有
    slug 可回，因此 slug 以 `legacy:` 前綴標示來源，讓下游分得出判定是怎麼來的。
    """
    for anchor in soup.find_all("a", href=True):
        match = CLASSIFICATION_RE.search(anchor["href"])
        if match:
            slug = match.group(1)
            return slug, VERDICT_BY_SLUG.get(slug)

    prefix_match = TITLE_PREFIX_RE.match(title or "")
    if prefix_match:
        prefix = prefix_match.group(1)
        verdict = LEGACY_PREFIX_VERDICT.get(prefix)
        if verdict:
            return f"legacy:{prefix}", verdict
    return None, None


def _extract_claim(soup, title: str = "") -> str:
    """被查核的主張，即「網傳『⋯』？」那一句。

    標題有「【判定】」前綴時（舊站遷移的文章）**優先從標題取**，因為那些頁面
    的內文根本沒有獨立的主張段落——實測其詳細頁符合 `CLAIM_RE` 的元素只有一個，
    而那是查核結論（「⋯因此，傳言為『部分錯誤』訊息。」）。線上 802 篇裡有
    143 篇因此把結論存成了主張，而這 143 篇的標題 100% 都帶得出真正的主張：

        claim 欄位   ✗ 傳言說法缺乏醫學根據，過度誇大喝水可治病，因此為「錯誤」訊息。
        標題去前綴   ✓ 網傳「喝水溫度決定壽命，不用藥、僅用水就能治療心臟病」？

    存錯的後果不只是欄位髒：下游的主張同一性驗證拿使用者的主張去比一句沒有
    主題的結論句，會判成「不同主張」而放棄一則明明查過的謠言。

    新站文章的標題沒有前綴、內文則有獨立的主張段落，維持原本的內文抽取。
    """
    body_claim = ""
    for element in soup.find_all(["p", "h1", "h2", "h3"]):
        text = element.get_text(" ", strip=True)
        if 8 < len(text) < 200 and CLAIM_RE.match(text):
            body_claim = text
            break

    # 內文抽到真正的主張就用它——它與標題同樣有效，沒有理由覆蓋。只有在抽到
    # 結論句、或什麼都沒抽到時才退回標題。
    if body_claim and not CONCLUSION_RE.search(body_claim):
        return body_claim

    prefix_match = TITLE_PREFIX_RE.match(title or "")
    if prefix_match:
        title_claim = title[prefix_match.end():].strip()
        if title_claim:
            return title_claim
    return body_claim


def _extract_dates(soup) -> tuple:
    """(published_at, updated_at)，YYYY-MM-DD；取不到的部分為 None。"""
    published = updated = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes if isinstance(nodes, list) else [nodes]:
            if not isinstance(node, dict):
                continue
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if "Article" not in types and "Report" not in types:
                continue
            published = published or (node.get("datePublished") or "")[:10] or None
            updated = updated or (node.get("dateModified") or "")[:10] or None
    return published, updated


def _extract_content(soup) -> str:
    """只取「背景」與「查核」兩節之間的文字。

    走訪 h2 之後的同層節點直到下一個 h2，比 find_all('p') 精確得多，也保住了
    查核點的 h3 小標——那些小標本身就是結論句，對檢索有用。
    """
    parts = []
    for heading in soup.find_all("h2"):
        if heading.get_text(strip=True) not in CONTENT_HEADINGS:
            continue
        for sibling in heading.find_all_next():
            if sibling.name == "h2":
                break
            if sibling.name in ("p", "h3", "li"):
                text = sibling.get_text(" ", strip=True)
                if len(text) > 10:
                    parts.append(text)

    if not parts:
        # 站方改版時的保底：退回舊行為，至少不會整批抓不到東西。
        parts = [p.get_text(strip=True) for p in soup.find_all("p")
                 if len(p.get_text(strip=True)) > 20]

    # find_all_next 會跨越區塊重複走訪，同一段可能被收兩次
    deduped, seen = [], set()
    for text in parts:
        if text not in seen:
            seen.add(text)
            deduped.append(text)
    return clean_html(" ".join(deduped))


def _parse_report(url: str) -> dict | None:
    soup = BeautifulSoup(_get(url).content, "html.parser")
    for tag in soup(["script", "style"]):
        if tag.name == "style" or tag.get("type") != "application/ld+json":
            tag.decompose()

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    title = re.sub(r"\s*[-–|]\s*台灣事實查核中心\s*$", "", title).strip()

    content = _extract_content(soup)
    if not title or not content:
        return None

    verdict_slug, verdict = _extract_verdict(soup, title)
    published_at, updated_at = _extract_dates(soup)

    return {
        "title": title,
        "content": content,
        "source": SOURCE_NAME,
        "url": url,
        "published_at": published_at,
        "updated_at": updated_at,
        "verdict": verdict,
        "verdict_slug": verdict_slug,
        "claim": _extract_claim(soup, title),
    }


def get_tfc_articles(test_mode=False, start_page=1, max_pages=None, sleep_seconds=0.5):
    """爬健康專區的查核報告。

    :param max_pages: 翻頁上限。**預設 None＝抓到底**。舊版預設 2，那是開發期
        為了「避免等太久」的暫時設定卻一直沒改回去，導致線上只收了 28 篇——
        健康專區實際有約 600 篇，等於漏掉 95%，而這是四個來源裡最貼近闢謠的一個。
    """
    print(f"\n[{SOURCE_NAME}] 開始爬取列表: {LIST_URL}")

    links, seen = [], set()
    page = start_page
    while max_pages is None or page < start_page + max_pages:
        try:
            page_links = _report_links(page)
        except Exception as exc:
            print(f"  第 {page} 頁列表抓取失敗: {exc}")
            break

        new = [link for link in page_links if link not in seen]
        # 翻過頭時站方不會回 404，而是重複回同一頁，因此以「沒有新連結」為終止條件
        if not new:
            print(f"  第 {page} 頁沒有新報告，翻頁結束。")
            break

        seen.update(new)
        links.extend(new)
        page += 1
        if test_mode and len(links) >= 3:
            break
        time.sleep(sleep_seconds)

    if test_mode:
        links = links[:3]
    print(f"  -> 列表共取得 {len(links)} 篇查核報告")

    articles, failed, no_verdict = [], 0, 0
    for index, url in enumerate(links, start=1):
        try:
            article = _parse_report(url)
        except Exception as exc:
            failed += 1
            print(f"  [{index}/{len(links)}] 抓取失敗 {url}: {exc}")
            continue
        if article is None:
            failed += 1
            print(f"  [{index}/{len(links)}] 解析不出標題或內容，略過: {url}")
            continue
        if article["verdict"] is None:
            no_verdict += 1
        articles.append(article)
        if index % 50 == 0:
            print(f"  [{index}/{len(links)}] 已取得 {len(articles)} 篇")
        time.sleep(sleep_seconds)

    print(f"  -> 成功清洗 {len(articles)} 篇資料（失敗 {failed} 篇）")
    if no_verdict:
        # 不是致命錯誤（內容仍有價值），但持續升高代表站方改了分類連結結構
        print(f"  ⚠️ 其中 {no_verdict} 篇取不到判定標籤")
    return articles


# ==========================================
# 本地測試區塊
# ==========================================
if __name__ == "__main__":
    print("=== 測試 TFC 查核報告爬蟲 ===")
    arts = get_tfc_articles(test_mode=True)
    print(f"\n=== 取得 {len(arts)} 篇 ===")
    for i, a in enumerate(arts, 1):
        print(f"\n[{i}] {a['title']}")
        print(f"    verdict      : {a['verdict']} ({a['verdict_slug']})")
        print(f"    claim        : {a['claim']}")
        print(f"    url          : {a['url']}")
        print(f"    published_at : {a['published_at']}  updated_at: {a['updated_at']}")
        print(f"    content({len(a['content'])}字): {a['content'][:140]}…")
