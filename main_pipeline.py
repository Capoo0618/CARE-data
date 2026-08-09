import os
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
    print(f"\n=== 🟢 [{time.strftime('%Y-%m-%d %H:%M:%S')}] 啟動正式爬蟲任務 ===")
    print("\n[階段一：呼叫爬蟲模組提取資料]")
    all_articles = []
    
    all_articles.extend(get_api_articles(test_mode=False)) 
    all_articles.extend(get_tfc_articles(test_mode=False)) 
    
    print(f"\n🏁 階段一完成！總共收集到 {len(all_articles)} 篇待處理的文章。")
    print("\n[階段二：切片與上傳]")
    try:
        client = MongoClient(MONGO_URI)
        collection = client["CARE_database"]["health_articles_chunks"]
        total_new = upload_to_mongodb(all_articles, collection)
        print(f"\n=== 🔴 [{time.strftime('%Y-%m-%d %H:%M:%S')}] 任務結束！成功上傳了 {total_new} 篇全新文章 ===")
    except Exception as e:
        print(f"MongoDB 連線或上傳失敗: {e}")

if __name__ == "__main__":
    # 【重點升級】：環境偵測
    # 當運行在 GitHub Actions 時，會自帶 GITHUB_ACTIONS=true 環境變數
    if os.getenv("GITHUB_ACTIONS") == "true":
        print("☁️ 偵測到雲端 GitHub Actions 環境，啟動單次排程任務...")
        job()
    else:
        print("💻 偵測到本地開發環境，啟動常駐排程系統...")
        print("每天早上 08:00 將自動執行爬蟲任務。")
        job() 
        schedule.every().day.at("08:00").do(job)
        while True:
            schedule.run_pending()
            time.sleep(60)