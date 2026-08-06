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

def upload_to_mongodb(articles, collection):
    print(f"\n=== 🚀 開始將 {len(articles)} 篇文章上傳至 MongoDB ===")
    new_count = 0
    skipped_sources = set()
    
    for article in articles:
        source_name = article["source"]
        if source_name in skipped_sources:
            continue

        query = {"$or": [{"url": article["url"]}, {"original_title": article["title"]}]}
        if article["url"] is None:
            query = {"original_title": article["title"]}

        if collection.find_one(query):
            print(f"  ⏭️ [{source_name}] 發現已存在資料: {article['title'][:15]}...")
            print(f"     -> 🛑 觸發提早結束機制，跳過【{source_name}】後續所有文章！")
            skipped_sources.add(source_name)
            continue
        
        print(f"  🆕 [處理中] 向量化並上傳: {article['title'][:15]}...")
        chunks = chunk_text(article["content"])
        
        chunks_inserted = 0
        for i, chunk in enumerate(chunks):
            vector = get_embedding(f"主題：{article['title']}\n內容：{chunk}")
            if vector:
                collection.insert_one({
                    "source_name": article["source"],
                    "url": article["url"],
                    "original_title": article["title"],
                    "chunk_content": chunk,
                    "chunk_index": i + 1,
                    "total_chunks": len(chunks),
                    "embedding": vector,
                    "uploaded_at": time.time()
                })
                chunks_inserted += 1
            else:
                print(f"    ⚠️ 第 {i+1} 個切片失敗！")
                
        if chunks_inserted > 0:
            print(f"    ✅ 成功寫入 {chunks_inserted}/{len(chunks)} 個切片")
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