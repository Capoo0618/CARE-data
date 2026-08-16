# scraper_api.py
import time
import requests
from ca_bundle import get_ca_bundle
from utils import clean_html

# 食藥署 `DataAction` 是全站新聞稿 feed，裡面約兩成是與衛教無關的行政公告
# （法規預告、研討會、表揚、會議紀錄）。它們進了知識庫只會在檢索時跟真正的
# 衛教內容競爭名額，所以在來源端就擋掉。
#
# 關鍵字是照實際標題校準的，不是憑印象列的：對線上 706 篇比對後命中 129 篇
# (18%)，逐條抽查確認沒有誤殺。「活動」刻意不列入——它只多命中一篇
# 〈食藥署澄清107年並未邀請蕾菈參加本署反毒活動〉，而那其實是澄清稿。
ADMIN_NOISE_KEYWORDS = (
    "預告", "研討會", "會議", "表揚", "圓滿落幕", "公告修正", "訪查", "頒獎",
    "成果發表", "簽署", "論壇", "座談會", "評鑑", "備忘錄", "揭牌", "記者會",
    "招標", "徵才", "人事", "研習", "開幕", "參訪", "年度計畫", "培訓",
    "觀摩", "競賽", "徵選", "頒發", "授證",
)


def is_admin_notice(title: str) -> bool:
    """這篇是行政公告（而非衛教內容）嗎？"""
    return any(kw in (title or "") for kw in ADMIN_NOISE_KEYWORDS)

def get_api_articles(test_mode=False):
    """
    爬取政府公開資料 API (食藥署公告、衛福部闢謠專區)
    :param test_mode: 若為 True，每個來源只抓取前 3 篇作為測試。
    :return: 回傳包含字典的 List，格式與 TFC 爬蟲完全相同

    食藥署這個 `DataAction` 端點在 2026-08-16 之前被標成「食藥署闢謠專區」，
    那是誤標：它回傳的是**全站新聞稿 feed**，706 篇裡只有 6 篇標題含「謠」字。
    真正的闢謠專區在 news.aspx?cid=5049，已另由 scraper_fda.py 負責。

    這個 feed 仍然保留，因為它跟闢謠專區**完全不重疊**，且含有真正有用的
    衛教內容（用藥安全、藥品保存與丟棄）。只做兩件事：改成誠實的來源名
    「食藥署公告」，並在來源端濾掉行政公告。

    已知限制：這個端點結構上就不提供文章網址，因此本來源的 url 恆為 None，
    答案中無法附上可點的連結。闢謠專區沒有這個問題。
    """
    api_sources = [
        {"url": "https://www.fda.gov.tw/DataAction", "name": "食藥署公告"},
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
            response = requests.get(source['url'], headers=headers, timeout=15, verify=get_ca_bundle())
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

                # 標題或內容任一為空就跳過（原本是 and，兩者都空才跳過）。
                # 沒有標題的文章去重只能靠空字串當鍵，而且向量化的輸入會變成
                # 「主題：（空）內容：…」，檢索品質明顯較差——不如不收。
                if not raw_title or not raw_content: continue

                # 行政公告不進知識庫（見 ADMIN_NOISE_KEYWORDS）。只套用在
                # 食藥署那個 feed——衛福部闢謠網站本來就沒有這類內容。
                if source["name"] == "食藥署公告" and is_admin_notice(raw_title):
                    continue

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