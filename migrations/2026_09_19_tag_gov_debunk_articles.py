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
import sys
import time
from collections import Counter

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateMany

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from claim_tagger import (  # noqa: E402
    MOJIBAKE_RE,
    SLUG_BY_VERDICT,
    load_articles,
    should_tag,
    tag_all,
)


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
