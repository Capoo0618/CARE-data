#!/usr/bin/env python3
"""一次性遷移：清掉用舊抓法產生的 TFC 切片，讓 ETL 重新收乾淨的版本。

背景
----
2026-08-17 之前，`scraper_tfc.py` 的內文抽取是「把所有長度 >20 的 <p> 串起來」，
導航、關於我們、支持事實查核、相關文章等固定文案會一起進向量庫，在檢索時與
真正的查核內容競爭名額。新版只取頁面上的「背景」與「查核」兩節。

為什麼一定要跑這支
------------------
ETL 以 url 判斷「已存在」，既有文章會被跳過，內文永遠不會被重抓。中繼資料
（verdict／claim／日期）有 `upload_to_mongodb` 的補寫路徑會補上，但**切片內容
不會**——那需要重新切片與向量化。

刪掉之後，下一次 ETL 會把這些文章當成新文章重新收錄，內文即為新版抽取結果。
只有約 148 個切片，重算成本可忽略。

判別條件
--------
`source_name = 台灣事實查核中心` 且**沒有 verdict 欄位**。新版一定會寫入
verdict（取不到判定時為 None，但欄位存在），因此這個條件精準指向舊資料，
且重複執行是安全的。

用法
----
    python migrations/2026_08_17_purge_stale_tfc_chunks.py            # 試跑
    python migrations/2026_08_17_purge_stale_tfc_chunks.py --apply    # 實際執行
"""
import argparse
import os

from dotenv import load_dotenv
from pymongo import MongoClient

SOURCE = "台灣事實查核中心"
STALE_FILTER = {"source_name": SOURCE, "verdict": {"$exists": False}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="實際刪除；省略則只試跑")
    args = parser.parse_args()

    load_dotenv()
    uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
    if not uri:
        print("❌ 找不到 MONGO_URI / MONGODB_URI")
        return 1

    coll = MongoClient(uri, serverSelectionTimeoutMS=20000)[
        "CARE_database"]["health_articles_chunks"]

    chunks = coll.count_documents(STALE_FILTER)
    titles = coll.distinct("original_title", STALE_FILTER)
    total = coll.count_documents({"source_name": SOURCE})

    if not chunks:
        print("沒有符合條件的舊資料，不需要遷移（或已經跑過了）。")
        return 0

    print(f"{SOURCE} 共 {total} chunks")
    print(f"  其中用舊抓法產生（無 verdict 欄位）: {len(titles)} 篇 / {chunks} chunks")
    print("\n將刪除的前 8 篇：")
    for title in sorted(titles)[:8]:
        print(f"    - {title[:60]}")
    print("\n刪除後由下一次 ETL 重新收錄，內文為新版抽取結果。")

    if not args.apply:
        print("\n（試跑，未寫入。加上 --apply 才會實際執行）")
        return 0

    deleted = coll.delete_many(STALE_FILTER).deleted_count
    print(f"\n✅ 已刪除 {deleted} 個切片")
    print(f"   {SOURCE} 剩餘 {coll.count_documents({'source_name': SOURCE})} chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
