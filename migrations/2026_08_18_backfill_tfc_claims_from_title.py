#!/usr/bin/env python3
"""一次性遷移：舊站 TFC 文章的 claim 改從標題取，覆蓋掉存成結論的那些。

背景
----
舊站遷移的文章（標題帶「【判定】」前綴）詳細頁沒有獨立的主張段落——實測其
符合 `CLAIM_RE` 的元素只有一個，而那是查核結論：

    claim 欄位   ✗ 傳言說法缺乏醫學根據，過度誇大喝水可治病，因此為「錯誤」訊息。
    標題去前綴   ✓ 網傳「喝水溫度決定壽命，不用藥、僅用水就能治療心臟病」？

線上 802 篇裡有 143 篇因此把結論存成了主張，而這 143 篇的標題 100% 都帶得出
真正的主張。scraper 已改為標題優先，但既有資料不會自己更新：ETL 以 url 判定
已存在會直接跳過。

為什麼值得補
------------
下游的主張同一性驗證（design 決策 9）拿使用者的主張去比對這個欄位。比一句
沒有主題的結論句必然判成「不同主張」，於是一則明明查核過的謠言會被回成
「證據不足」。線上量到的 25% 正樣本召回損失，其中一部分就是這個原因，而不是
fail-closed 的必然成本。

claim 是中繼資料，與切片內容無關，因此不需要重新向量化。

判別條件：`source_name` 為 TFC 且標題有「【】」前綴。以標題推導出的 claim 與
現存值相同時不寫入，所以重複執行是安全的。

用法
----
    python migrations/2026_08_18_backfill_tfc_claims_from_title.py
    python migrations/2026_08_18_backfill_tfc_claims_from_title.py --apply
"""
import argparse
import os
import re
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_tfc import TITLE_PREFIX_RE  # noqa: E402

SOURCE = "台灣事實查核中心"
# 結論句的特徵：帶判定字樣或「因此」的收尾
CONCLUSION_RE = re.compile(r"因此|為「?(錯誤|部分錯誤|事實釐清|證據不足)」?訊息")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="實際寫入")
    args = parser.parse_args()

    load_dotenv()
    uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
    if not uri:
        print("❌ 找不到 MONGO_URI / MONGODB_URI")
        return 1
    coll = MongoClient(uri, serverSelectionTimeoutMS=20000)[
        "CARE_database"]["health_articles_chunks"]

    # 一篇文章一筆（同篇的所有 chunk 共用 claim）
    articles: dict[str, dict] = {}
    for doc in coll.find({"source_name": SOURCE},
                         {"url": 1, "original_title": 1, "claim": 1}):
        articles.setdefault(doc.get("url") or doc["original_title"], doc)

    plan, was_conclusion = {}, 0
    for key, doc in articles.items():
        title = doc.get("original_title") or ""
        match = TITLE_PREFIX_RE.match(title)
        if not match:
            continue
        new_claim = title[match.end():].strip()
        old_claim = (doc.get("claim") or "").strip()
        if not new_claim or new_claim == old_claim:
            continue
        # 只修真正壞掉的：結論句或空值。既有 claim 已經是有效主張、只是措辭與
        # 標題不同時不覆蓋——那不是缺陷而是偏好，沒有證據支持哪一種比較好。
        if old_claim and not CONCLUSION_RE.search(old_claim):
            continue
        plan[key] = (doc.get("url"), title, old_claim, new_claim)
        if CONCLUSION_RE.search(old_claim):
            was_conclusion += 1

    print(f"{SOURCE} 共 {len(articles)} 篇，標題帶前綴者將以標題推導 claim")
    print(f"  需要更新: {len(plan)} 篇")
    print(f"    原本存的是查核結論: {was_conclusion} 篇")
    print(f"    原本為空: {len(plan) - was_conclusion} 篇")
    print("\n前 3 筆對照：")
    for _, title, old, new in list(plan.values())[:3]:
        print(f"    舊 ✗ {old[:56] or '（空）'}")
        print(f"    新 ✓ {new[:56]}")
        print()

    if not args.apply:
        print("（試跑，未寫入。加上 --apply 才會實際執行）")
        return 0

    chunks = 0
    for key, (url, title, _old, new_claim) in plan.items():
        query = {"url": url} if url else {"original_title": title}
        chunks += coll.update_many(
            {**query, "source_name": SOURCE},
            {"$set": {"claim": new_claim}},
        ).modified_count
    print(f"✅ 已更新 {len(plan)} 篇 / {chunks} 個 chunk 的 claim")

    remaining = sum(
        1 for d in coll.find({"source_name": SOURCE}, {"claim": 1})
        if CONCLUSION_RE.search(d.get("claim") or "")
    )
    print(f"   claim 仍為結論句的 chunk: {remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
