#!/usr/bin/env python3
"""Cofacts 真的假的：抓健康類的謠言與協作查核回覆。

為什麼收這個來源
----------------
TFC 826 篇 ＋ 政府闢謠 1,128 篇仍然接不住多數民眾轉傳的訊息（2026-09-19 線上
16 次查核只命中 1 次）。Cofacts 的資料正好補上這個缺口：它收的是**民眾實際
用 LINE 轉傳的原文**，與長輩傳進 CARE 的東西是同一種材料，不是查核機構改寫過
的標題。

授權與取得方式
--------------
資料採 CC BY-SA 4.0，`LEGAL.md` 要求標示「Cofacts 真的假的」、每一則的原始
連結與授權條款——`COFACTS_ATTRIBUTION` 就是為此，顯示層（判定卡）必須帶上它。

**不爬網站**：Cofacts 的使用者條款禁止自動爬取，只允許走 API。這裡用的是公開
的 GraphQL 端點（api.cofacts.tw/graphql），並帶上可識別的 User-Agent。
HuggingFace 上那份資料集是受限存取（要登入並同意條款），不適合放進自動排程。

品質門檻（2026-09-19 抽樣 326 篇實測）
--------------------------------------
Cofacts 是網友協作，回覆品質不一，四道門檻都要過：

1. `type` 只收 RUMOR（含不實訊息 → 錯誤）與 NOT_RUMOR（含真實訊息 → 正確）。
   OPINIONATED（個人意見）與 NOT_ARTICLE（非查證範圍）不是真假判定，不收。
2. 回覆必須附 `reference`（出處）。Cofacts 自己的編輯規範就要求附來源，沒附的
   是「我覺得」等級的回覆。
3. 正評減負評 ≥ 3。實測 ≥1 有 55～73%、≥3 有 24～33%、≥5 有 15～22%；取 3 是
   在「有人背書」與「涵蓋率」之間，James 拍板。
4. 原文要有 ≥15 個中文字，且不是一條純網址。實測 8～22% 的回報只有一個 YouTube
   連結，那種東西當主張沒有意義（比對不到，也讀不出在說什麼）。

判定對照：RUMOR → 錯誤、NOT_RUMOR → 正確。**沒有「部分錯誤」**：Cofacts 的
分類本身就沒有這一級，硬套會替網友的回覆加上他沒有下的判斷。
"""
import re
import time
from urllib.parse import quote

import requests

API_URL = "https://api.cofacts.tw/graphql"
SOURCE_NAME = "Cofacts 真的假的"
ARTICLE_URL = "https://cofacts.tw/article/{id}"

# CC BY-SA 4.0 要求的標示。顯示層看到這個來源就要帶上（見 CARE 的 verdict_flex）。
COFACTS_ATTRIBUTION = "本則查核內容取自「Cofacts 真的假的」協作社群，採 CC BY-SA 4.0 授權"

# 健康相關分類。id 取自 ListCategories；三類合計約 15,000 篇有回覆的文章。
CATEGORY_IDS = [
    "medical",  # 疾病、醫藥
    "covid19",  # COVID-19 疫情
    "lT3h7XEBrIRcahlYugqq",  # 保健秘訣、食品安全
]

VERDICT_BY_REPLY_TYPE = {"RUMOR": ("錯誤", "incorrect"), "NOT_RUMOR": ("正確", "correct")}

MIN_FEEDBACK_SCORE = 3
MIN_CJK_CHARS = 15
# 轉傳訊息可以很長，超過這個長度只留前面：主張比對看的是開頭那段說法，
# 後面多半是「請大家告訴大家」與轉傳鏈。
MAX_CLAIM_CHARS = 300

_CJK_RE = re.compile(r"[一-鿿]")
_URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$")

_HEADERS = {
    "Content-Type": "application/json",
    # 可識別的 UA：對方要看得出誰在打他們的 API。
    "User-Agent": "CARE-health-assistant/1.0 (+https://github.com/Yanagi-0912/CARE)",
}

_LIST_QUERY = """
query($cats: [String], $after: String) {
  ListArticles(
    filter: { categoryIds: $cats, replyCount: { GT: 0 } }
    first: 100
    after: $after
    orderBy: [{ createdAt: DESC }]
  ) {
    pageInfo { lastCursor }
    edges {
      node {
        id
        text
        createdAt
        articleReplies(status: NORMAL) {
          positiveFeedbackCount
          negativeFeedbackCount
          reply { id type text reference }
        }
      }
    }
  }
}
"""


def _post(query, variables, timeout=60):
    response = requests.post(
        API_URL,
        json={"query": query, "variables": variables},
        headers=_HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"Cofacts API 錯誤：{payload['errors']}")
    return payload["data"]


def _is_usable_text(text: str) -> bool:
    """原文要讀得出在說什麼。只有一條網址的回報佔 8～22%，那種不能當主張。"""
    if not text or _URL_ONLY_RE.match(text):
        return False
    return len(_CJK_RE.findall(text)) >= MIN_CJK_CHARS


def pick_reply(article_replies, min_score=MIN_FEEDBACK_SCORE):
    """挑出這篇最有背書的合格回覆；沒有就回 None。四道門檻見模組說明。"""
    best = None
    for item in article_replies or []:
        reply = item.get("reply") or {}
        if reply.get("type") not in VERDICT_BY_REPLY_TYPE:
            continue
        if not (reply.get("reference") or "").strip():
            continue
        score = (item.get("positiveFeedbackCount") or 0) - (
            item.get("negativeFeedbackCount") or 0
        )
        if score < min_score:
            continue
        if best is None or score > best[0]:
            best = (score, reply)
    return best


def _to_article(node, reply, score):
    verdict, slug = VERDICT_BY_REPLY_TYPE[reply["type"]]
    claim = " ".join(node["text"].split())[:MAX_CLAIM_CHARS]
    # 內文＝原訊息＋查核回覆＋出處，而且**原訊息要放在最前面**。
    #
    # 比對是打在切片的向量上（見 CARE 的 claim_verification/matcher）：使用者
    # 傳來的是謠言原文，若切片裡只有查核回覆（也就是反駁那一段），兩邊講的是
    # 相反的事，向量對不上，整批資料等於白收。TFC 的報告本文本來就含「網傳⋯」
    # 那段，所以沒踩到這個坑；Cofacts 的回覆與原訊息是分開的兩欄，要自己接。
    body = (reply.get("text") or "").strip()
    reference = (reply.get("reference") or "").strip()
    parts = [f"網傳訊息：{claim}", f"查核回覆：{body}" if body else ""]
    if reference:
        parts.append(f"出處：{reference}")
    content = "\n\n".join(part for part in parts if part)
    return {
        "title": claim[:120],
        "content": content,
        "source": SOURCE_NAME,
        "url": ARTICLE_URL.format(id=quote(node["id"], safe="")),
        "published_at": (node.get("createdAt") or "")[:10] or None,
        "updated_at": None,
        "verdict": verdict,
        "verdict_slug": slug,
        "claim": claim,
        # 給呈現層決定要不要顯示授權標示；不入庫的欄位由 ETL 忽略。
        "attribution": COFACTS_ATTRIBUTION,
        "feedback_score": score,
    }


def get_cofacts_articles(
    *, test_mode=False, max_pages=400, categories=None, min_score=MIN_FEEDBACK_SCORE,
    sleep=time.sleep, post=None,
):
    """抓健康類且通過品質門檻的 Cofacts 文章。

    `post` 是給測試用的注入點（預設打真的 API）。翻頁到沒有資料為止；
    `max_pages` 只是保險，不是預期會到的上限。
    """
    post = post or _post
    categories = list(categories or CATEGORY_IDS)
    articles, seen = [], set()

    for category in categories:
        after = None
        for page in range(max_pages):
            try:
                data = post(_LIST_QUERY, {"cats": [category], "after": after})
            except Exception as exc:  # noqa: BLE001 - 單一分類失敗不該拖垮其他分類
                print(f"  ⚠️ Cofacts 分類 {category} 第 {page + 1} 頁失敗，停止這一類：{exc}")
                break
            listing = data["ListArticles"]
            edges = listing.get("edges") or []
            if not edges:
                break
            for edge in edges:
                node = edge["node"]
                if node["id"] in seen or not _is_usable_text(node["text"]):
                    continue
                picked = pick_reply(node.get("articleReplies"), min_score)
                if picked is None:
                    continue
                seen.add(node["id"])
                articles.append(_to_article(node, picked[1], picked[0]))
            after = listing["pageInfo"]["lastCursor"]
            if test_mode and len(articles) >= 3:
                return articles
            # 對方是公益服務，翻頁之間讓一下。
            sleep(0.5)
        print(f"  [Cofacts] 分類 {category} 累計通過 {len(articles)} 篇")

    print(f"[Cofacts] 完成，取得 {len(articles)} 篇（品質門檻：正評≥{min_score}、附出處）")
    return articles


if __name__ == "__main__":
    rows = get_cofacts_articles(test_mode=True)
    for row in rows:
        print(f"\n[{row['verdict']}] +{row['feedback_score']} {row['url']}")
        print(f"  主張：{row['claim'][:60]}")
        print(f"  內文：{row['content'][:80]}")
