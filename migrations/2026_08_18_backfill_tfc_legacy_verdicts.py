#!/usr/bin/env python3
"""一次性遷移：從標題前綴補回舊站 TFC 文章的判定。

背景
----
TFC 有兩套判定標示慣例，互斥：
  新文（/fact-check-reports/<slug>）  分類連結 /fact-check-report-classification/…
  舊文（/fact-check-reports/migration-11252）  標題前綴「【錯誤】網傳「⋯」？」

`scraper_tfc` 起初只認分類連結，因此舊站遷移的文章判定全為 None——線上實測
102 篇無判定的文章 100% 都有前綴，等於白白丟掉四成 TFC 資料的判定標籤。
scraper 已補上第二條抽取路徑，但既有資料不會自己更新：ETL 以 url 判定已存在
會直接跳過，而補中繼資料那條路徑只在 `updated_at` 原本為空時才觸發。

好消息是前綴已經存在於 Mongo 的 `original_title`，不需要重新爬任何一頁。

判別條件：`source_name` 為 TFC 且 `verdict` 為 None 且標題有可辨識的前綴。
重複執行安全（補過的不再符合條件）。ETL 執行期間也可以跑，但建議跑完後再執行
一次，把該次新寫入的舊站文章一併補上。

用法
----
    python migrations/2026_08_18_backfill_tfc_legacy_verdicts.py
    python migrations/2026_08_18_backfill_tfc_legacy_verdicts.py --apply
"""
import argparse
import os
import sys
from collections import Counter

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_tfc import LEGACY_PREFIX_VERDICT, TITLE_PREFIX_RE  # noqa: E402

SOURCE = "台灣事實查核中心"
TARGET = {"source_name": SOURCE, "verdict": None}


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

    titles = coll.distinct("original_title", TARGET)
    plan, unknown = {}, []
    for title in titles:
        match = TITLE_PREFIX_RE.match(title)
        verdict = LEGACY_PREFIX_VERDICT.get(match.group(1)) if match else None
        if verdict:
            plan[title] = (f"legacy:{match.group(1)}", verdict)
        else:
            unknown.append(title)

    print(f"{SOURCE} 無判定文章: {len(titles)} 篇")
    print(f"  可從標題前綴補回: {len(plan)} 篇")
    print(f"  前綴認不得／無前綴: {len(unknown)} 篇（維持 None，不硬猜）")
    if plan:
        print("\n補回的判定分佈:")
        for verdict, n in Counter(v for _, v in plan.values()).most_common():
            print(f"    {verdict:<8} {n:>3}")
    for title in unknown[:5]:
        print(f"    ⚠️ 無法判定: {title[:56]}")

    if not args.apply:
        print("\n（試跑，未寫入。加上 --apply 才會實際執行）")
        return 0

    chunks = 0
    for title, (slug, verdict) in plan.items():
        chunks += coll.update_many(
            {**TARGET, "original_title": title},
            {"$set": {"verdict": verdict, "verdict_slug": slug}},
        ).modified_count
    print(f"\n✅ 已補回 {len(plan)} 篇 / {chunks} 個 chunk 的判定")
    remaining = coll.count_documents(TARGET)
    print(f"   {SOURCE} 仍無判定的 chunk: {remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
