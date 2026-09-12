#!/usr/bin/env python3
"""一次性遷移：把國健署那批的來源名從「衛福部闢謠網站」改成「國健署新聞」。

背景
----
`scraper_api.py` 把 `https://www.hpa.gov.tw/wf/newsapi.ashx` 的 source_name
標成「衛福部闢謠網站」。`hpa.gov.tw` 是**國民健康署**，衛福部的下屬機關，
不是本部（本部是 `mohw.gov.tw`）。

這與 2026-08-16 修掉的「DataAction 被誤標成食藥署闢謠專區」是同一類錯誤：
來源名與實際端點對不上。差別在於這次更急——`mohw-truth-clarification` 要新增
真正的衛福部本部來源，兩個名稱屆時會直接衝突。

為什麼不能只改 scraper
----------------------
ETL 以 url 判定「已存在」就跳過，既有文件的 source_name 永遠不會被回頭更新。
只改程式碼的結果是新舊兩批資料掛著兩個名字指向同一個來源，而使用者會在不同
回覆裡看到不一致的機構名。

**請在下一次 ETL 執行之前跑。**

為什麼這件事對使用者是可見的
----------------------------
`source_name` 不是內部欄位。它會出現在 CARE 回覆的參考來源清單、每日醫療消息卡
上，以及分享給家人的卡片上——而分享卡上的來源名是收件人**唯一能自行查證的
東西**。一個闢謠 bot 把發布機關標錯，比標題錯字嚴重。

安全性
------
只改 `source_name` 一個欄位，不動 `chunk_content` 與 `embedding`，因此既有向量
不失效、不需要重新向量化，也不需要與 CARE 端協調 cutover。重複執行安全：第二次
跑時已經沒有符合條件的文件。

用法
----
    python migrations/2026_09_09_rename_hpa_source.py            # 試跑，不寫入
    python migrations/2026_09_09_rename_hpa_source.py --apply    # 實際執行
"""
import argparse
import os

from dotenv import load_dotenv
from pymongo import MongoClient

OLD_NAME = "衛福部闢謠網站"
NEW_NAME = "國健署新聞"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="實際寫入；省略則只試跑並印出將要發生的變更")
    args = parser.parse_args()

    load_dotenv()
    uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
    if not uri:
        print("❌ 找不到 MONGO_URI / MONGODB_URI")
        return 1

    client = MongoClient(uri, serverSelectionTimeoutMS=20000)
    coll = client["CARE_database"]["health_articles_chunks"]

    target = {"source_name": OLD_NAME}
    chunks = coll.count_documents(target)
    if not chunks:
        print(f"沒有掛著「{OLD_NAME}」的文件，不需要遷移（或已經跑過了）。")
        return 0

    articles = coll.count_documents({**target, "chunk_index": 1})
    sample = coll.find(target, {"original_title": 1, "url": 1}).limit(3)

    print(f"將改名：「{OLD_NAME}」→「{NEW_NAME}」")
    print(f"  {articles} 篇 / {chunks} chunks")
    print("\n抽樣三筆確認是同一批：")
    for doc in sample:
        print(f"    {str(doc.get('original_title'))[:44]:<46} {doc.get('url')}")

    if not args.apply:
        print("\n（試跑，未寫入。加上 --apply 才會實際執行）")
        return 0

    renamed = coll.update_many(target, {"$set": {"source_name": NEW_NAME}}).modified_count
    print(f"\n✅ 已改標 {renamed} 個 chunk 為「{NEW_NAME}」")

    print("\n遷移後各來源分佈（chunk_index=1）：")
    for name in sorted(coll.distinct("source_name")):
        n = coll.count_documents({"source_name": name, "chunk_index": 1})
        print(f"    {name:<16} {n:>6} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
