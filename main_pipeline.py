import os
import sys
import time
import requests
import schedule
from dotenv import load_dotenv
from pymongo import MongoClient

# 匯入我們自己寫好的爬蟲模組
from scraper_api import get_api_articles
from scraper_fda import get_fda_articles
from scraper_tfc import get_tfc_articles

# 載入環境變數
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")

# 三個來源的正式名稱，與各爬蟲模組回傳的 source 欄位一致。
# 任一來源本次一篇都沒抓到，就是異常——見 find_missing_sources 的說明。
EXPECTED_SOURCES = frozenset({
    # 真正的闢謠專區（scraper_fda，有文章網址）
    "食藥署闢謠專區",
    # 食藥署全站新聞稿 feed（scraper_api，無網址）。2026-08-16 之前這批被
    # 誤標成「食藥署闢謠專區」，見 scraper_api.get_api_articles 的說明。
    "食藥署公告",
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


# 切塊的分隔階層，由粗到細。每一層都保留在切片尾端（`keepends` 語意），
# 因為中文的句號本身就是語意邊界的一部分，切掉會讓片段讀起來像斷句。
# 切塊邏輯的版本。改變切法時要 +1——既有文章會據此逐步重切，不需要手動
# 全刪重灌。ETL 以 url 判定「已存在」會直接跳過，沒有這個標記的話切法一改，
# 舊資料就永遠留在舊邊界上（線上 9,182 個切片都是 2026-08-22 之前的硬切）。
#
# 重切受每日 embedding 額度限制，會分多天完成；額度用盡時本次執行乾淨結束，
# 隔天自然接續（見 DailyQuotaExhausted）。
CHUNKER_VERSION = 2

_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "；", "，")


def _split_keeping_separator(text: str, separator: str) -> list:
    """以 separator 切開，但把 separator 留在前一段的尾端。"""
    parts = text.split(separator)
    out = [p + separator for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _recursive_split(text: str, chunk_size: int, separators) -> list:
    """逐層退讓的切分：先試最粗的分隔，切不夠小才往下一層。

    舊版是 `text[start:start + 500]` 的硬切，完全不看標點——線上 TFC 那批
    平均每篇 1,578 字、切成 4 片，每一片都從句子中間斷開。語意殘缺的片段
    直接進向量空間，也直接被下游拿去改寫理由。

    最後一層仍然是字元硬切，因為單一句子也可能超過 chunk_size（線上實測
    最長的一句有 200 字以上）。那時硬切是唯一選擇，但已經是罕例而非常態。
    """
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    for index, separator in enumerate(separators):
        if separator not in text:
            continue
        pieces = _split_keeping_separator(text, separator)
        # 這一層切不動（例如整段只有一個分隔且在結尾），換下一層
        if len(pieces) <= 1:
            continue

        chunks, buffer = [], ""
        for piece in pieces:
            if len(buffer) + len(piece) <= chunk_size:
                buffer += piece
                continue
            if buffer:
                chunks.append(buffer)
            # 單一片段仍超長時，用更細的分隔再切一次
            if len(piece) > chunk_size:
                chunks.extend(_recursive_split(piece, chunk_size, separators[index + 1:]))
                buffer = ""
            else:
                buffer = piece
        if buffer:
            chunks.append(buffer)
        return [c for c in chunks if c.strip()]

    # 所有分隔都用盡：硬切
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)
            if text[i:i + chunk_size].strip()]


def chunk_text(text: str, chunk_size=500, overlap=50) -> list:
    """把文章切成不超過 chunk_size 的片段，盡量在語意邊界上斷開。

    `overlap` 保留參數以維持既有呼叫端相容，但**不再使用**：邊界感知的切分
    本來就會在完整句子處斷開，前一片的結尾與後一片的開頭不會咬在同一句中間，
    重疊的原始用途（避免句子被切斷而失去語境）已由切分本身解決。

    2026 年 1 月的 arXiv 分析亦指出 overlap 在多數情況下沒有可測效益、只是
    墊高索引成本。若日後要恢復，應先在 golden set 上驗證，不要憑慣例加回來。
    """
    if not text:
        return []
    return _recursive_split(text, chunk_size, _SEPARATORS)

class DailyQuotaExhausted(Exception):
    """今日 embedding 額度已用盡——重試無效，只能等隔天。

    與「每分鐘超速」是兩回事，必須分開處理：超速等幾十秒就會過，額度用盡等到
    明天才會過。舊版把兩者當同一件事，撞到額度上限後仍每 40 秒重試一次，
    2026-08-17 那次因此空轉 4 小時、觸發 357 次，最後被 GitHub Actions 的
    6 小時上限砍掉——那 4 小時沒有寫進任何一筆資料。
    """


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
                    if attempt == max_retries - 1:
                        # 連續重試都撞同一面牆，代表不是短暫超速而是額度用盡。
                        # 以連續失敗次數判定而非解析錯誤訊息裡的 metric 名稱：
                        # 訊息格式由 Google 決定，改版就會失準；「retry in 48s」
                        # 這類提示在每日額度耗盡時也照樣出現，同樣不可信。
                        raise DailyQuotaExhausted(error_msg)
                    print(f"    ⏳ 觸發 API 限制，等待 40 秒後重試... (第 {attempt+1}/{max_retries} 次)")
                    time.sleep(40)
                    continue 
                return []
            return response_data.get("embedding", {}).get("values", [])
        except DailyQuotaExhausted:
            # 必須排在通用 except 之前，否則會被自己的重試迴圈吞掉——
            # 那正是這個例外要終結的行為。
            raise
        except Exception as e:
            print(f"    [向量例外錯誤] {e}")
            time.sleep(5)
    return []

def upload_to_mongodb(articles, collection, *, embed_fn=None):
    """把文章切片、向量化後寫入 MongoDB。

    寫入保證為「全有或全無」：一篇文章的所有 chunk 都成功取得向量才寫入，
    而且寫入中途失敗時會把殘留的切片清掉。任一環節失敗就整篇不留、留待下次
    執行重試——避免產生「宣告 4 塊、實際只有 3 塊」這種破洞（線上 pid=16703
    即為此類實例）。每次執行也會檢查既有文章的實際切片數是否與宣告值相符，
    不符就重寫修復，所以即使破洞真的發生了，下一次執行就會自癒。

    回傳 (new_count, write_failed)：write_failed 為 True 表示至少有一篇文章
    在寫入階段失敗，或本次「有嘗試但一篇都沒成功」（多半是向量化配額用盡）。
    單篇失敗不會中止整批（資料面 fail-open），但會透過這個旗標讓 job()
    以非零狀態碼結束（訊號面 fail-loud）。
    """
    embed_fn = embed_fn or get_embedding
    print(f"\n=== 🚀 開始將 {len(articles)} 篇文章上傳至 MongoDB ===")

    # 一次取回既有鍵，讓「已存在」的判定只作用於單篇，
    # 不再因為某篇已存在就放棄整個來源的後續文章。
    # 標題用 `is not None` 而非真值判斷：空字串是合法的鍵，若被濾掉，
    # 庫中標題為空的文章永遠比對不到，每天都會被當成新文章重新寫入一次。
    existing_urls = {u for u in collection.distinct("url") if u}
    existing_titles = {t for t in collection.distinct("original_title") if t is not None}

    new_count = 0
    write_failed = False
    attempted = 0
    quota_exhausted = False
    embed_failed = 0
    for article in articles:
        url = None
        title = None
        deleted_old = False
        insert_attempted = False
        try:
            url = article.get("url")
            title = article["title"]

            # 決定這一篇要不要重寫。兩種情形會重寫：
            #   (a) 既有切片數與宣告的 total_chunks 不符——破洞，修復它
            #   (b) 來源提供的修改日期與庫中不同——改版
            # 刻意不用內容雜湊比對：那會讓每次清洗邏輯微調都觸發全量重寫。
            # 這裡只做判定，實際刪除延後到 insert_many 之前（見下方），
            # 確保「刪掉舊版卻寫不出新版」這種資料遺失不會發生。
            incoming_updated = article.get("updated_at")
            needs_rewrite = False
            old = collection.find_one(
                {"url": url},
                {"updated_at": 1, "total_chunks": 1, "chunker_version": 1},
            ) if url else None
            if old is not None:
                declared = old.get("total_chunks")
                actual = collection.count_documents({"url": url})
                old_updated = old.get("updated_at")
                old_chunker = old.get("chunker_version", 1)
                if old_chunker != CHUNKER_VERSION:
                    # (c) 切法換了。舊版是不看標點的字元硬切，切片從句子中間
                    # 斷開，語意殘缺的片段直接進向量空間、也直接被下游拿去
                    # 改寫理由。這個檢查讓既有資料逐步重切而不需要人工介入。
                    print(f"  ✂️ 切法已更新（v{old_chunker} → v{CHUNKER_VERSION}），"
                          f"將重切: {title[:15]}...")
                    needs_rewrite = True
                elif declared is not None and actual != declared:
                    # 破洞：舊版逐塊寫入時某塊向量化失敗只印警告、其餘照常寫入；
                    # 或寫入中途失敗留下前綴（insert_many 預設 ordered=True）。
                    # 這篇之後會被判定「已存在」而永遠跳過，破洞不會自己補上。
                    # 線上實測有 71 篇這樣的文章、遺失 141 個切片。
                    # 這個檢查每次執行都跑，不限於尚未補日期的文章，所以是自癒的。
                    print(f"  🔧 既有文章不完整（宣告 {declared} 塊、"
                          f"實際 {actual} 塊），將重寫修復: {title[:15]}...")
                    needs_rewrite = True
                elif incoming_updated and old_updated is None:
                    # 注意優先序：上面的切法版本檢查排在這一支之前，因此切法
                    # 換版的那一輪，這個「只補日期不重算」的優化會被蓋過。那是
                    # 刻意的——切片邊界錯了就沒有「不重算」的餘地，而優化本來
                    # 就是為了避免「無意義」的重算。等全部重切完成之後，這一支
                    # 才會恢復作用。
                    #
                    # 這一篇是本次變更之前寫入的，沒有日期可比對。
                    # 完整的既有文章只補中繼資料、不重新向量化：把「沒有日期」
                    # 當成「日期不同」會讓合併後首次執行重算全部既有切片
                    # （衛福部 2,840 個，每個切片有 2 秒節流，實際要跑數小時
                    # 且極可能耗盡 Gemini 配額），換來的只是內容多半相同的重算。
                    # 補上日期之後，之後每一次真正的改版都能正常偵測。
                    # 代價：若某篇在本次變更之前就已於來源改版，那一次改版會被
                    # 漏掉。這是一次性且有界的，遠低於全量重算的成本。
                    # verdict／claim 一併補上：它們是中繼資料，跟切片內容無關，
                    # 不補的話既有的 TFC 文章會永遠沒有判定標籤——這一支只在
                    # 「已存在」時執行，之後再也不會有機會回頭寫。
                    collection.update_many(
                        {"url": url},
                        {"$set": {
                            "published_at": article.get("published_at"),
                            "updated_at": incoming_updated,
                            "verdict": article.get("verdict"),
                            "verdict_slug": article.get("verdict_slug"),
                            "claim": article.get("claim"),
                        }},
                    )
                    print(f"  📌 補上日期欄位（既有資料，不重算向量）: {title[:15]}...")
                elif incoming_updated and old_updated != incoming_updated:
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
            attempted += 1
            vectors = []
            failed = False
            for i, chunk in enumerate(chunks):
                try:
                    vector = embed_fn(f"主題：{title}\n內容：{chunk}")
                except DailyQuotaExhausted:
                    # 本篇未完成的切片不寫入（維持「全有或全無」），且不再處理
                    # 後續文章——今天剩下的每一次呼叫都注定失敗。ETL 是增量的，
                    # 隔天會從這裡接續。
                    print(f"\n⏸️ 今日 embedding 額度已用盡，停止本次執行。")
                    print(f"   已完成的文章都已寫入，未處理的留待下次執行接續。")
                    quota_exhausted = True
                    failed = True
                    break
                if not vector:
                    print(f"    ⚠️ 第 {i+1}/{len(chunks)} 個切片向量化失敗——"
                          f"整篇跳過，留待下次執行重試")
                    failed = True
                    break
                vectors.append(vector)
            if failed:
                embed_failed += 1
                if quota_exhausted:
                    break
                continue

            # verdict／claim 只有查核型來源（TFC）給得出來，其餘來源為 None。
            # 刻意寫進每個 chunk 而不是另開集合：下游 RAG 是以 chunk 為單位
            # 檢索的，判定必須跟著檢索結果一起回去，否則還要多一次查詢。
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
                    "verdict": article.get("verdict"),
                    "verdict_slug": article.get("verdict_slug"),
                    "claim": article.get("claim"),
                    "chunker_version": CHUNKER_VERSION,
                }
                for i, (chunk, vector) in enumerate(zip(chunks, vectors))
            ]
            if needs_rewrite:
                collection.delete_many({"url": url})
                deleted_old = True
            insert_attempted = True
            collection.insert_many(docs)
            print(f"    ✅ 成功寫入 {len(docs)} 個切片")

            # 讓同一批次內的重複文章也能被擋掉
            if url:
                existing_urls.add(url)
            existing_titles.add(title)
            new_count += 1
        except Exception as e:
            # 單篇失敗不連累整批：資料面能寫多少寫多少。
            # 但一定要回報，讓 job() 以非零狀態碼結束——訊號面 fail-loud。
            write_failed = True
            ident = article.get("url") or article.get("title") or "（無法辨識）"
            print(f"  ❌ 這篇處理失敗，其餘文章照常繼續：{ident} —— {type(e).__name__}: {e}")
            if insert_attempted:
                # insert_many 預設 ordered=True：伺服器逐筆寫入，中途出錯只中止
                # 「剩下的」，已經寫進去的不會回滾。若放著不管，這一篇就會變成
                # 「宣告 N 塊、實際少於 N 塊」——正是本次變更要消滅的破洞形態。
                # 清乾淨，讓它下次執行以全新文章重新寫入。
                try:
                    collection.delete_many(
                        {"url": url} if url else {"original_title": title})
                    print("     🧹 已清除本篇殘留的切片，下次執行會重新寫入")
                except Exception as cleanup_error:
                    print(f"     ⚠️ 清除殘留切片失敗："
                          f"{type(cleanup_error).__name__}: {cleanup_error}")
                    print("        本篇可能留下不完整的切片；"
                          "下次執行的完整性檢查會偵測並修復。")
            elif deleted_old:
                print("     ⚠️ 舊版切片已刪除但新版尚未寫入。此 URL 已不在庫中，"
                      "下次執行會當成全新文章重新寫入，暴露時間最長一個排程週期。")

    if embed_failed:
        print(f"\n⚠️ 本次有 {embed_failed} 篇文章因向量化失敗而未寫入，留待下次執行重試。")
    if quota_exhausted and new_count:
        # 有進度就不算失敗：額度用盡是已知、有界、隔天自解的情況，而 ETL 本身
        # 是增量的。天天紅燈只會訓練維護者忽略 CI，那會讓真正的故障也被忽略。
        print(f"\n⏸️ 本次因每日額度用盡提前結束，已寫入 {new_count} 篇。")
        print("   剩餘文章將於下次執行接續，不需要人工介入。")
    if attempted and new_count == 0:
        # 系統性向量化失敗（最常見的是 Gemini 配額用盡）會讓整批一篇都寫不進去，
        # 而來源檢查完全看不到——爬蟲是成功的，三個來源都有回傳文章。
        # 沒有這個判斷，知識庫可以連續數週停止更新而 CI 一路綠燈。
        # 刻意只在「有嘗試但一篇都沒成功」時判定：單篇偶發失敗下次執行就會補上，
        # 不值得每天紅燈——那只會訓練維護者忽略 CI。
        print(f"\n❌ 嚴重：本次嘗試處理 {attempted} 篇文章，但一篇都沒有成功寫入。")
        print("   最可能的原因是 Gemini 向量化配額用盡或 API 失效。")
        print("   本次執行將以非零狀態碼結束。")
        write_failed = True

    return new_count, write_failed

def _default_collection():
    """正式環境的 collection。獨立成函式，讓 job() 能以假件測試而不碰真實資料庫。"""
    client = MongoClient(MONGO_URI)
    return client["CARE_database"]["health_articles_chunks"]


def job(*, fetchers=None, collection_factory=None, embed_fn=None):
    """執行一次完整 ETL。回傳 0 表示正常，1 表示有來源全滅或寫入失敗。

    三個關鍵字參數是給測試用的依賴注入點，預設為正式環境的爬蟲模組、
    MongoDB collection 與 Gemini 向量化。退出碼的判定邏輯是本管線的
    核心保證之一，必須能在不發出任何網路請求的情況下驗證。
    """
    if fetchers is None:
        fetchers = (
            lambda: get_api_articles(test_mode=False),
            lambda: get_fda_articles(test_mode=False),
            lambda: get_tfc_articles(test_mode=False),
        )
    collection_factory = collection_factory or _default_collection

    print(f"\n=== 🟢 [{time.strftime('%Y-%m-%d %H:%M:%S')}] 啟動正式爬蟲任務 ===")
    print("\n[階段一：呼叫爬蟲模組提取資料]")
    all_articles = []

    for fetch in fetchers:
        all_articles.extend(fetch())

    print(f"\n🏁 階段一完成！總共收集到 {len(all_articles)} 篇待處理的文章。")

    exit_code = 0
    missing = find_missing_sources(all_articles)
    if missing:
        print(f"\n❌ 嚴重：以下來源本次完全沒有取得任何文章：{'、'.join(sorted(missing))}")
        print("   這通常代表爬蟲失效、來源網站改版、或網路／憑證問題。")
        print("   請檢查上方該來源的錯誤訊息。本次執行將以非零狀態碼結束。")
        exit_code = 1

    # 刻意不在來源缺漏時提早 return：其餘來源的文章仍應照常寫入知識庫。
    # 一個來源的暫時問題不該阻擋另外兩個來源的正常更新（資料面 fail-open），
    # 但這次執行仍會以非零狀態碼結束（訊號面 fail-loud）。
    print("\n[階段二：切片與上傳]")
    try:
        collection = collection_factory()
        total_new, write_failed = upload_to_mongodb(
            all_articles, collection, embed_fn=embed_fn)
        print(f"\n=== 🔴 [{time.strftime('%Y-%m-%d %H:%M:%S')}] 任務結束！"
              f"成功寫入 {total_new} 篇文章（新增與改版合計） ===")
        if write_failed:
            print("❌ 嚴重：有文章在寫入階段失敗（詳見上方訊息）。")
            print("   本次執行將以非零狀態碼結束。")
            exit_code = 1
    except Exception as e:
        print(f"❌ 嚴重：MongoDB 連線或上傳失敗: {e}")
        print("   本次執行將以非零狀態碼結束。")
        exit_code = 1

    return exit_code

def main(env=None, *, job_fn=None):
    """環境偵測與退出碼決策。回傳要交給作業系統的退出碼。

    GitHub Actions 會自帶 `GITHUB_ACTIONS=true`。在該模式下這是一次性執行，
    直接回傳 `job()` 的退出碼讓 Actions 顯示紅燈；本機則是常駐排程，
    先跑一次再進入迴圈，**刻意不因單次失敗終止程序**——開發時不該因為
    一次網路問題就讓排程死掉。

    參數是給測試用的注入點：常駐模式的無限迴圈無法在測試中執行，
    但「哪一種模式回傳什麼退出碼」這條規則必須測得到。
    """
    env = os.environ if env is None else env
    job_fn = job_fn or job

    if env.get("GITHUB_ACTIONS") == "true":
        print("☁️ 偵測到雲端 GitHub Actions 環境，啟動單次排程任務...")
        return job_fn()          # 非零退出碼讓 Actions 顯示紅燈

    print("💻 偵測到本地開發環境，啟動常駐排程系統...")
    print("每天早上 08:00 將自動執行爬蟲任務。")
    job_fn()                     # 常駐模式不因單次失敗結束程序
    schedule.every().day.at("08:00").do(job_fn)
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())