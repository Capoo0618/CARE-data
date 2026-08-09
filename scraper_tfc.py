import time
import requests
from bs4 import BeautifulSoup
from ca_bundle import get_ca_bundle
from utils import clean_html

def get_tfc_articles(test_mode=False, start_page=1, max_pages=2): # 注意這裡把 start_page 預設改成 1 了
    source_name = "台灣事實查核中心"
    
    # 基礎網址，用來組合翻頁
    base_url = "https://tfc-taiwan.org.tw/fact-check-report-type/health"
    print(f"\n[{source_name}] 開始進行網頁爬蟲 (使用 /page/X/ 結構)...")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    cleaned_articles = []
    limit_pages = 1 if test_mode else max_pages
    
    # 用來記錄上一頁抓到的網址，防止伺服器鬼打牆一直給同一頁
    previous_page_links = set() 

    for current_page in range(start_page, start_page + limit_pages):
        # 【修正策略】：根據你的發現，動態組合網址
        if current_page == 1:
            page_url = f"{base_url}/" # 第一頁通常沒有 /page/1/
        else:
            page_url = f"{base_url}/page/{current_page}/"
            
        print(f"\n  📄 正在爬取第 {current_page} 頁的列表: {page_url}")
        
        try:
            response = requests.get(page_url, headers=headers, timeout=15, verify=get_ca_bundle())
            
            # 如果對方伺服器對於不存在的頁面回傳 404，requests 會拋出 HTTPError，我們可以藉此知道翻到底了
            if response.status_code == 404:
                print(f"  -> 遇到 404 網頁不存在，已到達盡頭，結束翻頁。")
                break
                
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            a_tags = soup.find_all('a')
            found_links = {}
            
            for a in a_tags:
                title = a.get_text(strip=True)
                link = a.get('href')
                if not link or link == "#": continue
                
                if not title or "Read More" in title or "閱讀更多" in title:
                    continue
                
                if '/articles/' in link or '/fact-check-reports/' in link:
                    if link.startswith('/'):
                        link = "https://tfc-taiwan.org.tw" + link
                    
                    if link not in found_links or len(title) > len(found_links.get(link, "")):
                        found_links[link] = title
                        
            # 防呆機制 1：如果這一頁找不到任何文章，直接結束
            if not found_links:
                print(f"  -> 找不到任何查核報告，已到達盡頭，結束翻頁。")
                break
                
            # 【防呆機制 2：鬼打牆偵測】：判斷這頁抓到的跟上一頁是不是完全一樣
            current_page_links = set(found_links.keys())
            if current_page_links == previous_page_links:
                print(f"  -> 🛑 第 {current_page} 頁的內容與上一頁完全重複！代表伺服器拒絕往下翻頁，已強制提早結束。")
                break
            
            previous_page_links = current_page_links
                
            print(f"  -> 在第 {current_page} 頁找到了 {len(found_links)} 篇查核報告！準備深入抓取...")
            
            # 既然我們又回到健康分類專區了，就可以不用 health_keywords 過濾了，直接全抓！
            for link, title in found_links.items():
                print(f"    [深入抓取] {title[:30]}...") 
                time.sleep(2)
                
                try:
                    detail_res = requests.get(link, headers=headers, timeout=15, verify=get_ca_bundle())
                    detail_soup = BeautifulSoup(detail_res.content, 'html.parser')
                    paragraphs = detail_soup.find_all('p')
                    raw_content = " ".join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20])
                    
                    if raw_content:
                        cleaned_articles.append({
                            "title": title,
                            "content": clean_html(raw_content),
                            "source": source_name,
                            "url": link,
                            "published_at": None,
                            "updated_at": None,
                        })
                        
                    if test_mode and len(cleaned_articles) >= 3:
                        break
                        
                except Exception as e:
                    print(f"    深入抓取失敗 {link}: {e}")
            
            print(f"  -> 第 {current_page} 頁爬取完畢。")
            
            if test_mode and len(cleaned_articles) >= 3:
                break
                
        except requests.exceptions.RequestException as e:
            print(f"[{source_name}] 第 {current_page} 頁列表爬取失敗或網頁不存在: {e}")
            break
            
        time.sleep(2)
            
    print(f"\n[{source_name}] 爬蟲任務完畢！最終成功取得 {len(cleaned_articles)} 篇健康文章")
    return cleaned_articles

if __name__ == "__main__":
    print("=== 測試 TFC 獨立翻頁爬蟲模組 ===")
    # 把 test_mode 關掉，我們來測測看能不能成功翻到第二頁之後！
    # 為了避免等太久，我們先把 max_pages 設為 2，觀察第一頁跟第二頁的標題有沒有不一樣就好
    articles = get_tfc_articles(test_mode=False, start_page=1, max_pages=2)