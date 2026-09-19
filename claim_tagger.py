#!/usr/bin/env python3
"""替政府闢謠文章補上 `claim` 與 `verdict`，讓查核比對看得到它們。

為什麼需要這一步
----------------
CARE 的查核比對只認帶 `verdict` 的文件，而 ETL 只有 TFC 給得出那兩欄（見
`main_pipeline.upload_to_mongodb` 的註解）。食藥署闢謠專區、國健署真相與闢謠、
疾管署闢謠專區、衛福部真相說明這四個來源本質上就是「這個說法是錯的」，卻因為
沒有標籤而完全不會被比對到——2026-09-19 線上 16 次查核只命中 1 次。

怎麼抽
------
1. **主張（claim）先用標題規則**：這批標題句型固定（「⋯，這是真的嗎？」
   「網傳「⋯」為假訊息」），實測涵蓋 84%。抽得到就用標題的，因為那是使用者
   真正會轉傳的說法；模型改寫過的版本容易把結論混進主張裡（TFC 那次遷移
   踩過這個坑，見 migrations/2026_08_18_backfill_tfc_claims_from_title.py）。
2. **判定（verdict）一定要讀內文**：標題帶判定線索的只有 0%～43%，其餘是
   問句型，答案在內文；而且不能一律當成「錯誤」——國健署那批混著答案是「對」
   的題目（「確診婦女可以母乳哺育嗎？」）。模型要引用原文一句當佐證，
   **判斷不出來就跳過，不硬標**。

`claim`／`verdict` 是中繼資料，與切片內容無關，**不需要重新向量化**。

這個模組同時被兩邊使用：
- `main_pipeline.job()`：每次 ETL 之後自動補新收的文章。
- `migrations/2026_09_19_tag_gov_debunk_articles.py`：一次性回填與人工複查。
"""
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests

from scraper_tfc import VERDICT_BY_SLUG

SOURCES = ["食藥署闢謠專區", "衛福部真相說明", "國健署真相與闢謠", "疾管署闢謠專區"]
VALID_VERDICTS = set(VERDICT_BY_SLUG.values())
SLUG_BY_VERDICT = {v: k for k, v in VERDICT_BY_SLUG.items()}

MODEL = "gemini-3.5-flash-lite"
# 每篇送進模型的內文上限。闢謠稿多半兩三百字就講完結論，超過的部分是法規附錄
# 與聯絡方式，對判定沒有幫助，只是拉高成本。
BODY_CHARS = 2500
CONCURRENCY = 8
TIMEOUT_S = 40

# 機關前綴：「(疾管署) …」「（食藥署）…」
AGENCY_RE = re.compile(r"^[（(][^）)]{1,8}[）)]\s*")
QUOTED_RE = re.compile(r"[「『\"]([^「」『』\"]{6,})[」』\"]")
IS_TRUE_RE = re.compile(r"^(.{6,}?)[，,]?\s*(?:這)?是真的嗎[?？]?$")
QUESTION_RE = re.compile(r"[?？]\s*$")
# 「網傳」「近期Line流傳」這類引導詞。使用者轉述謠言時不會這樣講，留著只是
# 讓主張向量多一段跟語意無關的共同前綴，比對時等於白白稀釋。
LEADIN_RE = re.compile(
    r"^(?:有關\s*)?(?:近期|最近|日前)?\s*"
    r"(?:網路社群|網路|社群媒體|社群|媒體|Line|LINE|line|Facebook|FB|臉書)?\s*"
    r"(?:上)?(?:瘋傳|流傳|謠傳|盛傳|傳言|傳出|網傳|正瘋傳|有關)?\s*[，,、：:]?\s*"
)
# 「這篇在回應謠言」的標題特徵。用來把「衛福部真相說明」裡的政策回應稿篩掉：
# 那批 414 篇有 47% 在回應網路謠言，其餘是對媒體報導、民間團體質疑的說明
# （例如「回應『2018年度十大兒保新聞』」），抽出來的「主張」是報導名稱而不是
# 一個可以查證的說法。CARE 是健康助理，不該對政策爭議發判定。
# 另外三個來源本身就是闢謠專區，整批都算。
RUMOR_TITLE_RE = re.compile(r"網傳|謠傳|流傳|假訊息|不實|謠言|真的嗎|澄清|勿轉傳|誤傳|偽冒")
RUMOR_ONLY_SOURCES = {"衛福部真相說明"}
# 主張的最低長度。只擋明顯的垃圾（空字串、兩三個字的殘句），不做更多過濾。
#
# 2026-09-19 試過兩版更嚴的規則，都誤殺真謠言，已放棄：
#   8 字門檻 → 刷掉「可樂會殺精」「吃正露丸會致癌」「無花果不能吃」
#              「吃肉桂能降血糖」，而那正是長輩最常轉傳的句型。
#   述語關鍵字 → 中文動詞列不完，「臺灣愛滋疫情比泰國嚴重」「泰國水果罐頭疑似
#              遭愛滋病患者血液汙染」都被擋掉，可標記數反而從 1,087 掉到 1,000。
#
# 名詞片語（「基層缺藥」「毒化妝品清單」）確實不是可判真假的說法，但**擋它們
# 不該是這裡的工作**：查核管線本來就有同一性驗證（fail-closed，見 CARE 的
# claim_verification/identity.py），使用者的話跟這種片語對不上就會被擋下。
# 在這裡多設一道門檻，誤殺的是真謠言，擋下的卻是本來就過不了那一關的東西。
MIN_CLAIM_CHARS = 4

# UTF-8 被當成別的編碼解讀後的典型殘骸
MOJIBAKE_RE = re.compile(r"[Ѐ-ӿ]")

PROMPT = """你要判斷一篇政府機關的闢謠／澄清文章，對「某個流傳的說法」給了什麼結論。

文章標題：{title}
文章內容：
{body}

請只輸出 JSON，不要加說明文字，格式：
{{"claim": "...", "verdict": "...", "quote": "..."}}

claim：這篇文章在回應的那個說法，寫成一句完整、可獨立閱讀的陳述句（不要問句、
不要「網傳」開頭、不要加入文章的結論）。
verdict：只能是這五個之一——錯誤、部分錯誤、正確、事實釐清、證據不足。
  錯誤＝該說法不成立；部分錯誤＝部分屬實但有誤導；正確＝該說法成立；
  事實釐清＝說法本身不是真假問題，文章在補充脈絡；證據不足＝文章明講目前無定論。
quote：文章裡支持這個判定的原文一句，逐字照抄。

文章沒有對那個說法給出明確結論時，verdict 填 "無法判斷"。"""


def strip_agency(title: str) -> str:
    return AGENCY_RE.sub("", title).strip()


def strip_leadin(claim: str) -> str:
    """去掉「網傳」這類引導詞；整句都是引導詞時保留原樣。"""
    stripped = LEADIN_RE.sub("", claim, count=1).strip("，,、。 ")
    return stripped or claim


def claim_from_title(title: str) -> str | None:
    """標題規則抽出的主張；抽不到回 None（仍會送模型，由它從內文抽）。"""
    text = strip_agency(title)
    matched = IS_TRUE_RE.match(text)
    if matched:
        inner = matched.group(1).strip()
        quoted = QUOTED_RE.search(inner)
        return strip_leadin((quoted.group(1) if quoted else inner).strip("，,。 ")) or None
    quoted = QUOTED_RE.search(text)
    if quoted:
        return strip_leadin(quoted.group(1).strip()) or None
    if QUESTION_RE.search(text):
        return strip_leadin(text.rstrip("?？ ").strip()) or None
    return None


def should_tag(row: dict) -> bool:
    """這一篇的標籤能不能寫進資料庫。理由見上方兩個常數。"""
    if not row["verdict"] or not row["claim"]:
        return False
    if len(row["claim"]) < MIN_CLAIM_CHARS:
        return False
    if row["source"] in RUMOR_ONLY_SOURCES and not RUMOR_TITLE_RE.search(row["title"]):
        return False
    return True


def parse_model_json(raw: str) -> dict | None:
    """模型偶爾會包 ```json 圍欄，或在前後加一句話。取第一個 {...} 區塊。"""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def classify(session: requests.Session, api_key: str, title: str, body: str) -> dict | None:
    payload = {
        "contents": [
            {"parts": [{"text": PROMPT.format(title=title, body=body[:BODY_CHARS])}]}
        ],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}"
        f":generateContent?key={api_key}"
    )
    try:
        response = session.post(url, json=payload, timeout=TIMEOUT_S)
        response.raise_for_status()
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:  # noqa: BLE001 - 單篇失敗不該中斷整批
        print(f"    ⚠️ 模型呼叫失敗（{type(exc).__name__}）：{title[:30]}")
        return None
    return parse_model_json(text)


def load_articles(coll, limit: int) -> list[dict]:
    """以 url（沒有就用標題）為單位把切片併回文章。"""
    pipeline = [
        {"$match": {"source_name": {"$in": SOURCES}, "verdict": {"$in": [None, ""]}}},
        {"$sort": {"chunk_index": 1}},
        {
            "$group": {
                "_id": {"$ifNull": ["$url", "$original_title"]},
                "title": {"$first": "$original_title"},
                "source": {"$first": "$source_name"},
                "url": {"$first": "$url"},
                "body": {"$push": "$chunk_content"},
                "chunks": {"$sum": 1},
            }
        },
        {"$sort": {"_id": 1}},
    ]
    if limit:
        pipeline.append({"$limit": limit})
    articles = []
    for row in coll.aggregate(pipeline):
        articles.append(
            {
                "key": row["_id"],
                "title": row["title"] or "",
                "source": row["source"],
                "url": row.get("url") or "",
                "body": "\n".join(part or "" for part in row["body"]),
                "chunks": row["chunks"],
            }
        )
    return articles


def tag_all(articles: list[dict], api_key: str, *, verbose: bool = True) -> list[dict]:
    """平行送模型。用執行緒而不是 asyncio：這個 repo 的 HTTP 一律走 requests
    （見 main_pipeline），為了一支一次性腳本引入第二套 HTTP 客戶端不划算。"""
    session = requests.Session()
    results: list[dict] = []

    def one(index_article: tuple[int, dict]) -> dict:
        index, article = index_article
        parsed = classify(session, api_key, article["title"], article["body"])
        if verbose and (index + 1) % 50 == 0:
            print(f"    已處理 {index + 1}/{len(articles)}")
        verdict = str((parsed or {}).get("verdict") or "").strip()
        claim = str((parsed or {}).get("claim") or "").strip()
        return {
            **{k: article[k] for k in ("key", "title", "source", "url", "chunks")},
            # 標題抽得到就用標題的：那是使用者真正會轉傳的說法，模型改寫過的
            # 版本容易把結論混進主張裡（見 TFC 那次遷移踩到的坑）。
            "claim": claim_from_title(article["title"]) or claim,
            "model_claim": claim,
            "verdict": verdict if verdict in VALID_VERDICTS else "",
            "raw_verdict": verdict,
            "quote": str((parsed or {}).get("quote") or "").strip(),
        }

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(one, enumerate(articles)))
    return results



def tag_untagged(collection, api_key, *, limit=0, apply=True, verbose=True):
    """把還沒有標籤的政府闢謠文章補上 claim／verdict。

    回傳統計 dict。`apply=False` 只算不寫（給 dry-run 用）。重複執行安全：
    補過的文章 `verdict` 已非空，不再符合撈取條件。
    """
    from pymongo import UpdateMany

    articles = load_articles(collection, limit)
    mojibake = [a for a in articles if MOJIBAKE_RE.search(a["body"])]
    usable = [a for a in articles if not MOJIBAKE_RE.search(a["body"])]
    stats = {
        "candidates": len(articles),
        "mojibake": len(mojibake),
        "tagged": 0,
        "skipped": 0,
        "verdicts": {},
        "results": [],
    }
    if not usable:
        if verbose:
            print("沒有待補標籤的政府闢謠文章")
        return stats

    if verbose:
        print(f"待補標籤 {len(articles)} 篇（內容亂碼跳過 {len(mojibake)} 篇）")
    results = tag_all(usable, api_key, verbose=verbose)
    taggable = [r for r in results if should_tag(r)]
    stats["results"] = results
    stats["tagged"] = len(taggable)
    stats["skipped"] = len(results) - len(taggable)
    stats["verdicts"] = dict(Counter(r["verdict"] for r in taggable))

    if apply and taggable:
        operations = [
            UpdateMany(
                {"url": row["key"]} if row["url"] else {"original_title": row["key"]},
                {
                    "$set": {
                        "claim": row["claim"],
                        "verdict": row["verdict"],
                        "verdict_slug": SLUG_BY_VERDICT[row["verdict"]],
                    }
                },
            )
            for row in taggable
        ]
        modified = collection.bulk_write(operations, ordered=False).modified_count
        stats["modified_chunks"] = modified
        if verbose:
            print(f"✅ 補上標籤 {len(taggable)} 篇（{modified} 個切片）")
    elif verbose:
        print(f"可標記 {len(taggable)} 篇（未寫入）")
    return stats
