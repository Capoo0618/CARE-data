#!/usr/bin/env python3
"""一次性遷移：把誤標的食藥署新聞稿改名，並清掉其中的行政公告。

背景
----
2026-08-16 之前，`scraper_api.py` 把食藥署的 `DataAction` 端點標成
「食藥署闢謠專區」。那是誤標：該端點回傳的是全站新聞稿 feed，706 篇裡
只有 6 篇標題含「謠」字，約兩成是法規預告、研討會、表揚典禮這類行政公告。
真正的闢謠專區（news.aspx?cid=5049，587 篇、每篇都有網址）從未被收錄，
現已由 `scraper_fda.py` 負責，且沿用「食藥署闢謠專區」這個名稱。

為什麼一定要跑這支
------------------
ETL 對沒有 url 的文章是**比對標題**判斷「已存在」，既有文章會被直接跳過，
`source_name` 永遠不會被更新。所以光改 scraper 的程式碼不會讓既有的 706 篇
換名字——不跑這支，改名等於沒發生，而且新舊兩批資料會混在同一個標籤下。

**請在下一次 ETL 執行之前跑。**

做兩件事
--------
1. 把既有的「食藥署闢謠專區」且 url 為空的 chunk 改標成「食藥署公告」。
   以 url 為空作為判別條件：新的闢謠專區資料每一篇都有 url，兩批不會混淆。
2. 刪掉其中屬於行政公告的 chunk（判準與 scraper_api.is_admin_notice 相同，
   直接 import，不重新發明一份關鍵字）。

用法
----
    python migrations/2026_08_16_relabel_fda_notices.py            # 試跑，不寫入
    python migrations/2026_08_16_relabel_fda_notices.py --apply    # 實際執行
"""
import argparse
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_api import is_admin_notice  # noqa: E402

OLD_NAME = "食藥署闢謠專區"
NEW_NAME = "食藥署公告"

# 舊資料的判別條件：掛著舊名稱、但沒有文章網址。新的闢謠專區資料每篇都有
# url，因此這個條件不會誤傷；也代表這支腳本重複執行是安全的（第二次跑時
# 已經沒有符合條件的文件了）。
LEGACY_FILTER = {
    "source_name": OLD_NAME,
    "$or": [{"url": None}, {"url": ""}, {"url": {"$exists": False}}],
}


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

    legacy_chunks = coll.count_documents(LEGACY_FILTER)
    legacy_titles = coll.distinct("original_title", LEGACY_FILTER)
    if not legacy_chunks:
        print("沒有符合條件的舊資料，不需要遷移（或已經跑過了）。")
        return 0

    noise_titles = sorted(t for t in legacy_titles if is_admin_notice(t))
    noise_chunks = coll.count_documents(
        {**LEGACY_FILTER, "original_title": {"$in": noise_titles}}) if noise_titles else 0

    print(f"舊資料（{OLD_NAME} 且無 url）: {len(legacy_titles)} 篇 / {legacy_chunks} chunks")
    print(f"  其中屬行政公告，將刪除    : {len(noise_titles)} 篇 / {noise_chunks} chunks")
    print(f"  其餘改標成「{NEW_NAME}」   : "
          f"{len(legacy_titles) - len(noise_titles)} 篇 / {legacy_chunks - noise_chunks} chunks")
    print("\n將刪除的前 10 篇：")
    for t in noise_titles[:10]:
        print(f"    - {t[:64]}")

    if not args.apply:
        print("\n（試跑，未寫入。加上 --apply 才會實際執行）")
        return 0

    if noise_titles:
        deleted = coll.delete_many(
            {**LEGACY_FILTER, "original_title": {"$in": noise_titles}}).deleted_count
        print(f"\n✅ 已刪除 {deleted} 個行政公告 chunk")

    renamed = coll.update_many(
        LEGACY_FILTER, {"$set": {"source_name": NEW_NAME}}).modified_count
    print(f"✅ 已改標 {renamed} 個 chunk 為「{NEW_NAME}」")

    print("\n遷移後各來源分佈：")
    for name in sorted(coll.distinct("source_name")):
        print(f"    {name:<16} {coll.count_documents({'source_name': name}):>6} chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
