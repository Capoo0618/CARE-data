#!/usr/bin/env python3
"""一次性遷移：重抓內文是亂碼的文章。

背景
----
衛福部有一批頁面 HTTP 標頭寫著 `charset=utf-8`，但 HTML 裡沒有 `<meta charset>`。
`BeautifulSoup(response.content, ...)` 在沒有 meta 時會自己猜編碼，實測
`https://www.mohw.gov.tw/cp-4343-86333-1.html` 被猜成 `ptcp154`（西里爾語系），
整篇存成「з–ҫз—…з®ЎеҲ¶зҪІ」這種亂碼。**ETL 不會因此報錯**，照樣寫入、照樣
「成功」，所以它在庫裡躺了很久才被發現（2026-09-19，10 篇 47 個切片）。

`utils.make_soup` 已改為以 HTTP 標頭的 charset 為準（沒有就用 utf-8），五支
爬蟲都換過去了。但既有資料不會自己更新：ETL 以 url 判定已存在會直接跳過。

做什麼
------
1. 找出內文含西里爾字母的切片，依 url 分組。
2. 用修好的爬蟲重抓明細頁，確認抓回來的內容不再是亂碼。
3. 刪掉舊切片，重新切片、向量化、寫回（走 `main_pipeline.upload_to_mongodb`，
   與 ETL 同一條路，不另寫一份寫入邏輯）。

會呼叫 embedding API（10 篇約 47 個切片，用量很小）。

用法
----
    python migrations/2026_09_19_refetch_mojibake_articles.py
    python migrations/2026_09_19_refetch_mojibake_articles.py --apply
"""
import argparse
import os
import re
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from main_pipeline import upload_to_mongodb  # noqa: E402
from scraper_mohw import _parse_detail  # noqa: E402

# UTF-8 被當成西里爾語系編碼解讀後的典型殘骸
MOJIBAKE_RE = re.compile(r"[Ѐ-ӿ]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="實際重抓並寫入")
    args = parser.parse_args()

    load_dotenv()
    uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
    if not uri:
        print("❌ 找不到 MONGO_URI / MONGODB_URI")
        return 1
    coll = MongoClient(uri, serverSelectionTimeoutMS=20000)["CARE_database"][
        "health_articles_chunks"
    ]

    broken = list(
        coll.aggregate(
            [
                {"$match": {"chunk_content": {"$regex": "[Ѐ-ӿ]"}}},
                {
                    "$group": {
                        "_id": "$url",
                        "title": {"$first": "$original_title"},
                        "source": {"$first": "$source_name"},
                        "published_at": {"$first": "$published_at"},
                        "chunks": {"$sum": 1},
                    }
                },
            ]
        )
    )
    print(f"內文亂碼的文章 {len(broken)} 篇（{sum(b['chunks'] for b in broken)} 個切片）")
    for row in broken:
        print(f"  {row['source']} | {row['chunks']} 段 | {row['_id']}")

    no_url = [b for b in broken if not b["_id"]]
    if no_url:
        # 沒有 url 就沒得重抓（也不該發生：這批來源每篇都有 url）。
        print(f"⚠️ {len(no_url)} 篇沒有 url，無法重抓，略過")

    articles = []
    still_broken = []
    for row in broken:
        url = row["_id"]
        if not url:
            continue
        try:
            detail = _parse_detail(url)
        except Exception as exc:  # noqa: BLE001 - 單篇失敗不中斷整批
            print(f"  ⚠️ 重抓失敗，略過：{url} —— {type(exc).__name__}")
            continue
        if detail is None:
            print(f"  ⚠️ 解析不出標題或內文，略過：{url}")
            continue
        if MOJIBAKE_RE.search(detail["content"]):
            # 修完還是亂碼＝這一篇不是同一個成因，硬寫回去只是換一批亂碼。
            still_broken.append(url)
            continue
        articles.append(
            {
                "title": row["title"] or detail["title"],
                "content": detail["content"],
                "source": detail["source"],
                "url": url,
                "published_at": row.get("published_at"),
                "updated_at": None,
            }
        )

    print(f"\n重抓成功且內容正常 {len(articles)} 篇")
    if still_broken:
        print(f"⚠️ 重抓後仍是亂碼 {len(still_broken)} 篇：{still_broken}")
    for article in articles[:3]:
        print(f"  {article['title'][:40]}")
        print(f"    {article['content'][:60]}")

    if not args.apply:
        print("\n（dry-run，未寫入。確認後加 --apply）")
        return 0

    if not articles:
        print("沒有可寫入的文章")
        return 0

    # 先刪舊切片：upload_to_mongodb 看到 url 已存在會跳過，不刪就等於沒換。
    urls = [a["url"] for a in articles]
    deleted = coll.delete_many({"url": {"$in": urls}}).deleted_count
    print(f"已刪除舊切片 {deleted} 個，開始重新切片與向量化")

    new_count, write_failed = upload_to_mongodb(articles, coll)
    print(f"✅ 重新寫入 {new_count} 篇；write_failed={write_failed}")
    remaining = coll.count_documents({"chunk_content": {"$regex": "[Ѐ-ӿ]"}})
    print(f"資料庫剩餘亂碼切片：{remaining}")
    return 1 if write_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
