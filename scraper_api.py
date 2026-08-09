# scraper_api.py
import time
import requests
import urllib3
from utils import clean_html

# 關閉憑證警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_api_articles(test_mode=False):
    """
    爬取政府公開資料 API (食藥署、衛福部闢謠專區)
    :param test_mode: 若為 True，每個來源只抓取前 3 篇作為測試。
    :return: 回傳包含字典的 List，格式與 TFC 爬蟲完全相同
    """
    api_sources = [
        {"url": "https://www.fda.gov.tw/DataAction", "name": "食藥署闢謠專區"},
        {"url": "https://www.hpa.gov.tw/wf/newsapi.ashx", "name": "衛福部闢謠網站"}
    ]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    
    all_articles = []
    
    for source in api_sources:
        print(f"\n[{source['name']}] 開始抓取 API: {source['url']}")
        try:
            time.sleep(1) 
            response = requests.get(source['url'], headers=headers, timeout=15, verify=False)
            response.raise_for_status()
            raw_data = response.json()
            
            # 相容不同 API 的回傳結構
            items = raw_data if isinstance(raw_data, list) else raw_data.get("data", [])
            
            cleaned_articles = []
            for item in items:
                # 處理欄位名稱不一致的問題 (食藥署 vs 衛福部)
                raw_title = item.get("\u6a19\u984c", item.get("Title", item.get("title", "")))
                raw_content = item.get("\u5167\u5bb9", item.get("Content", item.get("content", "")))
                raw_url = item.get("\u9023\u7d50\u7db2\u5740", item.get("url", item.get("Url", item.get("URL", item.get("連結", None)))))
                if raw_url == "": raw_url = None

                if not raw_title and not raw_content: continue

                cleaned_articles.append({
                    "title": raw_title.strip(),
                    "content": clean_html(raw_content), # 使用共用工具清洗
                    "source": source["name"],
                    "url": raw_url,
                    # 兩支 API 都有「發布日期」；只有 HPA 有「修改日期」，
                    # 食藥署取不到時為 None，代表該來源無法偵測更新。
                    "published_at": item.get("發布日期") or item.get("PublishDate"),
                    "updated_at": item.get("修改日期"),
                })
            
            print(f"  -> 成功清洗 {len(cleaned_articles)} 篇資料")
            
            # 如果是測試模式，只取前三筆
            if test_mode:
                all_articles.extend(cleaned_articles[:3])
            else:
                all_articles.extend(cleaned_articles)
                
        except Exception as e:
            print(f"[{source['name']}] 爬取失敗: {e}")
            
    return all_articles

# ==========================================
# 本地測試區塊
# ==========================================
if __name__ == "__main__":
    print("=== 測試 API 獨立模組 ===")
    articles = get_api_articles(test_mode=False)
    
    print(f"\n=== 總共取得 {len(articles)} 篇測試文章 ===")
    for i, art in enumerate(articles):
        print(f"[{i+1}] 來源: {art['source']} | 標題: {art['title'][:20]}...")