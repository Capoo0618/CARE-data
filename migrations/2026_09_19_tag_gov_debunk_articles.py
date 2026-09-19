#!/usr/bin/env python3
"""一次性遷移：替政府闢謠文章補上 `claim` 與 `verdict`，讓它們也能被查核比對到。

背景
----
查核比對（CARE 的 `ClaimVerificationService`）只看得到帶 `verdict` 的文件，
而那目前**只有台灣事實查核中心** 826 篇。資料庫裡另外躺著 1,363 篇政府闢謠
文章：

    食藥署闢謠專區   587 篇
    衛福部真相說明   427 篇
    國健署真相與闢謠 326 篇
    疾管署闢謠專區    23 篇

它們本質上就是「這個說法是錯的」，但匯入時 `claim`／`verdict` 是空的（見
`main_pipeline` 的註解：那兩欄只有查核型來源給得出來），所以比對完全看不到
它們，只能在「相關衛教資訊」那區被引用。2026-09-19 線上 16 次查核只命中 1
次，多數回「證據不足」——補上這兩欄是手上最便宜的改善。

`claim`／`verdict` 是中繼資料，與切片內容無關，**不需要重新向量化**。

怎麼抽
------
1. **主張（claim）先用標題規則**。這批標題句型很固定，實測涵蓋率：
   食藥署闢謠專區 99%、國健署 95%、疾管署 83%、衛福部真相說明 54%（其餘是
   「回應⋯報導」這種沒有引號的澄清稿），整體 1,146/1,363＝84%。
2. **判定（verdict）一定要讀內文**。標題帶判定線索的只有 0%～43%，其餘是
   問句型（「皮蛋是用馬尿浸泡製成的，這是真的嗎？」），答案在內文；而且
   不能一律當成「錯誤」——國健署那批混著答案是「對」的題目（例如「確診婦女
   可以母乳哺育嗎？」）。標錯判定比沒有判定糟得多，所以交給模型讀，並要求
   它引用原文一句當佐證；**判斷不出來就跳過，不硬標**。

模型輸出的 verdict 只接受 TFC 那五個值（與 `scraper_tfc.VERDICT_BY_SLUG`
同一套詞彙），不在清單內一律跳過。

亂碼
----
「衛福部真相說明」有 47 個切片是抓取當時編碼就壞掉的（內文出現西里爾字母）。
那些文章直接跳過並列進報告——標籤補在壞掉的內容上沒有意義，要重抓。

用法
----
    python migrations/2026_09_19_tag_gov_debunk_articles.py --limit 30
    python migrations/2026_09_19_tag_gov_debunk_articles.py
    python migrations/2026_09_19_tag_gov_debunk_articles.py --apply

預設 dry-run：只印統計與樣本、寫一份 JSON 報告，不碰資料庫。重複執行安全
（補過的文章 `verdict` 已非空，不再符合條件），所以**每次 ETL 之後可以再跑一次**
把新收的闢謠文章補上——ETL 本身不會產生這兩欄（見 `main_pipeline` 的註解）。

`--from-report` 可以沿用上一次的模型結果重算過濾條件，調門檻時不必重付一次。

2026-09-19 首次執行結果
----------------------
1,363 篇未標記 → 亂碼跳過 10、模型判不出來 13、過濾條件刷掉 253，**實際寫入
1,087 篇（1,400 個切片）**。判定分布：錯誤 802、部分錯誤 162、正確 48、
事實釐清 39、證據不足 36。可比對的查核文章從 826 篇增加到 1,913 篇。

寫入後以真實管線抽驗 5 題，4 題命中且來源都是這次新標的：
皮蛋用馬尿泡（錯誤）、金針菇殺癌細胞（證據不足）、蘋果減肥法（部分錯誤）、
電療器材治百病（部分錯誤）；這四題在標記之前一律回「證據不足」。
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateMany

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_tfc import VERDICT_BY_SLUG  # noqa: E402

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
# 少於這個字數的「主張」多半是碎片（「第二波有毒食品」「鮮乳含磺胺劑」），
# 拿去跟使用者的話做同一性驗證只會誤配。
MIN_CLAIM_CHARS = 8

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


def tag_all(articles: list[dict], api_key: str) -> list[dict]:
    """平行送模型。用執行緒而不是 asyncio：這個 repo 的 HTTP 一律走 requests
    （見 main_pipeline），為了一支一次性腳本引入第二套 HTTP 客戶端不划算。"""
    session = requests.Session()
    results: list[dict] = []

    def one(index_article: tuple[int, dict]) -> dict:
        index, article = index_article
        parsed = classify(session, api_key, article["title"], article["body"])
        if (index + 1) % 50 == 0:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="實際寫入資料庫")
    parser.add_argument("--limit", type=int, default=0, help="只處理前幾篇（0＝全部）")
    parser.add_argument("--out", default="", help="報告輸出路徑")
    parser.add_argument(
        "--from-report",
        default="",
        help="改用既有報告的模型結果（不再呼叫模型）。調整過濾條件時用這個，不必重付一次。",
    )
    args = parser.parse_args()

    load_dotenv()
    uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
    api_key = os.getenv("GEMINI_API_KEY")
    if not uri:
        print("❌ 找不到 MONGO_URI / MONGODB_URI")
        return 1
    if not api_key and not args.from_report:
        print("❌ 找不到 GEMINI_API_KEY")
        return 1

    coll = MongoClient(uri, serverSelectionTimeoutMS=20000)["CARE_database"][
        "health_articles_chunks"
    ]

    if args.from_report:
        with open(args.from_report, encoding="utf-8") as handle:
            cached = json.load(handle)
        results = cached["taggable"] + cached["skipped"]
        mojibake = cached.get("mojibake", [])
        print(f"沿用既有報告 {args.from_report}：{len(results)} 篇模型結果，不再呼叫模型")
    else:
        articles = load_articles(coll, args.limit)
        mojibake = [a for a in articles if MOJIBAKE_RE.search(a["body"])]
        usable = [a for a in articles if not MOJIBAKE_RE.search(a["body"])]
        print(
            f"未標記文章 {len(articles)} 篇；內容亂碼跳過 {len(mojibake)} 篇，"
            f"處理 {len(usable)} 篇"
        )
        started = time.time()
        results = tag_all(usable, api_key)
        print(f"模型處理完成，耗時 {time.time() - started:.0f} 秒")

    taggable = [r for r in results if should_tag(r)]
    by_source = Counter(r["source"] for r in taggable)
    verdicts = Counter(r["verdict"] for r in taggable)
    skipped = [r for r in results if not should_tag(r)]

    print(f"\n可標記 {len(taggable)}/{len(results)} 篇")
    for source, count in by_source.most_common():
        print(f"    {source}: {count}")
    print(f"判定分布：{dict(verdicts)}")
    print(f"跳過 {len(skipped)} 篇（模型判不出來或抽不到主張）")

    print("\n樣本（前 8 篇）：")
    for row in taggable[:8]:
        print(f"  [{row['verdict']}] {row['claim'][:46]}")
        print(f"      標題：{row['title'][:46]}")
        print(f"      佐證：{row['quote'][:60]}")

    report_path = args.out or f"migrations/tag_gov_debunk-{time.strftime('%Y%m%d-%H%M')}.json"
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "taggable": taggable,
                "skipped": skipped,
                "mojibake": [
                    {k: a[k] for k in ("title", "source", "url", "chunks")} for a in mojibake
                ]
                if mojibake and "body" in (mojibake[0] if mojibake else {})
                else mojibake,
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n報告：{report_path}")

    if not args.apply:
        print("（dry-run，未寫入。確認後加 --apply）")
        return 0

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
    if not operations:
        print("沒有可寫入的資料")
        return 0
    result = coll.bulk_write(operations, ordered=False)
    print(f"✅ 已更新 {result.modified_count} 個切片（{len(operations)} 篇）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
