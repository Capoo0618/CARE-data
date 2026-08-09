import unittest
import requests
from bs4 import BeautifulSoup
from ca_bundle import get_ca_bundle
from utils import clean_html
from scraper_api import get_api_articles
from scraper_tfc import get_tfc_articles


class FakeCollection:
    """記錄呼叫的假 collection，讓寫入邏輯可在無網路下測試。"""

    def __init__(self, existing=None):
        self.docs = list(existing or [])
        self.inserted_batches = []
        self.deleted_filters = []
        self.update_many_calls = []

    def distinct(self, field):
        return [d.get(field) for d in self.docs if d.get(field) is not None]

    def find_one(self, query, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None

    def insert_many(self, docs):
        self.inserted_batches.append(list(docs))
        self.docs.extend(docs)

    def delete_many(self, query):
        self.deleted_filters.append(query)
        url = query.get("url")
        self.docs = [d for d in self.docs if d.get("url") != url]

    def update_many(self, filt, update):
        """只支援 {"url": ...} 條件與 $set —— 與 main_pipeline 實際用法一致。"""
        url = filt.get("url")
        changed = 0
        for doc in self.docs:
            if doc.get("url") == url:
                doc.update(update["$set"])
                changed += 1
        self.update_many_calls.append((filt, update))
        return changed

    def count_documents(self, filt):
        """只支援 {"url": ...} 條件 —— 與 main_pipeline 實際用法一致。"""
        url = filt.get("url")
        return sum(1 for doc in self.docs if doc.get("url") == url)


def fake_embed_ok(text):
    return [0.1, 0.2, 0.3]


def make_failing_embed(fail_on_nth):
    """第 fail_on_nth 次呼叫回傳空 list（模擬向量化失敗）。"""
    state = {"n": 0}

    def _embed(text):
        state["n"] += 1
        return [] if state["n"] == fail_on_nth else [0.1, 0.2, 0.3]

    return _embed


class TestHealthETLPipeline(unittest.TestCase):
    
    def test_01_clean_html_cases(self):
        """測試要求 1：資料清洗的「多組測試案例 (Test Cases)」"""
        # 定義多組邊界測試 (Test Cases)
        cases = [
            # 案例 A：一般 HTML 標籤
            {"input": "<p>這是<b>測試</b></p>", "expected": "這是測試"},
            # 案例 B：連續多餘空白與換行
            {"input": "  很多 \n\n 空白   和 \t 換行  ", "expected": "很多 空白 和 換行"},
            # 案例 C：HTML 實體字元轉義 (Entity Unescape)
            {"input": "&lt;這不是標籤&gt; &amp; &nbsp;空白", "expected": "<這不是標籤> & 空白"},
            # 案例 D：極端案例 (None 或空字串)
            {"input": None, "expected": ""},
            {"input": "", "expected": ""}
        ]
        
        for i, case in enumerate(cases):
            with self.subTest(case_index=i):
                result = clean_html(case["input"])
                self.assertEqual(result, case["expected"], f"第 {i} 組測試案例失敗：輸入 {case['input']}")

    def test_02_api_data_integrity(self):
        """測試要求 2：確保 API 爬蟲資料與來源網站「一模一樣」"""
        # 1. 模擬人工：當下直接去食藥署 API 看最原始的第一筆資料
        raw_url = "https://www.fda.gov.tw/DataAction"
        res = requests.get(raw_url, verify=get_ca_bundle(), timeout=10)
        raw_data = res.json()
        # 食藥署的標題欄位可能是 "標題" 或 "Title"
        expected_title = raw_data[0].get("標題", raw_data[0].get("Title", "")).strip()
        
        # 2. 呼叫我們的模組
        articles = get_api_articles(test_mode=True)
        
        # 3. 找出模組抓到的「食藥署」第一篇文章
        fda_first_article = next(art for art in articles if art["source"] == "食藥署闢謠專區")
        
        # 4. 斷言比對 (Data Integrity Check)
        print(f"\n    🔍 [API 來源原始資料] 最新標題: {expected_title}")
        print(f"    ✅ [API 爬蟲模組產出] 最終標題: {fda_first_article['title']}")
        
        self.assertEqual(fda_first_article["title"], expected_title, 
                         "API 爬蟲抓取的標題與來源 API 原始資料不一致！")
        
        # 確保格式欄位齊全
        self.assertIn("content", fda_first_article)
        self.assertIn("url", fda_first_article)

    def test_03_tfc_data_integrity(self):
        """測試要求 2：確保 TFC 網頁爬蟲資料與網站「一模一樣」"""
        # 1. 模擬人工：當下用最原始的方式去 TFC 健康專區把第一篇文章標題硬生生抓下來
        raw_url = "https://tfc-taiwan.org.tw/fact-check-report-type/health/"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(raw_url, headers=headers, verify=get_ca_bundle(), timeout=10)
        soup = BeautifulSoup(res.content, 'html.parser')
        
        expected_title = ""
        # 尋找第一個真正的文章標題 (避開 Read More)
        for a in soup.find_all('a'):
            title = a.get_text(strip=True)
            link = a.get('href')
            if link and ('/articles/' in link or '/fact-check-reports/' in link):
                if title and "Read More" not in title and "閱讀更多" not in title:
                    expected_title = title
                    break
                    
        # 2. 呼叫我們的爬蟲模組
        articles = get_tfc_articles(test_mode=True)
        tfc_first_article = articles[0]
        
        # 3. 斷言比對 (保證爬蟲沒有漏字、沒有切錯)
        print(f"\n    🔍 [TFC 網頁當前顯示] 最新標題: {expected_title}")
        print(f"    ✅ [TFC 爬蟲模組產出] 最終標題: {tfc_first_article['title']}")
        
        self.assertEqual(tfc_first_article["title"], expected_title, 
                         "TFC 爬蟲抓取的標題與網頁當前顯示的第一篇標題不一致！")
        
        # 確保格式欄位齊全
        self.assertIn("content", tfc_first_article)
        self.assertTrue(len(tfc_first_article["content"]) > 50, "內文長度過短，可能抓取失敗")

    def test_04_partial_embedding_failure_writes_nothing(self):
        """要求：任一 chunk 向量化失敗，整篇都不得寫入（不留破洞）"""
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection()
        article = {
            "title": "測試文章",
            "content": "第一句。" * 200,   # 確保會切成多個 chunk
            "source": "測試來源",
            "url": "https://example.com/a",
        }

        upload_to_mongodb([article], collection, embed_fn=make_failing_embed(2))

        self.assertEqual(collection.inserted_batches, [],
                         "有 chunk 向量化失敗時，不應寫入任何一筆")

    def test_05_all_chunks_succeed_writes_once(self):
        """要求：全部成功時以單次批次寫入，且 total_chunks 與實際筆數一致"""
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection()
        article = {
            "title": "測試文章",
            "content": "第一句。" * 200,
            "source": "測試來源",
            "url": "https://example.com/a",
        }

        upload_to_mongodb([article], collection, embed_fn=fake_embed_ok)

        self.assertEqual(len(collection.inserted_batches), 1, "應該只有一次批次寫入")
        batch = collection.inserted_batches[0]
        self.assertTrue(len(batch) > 0)
        for doc in batch:
            self.assertEqual(doc["total_chunks"], len(batch),
                             "total_chunks 必須等於實際寫入筆數")

    def test_06_existing_article_does_not_skip_rest_of_source(self):
        """要求：某篇已存在不得導致同來源後續文章被跳過"""
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection(existing=[
            {"url": "https://example.com/old", "original_title": "舊文章"},
        ])
        articles = [
            {"title": "舊文章", "content": "內容。", "source": "同一來源",
             "url": "https://example.com/old"},
            {"title": "新文章", "content": "內容。", "source": "同一來源",
             "url": "https://example.com/new"},
        ]

        upload_to_mongodb(articles, collection, embed_fn=fake_embed_ok)

        written_urls = {d["url"] for b in collection.inserted_batches for d in b}
        self.assertIn("https://example.com/new", written_urls,
                      "第 2 篇是新文章，不應因為第 1 篇已存在而被跳過")

    def test_07_url_none_articles_dedup_by_title_only(self):
        """要求：url 為 None 的文章（食藥署）以標題去重，且不同標題不得互相碰撞"""
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection(existing=[
            {"url": None, "original_title": "已存在的文章"},
        ])
        articles = [
            {"title": "已存在的文章", "content": "內容。", "source": "食藥署闢謠專區", "url": None},
            {"title": "全新文章A", "content": "內容。", "source": "食藥署闢謠專區", "url": None},
            {"title": "全新文章B", "content": "內容。", "source": "食藥署闢謠專區", "url": None},
        ]

        upload_to_mongodb(articles, collection, embed_fn=fake_embed_ok)

        written = {d["original_title"] for b in collection.inserted_batches for d in b}
        self.assertNotIn("已存在的文章", written, "標題已存在者應跳過")
        self.assertEqual(written, {"全新文章A", "全新文章B"},
                         "兩篇 url 皆為 None 但標題不同的文章，不得被視為重複")

    def test_08_updated_article_replaces_old_chunks(self):
        """要求：來源修改日期改變時，舊 chunk 全部清掉重寫"""
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection(existing=[
            {"url": "https://example.com/a", "original_title": "文章",
             "updated_at": "2026-01-01", "chunk_index": 1},
        ])
        article = {
            "title": "文章", "content": "新內容。", "source": "來源",
            "url": "https://example.com/a",
            "published_at": "2025-12-01", "updated_at": "2026-08-01",
        }

        upload_to_mongodb([article], collection, embed_fn=fake_embed_ok)

        self.assertIn({"url": "https://example.com/a"}, collection.deleted_filters,
                      "修改日期不同時，應先刪除該 url 的既有 chunk")
        self.assertEqual(len(collection.inserted_batches), 1, "應重新寫入新版本")
        self.assertEqual(collection.inserted_batches[0][0]["updated_at"], "2026-08-01")

    def test_09_unchanged_article_is_skipped(self):
        """要求：修改日期相同時維持跳過，不重複嵌入"""
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection(existing=[
            {"url": "https://example.com/a", "original_title": "文章",
             "updated_at": "2026-08-01"},
        ])
        article = {
            "title": "文章", "content": "內容。", "source": "來源",
            "url": "https://example.com/a", "updated_at": "2026-08-01",
        }

        upload_to_mongodb([article], collection, embed_fn=fake_embed_ok)

        self.assertEqual(collection.inserted_batches, [], "沒有更新就不該重寫")
        self.assertEqual(collection.deleted_filters, [], "沒有更新就不該刪除")

    def test_10_failed_rewrite_does_not_delete_old_version(self):
        """要求：改版重寫時若向量化失敗，不得刪除舊版本（避免資料遺失）"""
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection(existing=[
            {"url": "https://example.com/a", "original_title": "文章",
             "updated_at": "2026-01-01", "chunk_index": 1},
        ])
        article = {
            "title": "文章", "content": "第一句。" * 200, "source": "來源",
            "url": "https://example.com/a",
            "published_at": "2025-12-01", "updated_at": "2026-08-01",
        }

        upload_to_mongodb([article], collection, embed_fn=make_failing_embed(2))

        self.assertEqual(collection.deleted_filters, [],
                         "向量化失敗時不得刪除舊版本")
        self.assertEqual(collection.inserted_batches, [], "也不應寫入新版本")
        self.assertTrue(any(d.get("url") == "https://example.com/a"
                            for d in collection.docs),
                        "舊版本必須原封不動留在庫中")

    def test_11_ca_bundle_contains_pinned_intermediate(self):
        """要求：CA bundle 同時含 certifi 根憑證與釘選的 TWCA 中繼憑證"""
        import os

        import certifi
        from ca_bundle import _PINNED_DIR, get_ca_bundle

        bundle_path = get_ca_bundle()
        bundle = open(bundle_path, encoding="utf-8").read()
        certifi_content = open(certifi.where(), encoding="utf-8").read()
        pinned = open(os.path.join(_PINNED_DIR, "twca_secure_ssl_ca.pem"),
                     encoding="utf-8").read()

        self.assertIn(pinned.strip(), bundle, "bundle 必須包含釘選的 TWCA 中繼憑證")
        self.assertIn(certifi_content[:200], bundle, "bundle 必須保留 certifi 的根憑證清單")
        self.assertGreater(len(bundle), len(certifi_content),
                           "bundle 應為 certifi 的超集，而非取代它")

    def test_12_find_missing_sources(self):
        """要求：能偵測出「某個來源本次一篇都沒抓到」"""
        from main_pipeline import EXPECTED_SOURCES, find_missing_sources

        full = [{"source": s} for s in EXPECTED_SOURCES]
        self.assertEqual(find_missing_sources(full), set(),
                         "三個來源都有產出時不應回報缺漏")

        without_hpa = [a for a in full if a["source"] != "衛福部闢謠網站"]
        self.assertEqual(find_missing_sources(without_hpa), {"衛福部闢謠網站"},
                         "衛福部全滅時必須被指名")

        self.assertEqual(find_missing_sources([]), set(EXPECTED_SOURCES),
                         "完全沒抓到任何文章時，三個來源都算缺漏")

        # 數量不影響判定——只要有產出就算通過
        one_each = [{"source": s} for s in EXPECTED_SOURCES]
        self.assertEqual(find_missing_sources(one_each), set())

    def test_13_legacy_article_without_updated_at_is_backfilled_not_reembedded(self):
        """既有資料沒有 updated_at 時，只補日期欄位，不刪除也不重新向量化。

        線上既有文件是本次變更之前寫入的，一律沒有 updated_at。若把「沒有」
        當成「不同」，合併後首次執行會重算全部 2,840 個切片（約數小時、
        極可能耗盡配額），而換得的只是內容多半相同的重算。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([
            {"url": "https://example.tw/a", "original_title": "舊文",
             "chunk_content": "舊內容", "chunk_index": 1, "total_chunks": 1,
             "embedding": [0.1]},
        ])
        article = {
            "source": "衛福部闢謠網站", "url": "https://example.tw/a",
            "title": "舊文", "content": "新內容",
            "published_at": "2024/01/01", "updated_at": "2024/03/15",
        }

        calls = []

        def counting_embed(text):
            calls.append(text)
            return [0.5] * 3072

        upload_to_mongodb([article], collection, embed_fn=counting_embed)

        self.assertEqual(calls, [], "既有資料只需補日期，不應重新呼叫向量化 API")
        self.assertEqual(collection.deleted_filters, [], "不應刪除任何既有切片")
        self.assertEqual(len(collection.docs), 1, "切片數不應改變")
        self.assertEqual(collection.docs[0]["updated_at"], "2024/03/15",
                         "應補上 updated_at，否則之後真正的改版永遠偵測不到")
        self.assertEqual(collection.docs[0]["published_at"], "2024/01/01")
        self.assertEqual(collection.docs[0]["chunk_content"], "舊內容",
                         "內容不應被更動——本次只補中繼資料")

    def test_14_write_failure_skips_one_article_and_continues(self):
        """單篇寫入失敗只跳過該篇，其餘照常處理，並回報 write_failed=True。

        設計原則：資料面 fail-open（能寫多少寫多少）、訊號面 fail-loud
        （回傳值讓 job() 以非零狀態碼結束）。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([])
        original_insert = collection.insert_many

        def failing_first_insert(docs):
            if docs[0]["url"] == "https://example.tw/bad":
                raise RuntimeError("模擬 Atlas 寫入失敗")
            return original_insert(docs)

        collection.insert_many = failing_first_insert

        articles = [
            {"source": "衛福部闢謠網站", "url": "https://example.tw/bad",
             "title": "會失敗的文章", "content": "內容", "updated_at": None},
            {"source": "衛福部闢謠網站", "url": "https://example.tw/ok",
             "title": "後面的文章", "content": "內容", "updated_at": None},
        ]

        new_count, write_failed = upload_to_mongodb(
            articles, collection, embed_fn=fake_embed_ok)

        self.assertTrue(write_failed, "寫入失敗必須回報，否則 CI 不會紅燈")
        self.assertEqual(new_count, 1, "後面的文章不應被前一篇的例外連累")
        titles = {d["original_title"] for d in collection.docs}
        self.assertEqual(titles, {"後面的文章"})

    def test_15_duplicate_article_in_same_batch_written_once(self):
        """同一批次內出現兩次的文章只寫入一次。

        來源翻頁重疊或改版偵測都可能讓同一篇出現兩次；若沒有批次內去重，
        知識庫會出現重複切片，直接汙染下游 RAG 的檢索結果。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([])
        article = {
            "source": "台灣事實查核中心", "url": "https://example.tw/dup",
            "title": "重複的文章", "content": "內容", "updated_at": None,
        }

        new_count, write_failed = upload_to_mongodb(
            [article, dict(article)], collection, embed_fn=fake_embed_ok)

        self.assertFalse(write_failed)
        self.assertEqual(new_count, 1)
        self.assertEqual(len(collection.docs), 1, "同一篇不應寫入兩次")

    def test_16_rewrite_with_empty_content_does_not_delete_old_version(self):
        """改版文章若新內容為空，必須保留舊版，不得刪除。

        這是資料遺失路徑上的守衛：空內容 → chunk_text 回傳 []，
        若刪除發生在此之前，該篇就會被清空且下次也補不回來。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([
            {"url": "https://example.tw/e", "original_title": "有舊版的文章",
             "chunk_content": "舊內容", "chunk_index": 1, "total_chunks": 1,
             "embedding": [0.1], "updated_at": "2024/01/01"},
        ])
        article = {
            "source": "衛福部闢謠網站", "url": "https://example.tw/e",
            "title": "有舊版的文章", "content": "", "updated_at": "2024/09/09",
        }

        upload_to_mongodb([article], collection, embed_fn=fake_embed_ok)

        self.assertEqual(collection.deleted_filters, [], "不應刪除舊版")
        self.assertEqual(len(collection.docs), 1)
        self.assertEqual(collection.docs[0]["chunk_content"], "舊內容")

    def test_17_incomplete_legacy_article_is_repaired(self):
        """既有文章的實際切片數與宣告的 total_chunks 不符時，重寫修復。

        這是本 change 的第一個動機：舊版逐塊寫入在某塊向量化失敗時只印警告，
        其餘照常寫入，留下「宣告 4 塊、實際 3 塊」的破洞，且該篇之後會被判定
        「已存在」而永遠跳過。線上實測有 71 篇這樣的文章、遺失約 141 個切片。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([
            {"url": "https://example.tw/hole", "original_title": "破洞文章",
             "chunk_content": "第一塊", "chunk_index": 1, "total_chunks": 3,
             "embedding": [0.1]},
            {"url": "https://example.tw/hole", "original_title": "破洞文章",
             "chunk_content": "第三塊", "chunk_index": 3, "total_chunks": 3,
             "embedding": [0.1]},
        ])
        article = {
            "source": "衛福部闢謠網站", "url": "https://example.tw/hole",
            "title": "破洞文章", "content": "完整的新內容",
            "published_at": "2024/01/01", "updated_at": "2024/03/15",
        }

        new_count, write_failed = upload_to_mongodb(
            [article], collection, embed_fn=fake_embed_ok)

        self.assertFalse(write_failed)
        self.assertEqual(new_count, 1, "破洞文章應被重寫")
        self.assertEqual(len(collection.deleted_filters), 1,
                         "應刪除既有的不完整切片")
        self.assertTrue(
            all(d["chunk_content"] != "第一塊" for d in collection.docs),
            "舊的不完整切片不應留下")
        self.assertEqual(collection.docs[0]["total_chunks"], len(collection.docs),
                         "重寫後宣告值必須與實際切片數一致")
        self.assertEqual(collection.docs[0]["updated_at"], "2024/03/15")

    def test_18_complete_legacy_article_is_only_backfilled(self):
        """既有文章切片數與宣告值相符時，仍然只補日期、不重算向量。

        守住 Task 7 的成果：新增的破洞檢查不得誤觸發，
        否則又會回到「全量 2,840 個切片重算」的狀態。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([
            {"url": "https://example.tw/ok", "original_title": "完整文章",
             "chunk_content": "第一塊", "chunk_index": 1, "total_chunks": 2,
             "embedding": [0.1]},
            {"url": "https://example.tw/ok", "original_title": "完整文章",
             "chunk_content": "第二塊", "chunk_index": 2, "total_chunks": 2,
             "embedding": [0.1]},
        ])
        article = {
            "source": "衛福部闢謠網站", "url": "https://example.tw/ok",
            "title": "完整文章", "content": "新內容",
            "published_at": "2024/01/01", "updated_at": "2024/03/15",
        }

        calls = []

        def counting_embed(text):
            calls.append(text)
            return [0.5] * 3072

        upload_to_mongodb([article], collection, embed_fn=counting_embed)

        self.assertEqual(calls, [], "完整的既有文章不應重新向量化")
        self.assertEqual(collection.deleted_filters, [], "不應刪除任何切片")
        self.assertEqual(len(collection.docs), 2)
        self.assertTrue(all(d["updated_at"] == "2024/03/15" for d in collection.docs),
                        "所有切片都要補上日期")

if __name__ == '__main__':
    print("==================================================")
    print(" 🏥 ETL 資料管線 - 單元測試與一致性驗證啟動")
    print("==================================================\n")
    # verbosity=2 會印出每一條詳細的測試名稱，展示給教授看非常加分
    unittest.main(verbosity=2)