# utils.py
import re
import html

from bs4 import BeautifulSoup


def make_soup(response, parser="html.parser"):
    """從 requests 的回應建 BeautifulSoup，**編碼以 HTTP 標頭為準**。

    為什麼不能直接 `BeautifulSoup(response.content, ...)`：衛福部有一批頁面
    標頭寫著 `charset=utf-8`，但 HTML 裡沒有 `<meta charset>`。BeautifulSoup
    在沒有 meta 時會自己猜，實測 `https://www.mohw.gov.tw/cp-4343-86333-1.html`
    被猜成 `ptcp154`（西里爾語系），整篇存進資料庫變成
    「з–ҫз—…з®ЎеҲ¶зҪІ」這種亂碼——2026-09-19 在知識庫裡找到 10 篇這樣的文章，
    而且不會有任何錯誤訊息，ETL 照樣「成功」。

    標頭沒帶 charset 時退回 utf-8：這幾個站台都是 utf-8，猜錯的代價（整篇亂碼）
    遠大於硬指定的風險。真的不是 utf-8 時會拋 UnicodeDecodeError 被呼叫端的
    重試／計數接住，不會靜靜地寫壞資料。
    """
    content_type = (response.headers.get("content-type") or "").lower()
    encoding = response.encoding if "charset=" in content_type else None
    return BeautifulSoup(response.content, parser, from_encoding=encoding or "utf-8")

def clean_html(raw_html):
    """清洗 HTML 標籤與特殊字元，回傳純文字"""
    if not raw_html: return ""
    clean_text = re.sub(r'<[^>]+>', '', raw_html)
    clean_text = clean_text.replace('&nbsp;', ' ').replace('&rdquo;', '"').replace('&ldquo;', '"')
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    return html.unescape(clean_text)