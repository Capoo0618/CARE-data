import os
import sys
import time
import requests
import schedule
from dotenv import load_dotenv
from pymongo import MongoClient

# 匯入我們自己寫好的爬蟲模組
from scraper_api import get_api_articles
from scraper_tfc import get_tfc_articles

# 載入環境變數
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")

# 三個來源的正式名稱，與各爬蟲模組回傳的 source 欄位一致。
# 任一來源本次一篇都沒抓到，就是異常——見 find_missing_sources 的說明。
EXPECTED_SOURCES = frozenset({
    "食藥署闢謠專區",
    "衛福部闢謠網站",
    "台灣事實查核中心",
})


def find_missing_sources(articles, expected=EXPECTED_SOURCES):
    """回傳本次完全沒有產出任何文章的來源名稱集合。

    為什麼需要這個檢查：兩支爬蟲模組都用 `except Exception: print(...)`
    處理失敗，函式仍會正常回傳（只是少了那個來源的資料）。若沒有這層檢查，
    一個來源可以連續數週完全抓不到東西，而 ETL 每天照常「成功」結束、
    CI 一路綠燈——實際發生過：衛福部因伺服器未附中繼憑證而 TLS 驗證失敗，
    測試套件卻全綠（見 Task 4）。

    刻意只看「有沒有產出」而不看數量：來源本身的文章數會自然波動，
    設數量門檻會產生假警報；而「一篇都沒有」幾乎必然是故障。
    """
    seen = {a.get("source") for a in articles}
    return set(expected) - seen


def chunk_text(text: str, chunk_size=500, overlap=50) -> list:
    if not text: return []
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + chunk_size])
        start += (chunk_size - overlap)
    return chunks

def get_embedding(text: str, max_retries=3) -> list:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={API_KEY}"
    payload = {"model": "models/gemini-embedding-001", "content": {"parts": [{"text": text}]}}
    
    for attempt in range(max_retries):
        try:
            time.sleep(2) 
            response = requests.post(url, json=payload, timeout=15)
            response_data = response.json()
            
            if "error" in response_data:
                error_msg = response_data['error'].get('message', '未知錯誤')
                print(f"    [向量 API 錯誤] {error_msg}")
                if "Quota exceeded" in error_msg or "429" in str(error_msg):
                    print(f"    ⏳ 觸發 API 限制，等待 40 秒後重試... (第 {attempt+1}/{max_retries} 次)")
                    time.sleep(40)
                    continue 
                return []
            return response_data.get("embedding", {}).get("values", [])
        except Exception as e:
            print(f"    [向量例外錯誤] {e}")
            time.sleep(5)
    return []

def upload_to_mongodb(articles, collection, *, embed_fn=None):
    """把文章切片、向量化後寫入 MongoDB。

    寫入保證為「全有或全無」：一篇文章的所有 chunk 都成功取得向量才寫入。
    任一 chunk 失敗就整篇跳過、留待下次執行重試——避免產生「宣告 4 塊、
    實際只有 3 塊」這種永遠不會被補上的破洞（線上 pid=16703 即為此類實例）。
    """
    embed_fn = embed_fn or get_embedding
    print(f"\n=== 🚀 開始將 {len(articles)} 篇文章上傳至 MongoDB ===")

    # 一次取回既有鍵，取代逐篇 find_one；並讓「已存在」的判定只作用於單篇，
    # 不再因為某篇已存在就放棄整個來源的後續文章。
    existing_urls = {u for u in collection.distinct("url") if u}
    existing_titles = {t for t in collection.distinct("original_title") if t}

    new_count = 0
    for article in articles:
        url = article.get("url")
        title = article["title"]

        # 來源有提供修改日期且與庫中不同 → 視為改版。
        # 刻意不用內容雜湊比對：那會讓每次清洗邏輯微調都觸發全量重寫。
        # 這裡只做判定，實際刪除延後到 insert_many 之前（見下方），
        # 確保「刪掉舊版卻寫不出新版」這種資料遺失不會發生。
        incoming_updated = article.get("updated_at")
        needs_rewrite = False
        if url and incoming_updated:
            old = collection.find_one({"url": url}, {"updated_at": 1})
            if old and old.get("updated_at") != incoming_updated:
                print(f"  🔄 偵測到改版，將重寫: {title[:15]}...")
                needs_rewrite = True

        if not needs_rewrite and ((url and url in existing_urls) or title in existing_titles):
            print(f"  ⏭️ 已存在，跳過: {title[:15]}...")
            continue

        chunks = chunk_text(article["content"])
        if not chunks:
            print(f"  ⚠️ 內容為空，跳過: {title[:15]}...")
            continue

        print(f"  🆕 [處理中] 向量化並上傳: {title[:15]}...")
        vectors = []
        failed = False
        for i, chunk in enumerate(chunks):
            vector = embed_fn(f"主題：{title}\n內容：{chunk}")
            if not vector:
                print(f"    ⚠️ 第 {i+1}/{len(chunks)} 個切片向量化失敗——"
                      f"整篇跳過，留待下次執行重試")
                failed = True
                break
            vectors.append(vector)
        if failed:
            continue

        docs = [
            {
                "source_name": article["source"],
                "url": url,
                "original_title": title,
                "chunk_content": chunk,
                "chunk_index": i + 1,
                "total_chunks": len(chunks),
                "embedding": vector,
                "uploaded_at": time.time(),
                "published_at": article.get("published_at"),
                "updated_at": article.get("updated_at"),
            }
            for i, (chunk, vector) in enumerate(zip(chunks, vectors))
        ]
        if needs_rewrite:
            collection.delete_many({"url": url})
        collection.insert_many(docs)
        print(f"    ✅ 成功寫入 {len(docs)} 個切片")

        # 讓同一批次內的重複文章也能被擋掉
        if url:
            existing_urls.add(url)
        existing_titles.add(title)
        new_count += 1

    return new_count

def job():
    """執行一次完整 ETL。回傳 0 表示正常，1 表示有來源全滅或寫入失敗。"""
    print(f"\n=== 🟢 [{time.strftime('%Y-%m-%d %H:%M:%S')}] 啟動正式爬蟲任務 ===")
    print("\n[階段一：呼叫爬蟲模組提取資料]")
    all_articles = []

    all_articles.extend(get_api_articles(test_mode=False))
    all_articles.extend(get_tfc_articles(test_mode=False))

    print(f"\n🏁 階段一完成！總共收集到 {len(all_articles)} 篇待處理的文章。")

    exit_code = 0
    missing = find_missing_sources(all_articles)
    if missing:
        print(f"\n❌ 嚴重：以下來源本次完全沒有取得任何文章：{'、'.join(sorted(missing))}")
        print("   這通常代表爬蟲失效、來源網站改版、或網路／憑證問題。")
        print("   請檢查上方該來源的錯誤訊息。本次執行將以非零狀態碼結束。")
        exit_code = 1

    print("\n[階段二：切片與上傳]")
    try:
        client = MongoClient(MONGO_URI)
        collection = client["CARE_database"]["health_articles_chunks"]
        total_new = upload_to_mongodb(all_articles, collection)
        print(f"\n=== 🔴 [{time.strftime('%Y-%m-%d %H:%M:%S')}] 任務結束！成功上傳了 {total_new} 篇全新文章 ===")
    except Exception as e:
        print(f"❌ 嚴重：MongoDB 連線或上傳失敗: {e}")
        print("   本次執行將以非零狀態碼結束。")
        exit_code = 1

    return exit_code

if __name__ == "__main__":
    # 【重點升級】：環境偵測
    # 當運行在 GitHub Actions 時，會自帶 GITHUB_ACTIONS=true 環境變數
    if os.getenv("GITHUB_ACTIONS") == "true":
        print("☁️ 偵測到雲端 GitHub Actions 環境，啟動單次排程任務...")
        sys.exit(job())          # 非零退出碼讓 Actions 顯示紅燈
    else:
        print("💻 偵測到本地開發環境，啟動常駐排程系統...")
        print("每天早上 08:00 將自動執行爬蟲任務。")
        job()                    # 常駐模式不因單次失敗結束程序
        schedule.every().day.at("08:00").do(job)
        while True:
            schedule.run_pending()
            time.sleep(60)