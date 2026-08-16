import re
import unittest
import requests
from bs4 import BeautifulSoup
from ca_bundle import get_ca_bundle
from utils import clean_html
from scraper_api import get_api_articles, is_admin_notice
from scraper_fda import get_fda_articles
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
        """套用 projection（僅支援 {"欄位": 1, ...} 這種 inclusion 形式），
        與 pymongo 慣例一致：未給 projection 時回傳完整文件，
        給了則只回傳被指名的欄位（`_id` 若存在則一併保留）。
        """
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                if projection is None:
                    return d
                result = {k: d[k] for k in projection if k != "_id" and k in d}
                if "_id" in d:
                    result["_id"] = d["_id"]
                return result
        return None

    def insert_many(self, docs):
        self.inserted_batches.append(list(docs))
        self.docs.extend(docs)

    def delete_many(self, query):
        """支援 {"url": ...} 與 {"original_title": ...} —— 與 main_pipeline
        實際用法一致（url 為 None 的來源以標題為刪除鍵）。"""
        self.deleted_filters.append(query)
        self.docs = [d for d in self.docs
                     if not all(d.get(k) == v for k, v in query.items())]

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
        """測試要求 2：確保 API 爬蟲資料與來源網站「一模一樣」

        2026-08-16 起這支只負責衛福部——食藥署已改由 scraper_fda 爬闢謠專區，
        見 test_02b。
        """
        # 1. 模擬人工：當下直接去衛福部 API 看最原始的第一筆資料
        raw_url = "https://www.hpa.gov.tw/wf/newsapi.ashx"
        res = requests.get(raw_url, verify=get_ca_bundle(), timeout=10)
        raw_data = res.json()
        items = raw_data if isinstance(raw_data, list) else raw_data.get("data", [])
        first = items[0]
        expected_title = first.get("標題", first.get("Title", first.get("title", ""))).strip()

        # 2. 呼叫我們的模組
        articles = get_api_articles(test_mode=True)

        # 3. 找出模組抓到的「衛福部」第一篇文章
        hpa_first_article = next(art for art in articles if art["source"] == "衛福部闢謠網站")

        # 4. 斷言比對 (Data Integrity Check)
        print(f"\n    🔍 [API 來源原始資料] 最新標題: {expected_title}")
        print(f"    ✅ [API 爬蟲模組產出] 最終標題: {hpa_first_article['title']}")

        self.assertEqual(hpa_first_article["title"], expected_title,
                         "API 爬蟲抓取的標題與來源 API 原始資料不一致！")

        # 確保格式欄位齊全
        self.assertIn("content", hpa_first_article)
        self.assertIn("url", hpa_first_article)

    def test_02a_admin_notice_filter(self):
        """行政公告判準：擋掉法規預告與典禮，但不擋衛教與澄清稿。

        關鍵字是照線上 706 篇標題校準的，這支測試釘住兩端——漏擋會讓雜訊
        回到知識庫，誤擋會刪掉真正的衛教內容（kb-003 / kb-024 就依賴其中幾篇）。
        """
        should_block = [
            "101年度優良廚師表揚活動",
            "預告修正「食品業者登錄辦法」第四條，擬納入物流業及倉儲資料",
            "食藥署舉辦「2025 亞太國際化粧品檢驗技術研討會」",
            "103年度餐飲業HACCP衛生評鑑綜合座談會暨成果表揚大會",
            "食藥署公布113年2月21日豬肉檢出西布特羅案專家會議紀錄",
        ]
        should_keep = [
            "守護兒童用藥安全 使用抗生素四不一要",
            "夏雨綿綿，家中藥品處理報你哉",
            "讓食藥署教您如何保存與處理家中藥品",
            "新年到!藥品正確儲存及丟棄三撇步",
            "安心過好年：春節用藥安全小叮嚀",
            # 「活動」刻意不是關鍵字——這篇是澄清稿，不是行政公告
            "食藥署澄清107年並未邀請蕾菈參加本署反毒活動",
        ]
        for title in should_block:
            with self.subTest(block=title):
                self.assertTrue(is_admin_notice(title), f"應擋下卻放行：{title}")
        for title in should_keep:
            with self.subTest(keep=title):
                self.assertFalse(is_admin_notice(title), f"應保留卻擋下：{title}")

    def test_02b_fda_data_integrity(self):
        """食藥署闢謠專區爬蟲與來源網頁一致，且每篇都帶得回網址。

        `url` 這個斷言是這支測試存在的主因：舊的 DataAction 端點結構上就
        給不出文章網址，導致全庫 28.5% 的 chunk 無法在答案中附上可查證的
        連結，而這件事一路沒被任何測試擋下來。
        """
        # 1. 模擬人工：直接去闢謠專區列表頁抓第一篇的 id 與標題
        raw_url = "https://www.fda.gov.tw/TC/news.aspx?cid=5049&pn=1"
        res = requests.get(raw_url, headers={"User-Agent": "Mozilla/5.0"},
                           verify=get_ca_bundle(), timeout=15)
        soup = BeautifulSoup(res.content, "html.parser")
        first_link = next(a for a in soup.find_all("a")
                          if "newsContent.aspx?cid=5049&id=" in (a.get("href") or ""))
        expected_title = first_link.get_text(strip=True)
        expected_id = re.search(r"id=(\d+)", first_link["href"]).group(1)

        # 2. 呼叫我們的模組
        articles = get_fda_articles(test_mode=True)
        self.assertTrue(articles, "食藥署闢謠專區一篇都沒抓到")
        first = articles[0]

        print(f"\n    🔍 [闢謠專區列表頁] 最新標題: {expected_title}")
        print(f"    ✅ [FDA 爬蟲模組產出] 最終標題: {first['title']}")

        self.assertEqual(first["title"], expected_title,
                         "FDA 爬蟲抓取的標題與來源網頁不一致！")

        # 3. 每篇都必須有可點的文章網址——這正是換掉舊來源的理由
        for art in articles:
            self.assertEqual(art["source"], "食藥署闢謠專區")
            self.assertTrue(art["url"], f"《{art['title']}》沒有 url")
            self.assertIn("newsContent.aspx?cid=5049&id=", art["url"])
            self.assertTrue(art["content"].strip(), f"《{art['title']}》內容是空的")
        self.assertIn(expected_id, first["url"])

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
        
        # 比對前先把連續空白收斂：列表頁 anchor 與詳細頁 <title> 對同一個標題
        # 的空白處理不一致（站方 WordPress 產出的差異），逐字元比對會因此假性
        # 失敗。這裡要驗的是「沒有漏字、沒有切錯」，不是空白的位元組相同。
        def _norm(text):
            return re.sub(r"\s+", " ", text).strip()

        self.assertEqual(_norm(tfc_first_article["title"]), _norm(expected_title),
                         "TFC 爬蟲抓取的標題與網頁當前顯示的第一篇標題不一致！")

        # 確保格式欄位齊全
        self.assertIn("content", tfc_first_article)
        self.assertTrue(len(tfc_first_article["content"]) > 50, "內文長度過短，可能抓取失敗")

    def test_03a_tfc_carries_verdict_claim_and_dates(self):
        """TFC 是唯一本來就在做查核的來源，判定標籤必須跟著資料一起回來。

        這三個欄位是後續判定功能的地基：verdict 由專業查核組織標註，省下自行
        標註與 LLM 猜測；沒有它們，TFC 與其他三個來源就只是「又一批健康文章」。
        """
        articles = get_tfc_articles(test_mode=True)
        self.assertTrue(articles, "TFC 一篇都沒抓到")

        allowed = {"錯誤", "部分錯誤", "正確", "事實釐清", "證據不足"}
        for art in articles:
            with self.subTest(title=art["title"][:20]):
                self.assertEqual(art["source"], "台灣事實查核中心")
                self.assertTrue(art["url"].startswith(
                    "https://tfc-taiwan.org.tw/fact-check-reports/"))
                # 判定：slug 與中文名必須成對，且落在 TFC 官方五分類內
                self.assertIsNotNone(art["verdict_slug"], "取不到判定 slug")
                self.assertIn(art["verdict"], allowed)
                # 日期：舊版寫死 None，改版偵測因此完全失效
                self.assertRegex(art["published_at"] or "", r"^\d{4}-\d{2}-\d{2}$")
                self.assertRegex(art["updated_at"] or "", r"^\d{4}-\d{2}-\d{2}$")

    def test_03b_tfc_content_excludes_boilerplate(self):
        """內文只取「背景」「查核」兩節，不得混入頁尾與募款區塊。

        舊版把所有長度 >20 的 <p> 串起來，導航、關於我們、支持事實查核那些固定
        文案會一起進向量庫，在檢索時與真正的查核內容競爭。
        """
        articles = get_tfc_articles(test_mode=True)
        for art in articles:
            with self.subTest(title=art["title"][:20]):
                for boilerplate in ("關於我們", "支持事實查核", "訂閱電子報"):
                    self.assertNotIn(boilerplate, art["content"],
                                     f"內文混入頁尾文案「{boilerplate}」")

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

        # 上面的比對只證明「檔案裡有什麼就併進去了」，不會發現檔案本身被換掉或
        # 損毀。這裡把 PEM 解碼成 DER 後比對 SHA-256，才擋得住位元層級的竄改。
        import hashlib
        import ssl

        der = ssl.PEM_cert_to_DER_cert(pinned)
        self.assertEqual(
            hashlib.sha256(der).hexdigest().upper(),
            "1A2C75FD096E0499E9FF6AC74E526F61EAAE3EDFC8C2EA4436FEE0C24D8B7D0E",
            "釘選的憑證與 certs/README.md 記錄的 SHA-256 不符——"
            "檔案可能被替換或損毀")

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

    def test_13a_legacy_backfill_also_fills_verdict_and_claim(self):
        """補中繼資料時 verdict／claim 也要補上。

        這條路徑只在「文章已存在」時執行，之後不會再有機會回頭寫：漏掉的話，
        線上既有的 TFC 文章會永遠沒有判定標籤，而那正是查核型來源的價值所在。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([
            {"url": "https://tfc-taiwan.org.tw/fact-check-reports/x",
             "original_title": "舊查核報告", "chunk_content": "舊內容",
             "chunk_index": 1, "total_chunks": 1, "embedding": [0.1]},
        ])
        article = {
            "source": "台灣事實查核中心",
            "url": "https://tfc-taiwan.org.tw/fact-check-reports/x",
            "title": "舊查核報告", "content": "新內容",
            "published_at": "2026-07-24", "updated_at": "2026-07-25",
            "verdict": "錯誤", "verdict_slug": "incorrect",
            "claim": "網傳「吃X可以治癌」？",
        }

        def counting_embed(text):
            raise AssertionError("既有資料不應重新向量化")

        upload_to_mongodb([article], collection, embed_fn=counting_embed)

        doc = collection.docs[0]
        self.assertEqual(doc["verdict"], "錯誤")
        self.assertEqual(doc["verdict_slug"], "incorrect")
        self.assertEqual(doc["claim"], "網傳「吃X可以治癌」？")
        self.assertEqual(doc["chunk_content"], "舊內容", "內容不應被更動")

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

    def test_19_repair_with_embedding_failure_does_not_delete_old_version(self):
        """破洞修復重寫時若向量化失敗，不得刪除舊版本（避免資料遺失）。

        needs_rewrite 不論由哪個分支設定——test_10 驗證的是「改版偵測」
        觸發的路徑，這裡驗證的是「破洞判斷」觸發的路徑——下游都必須走
        同一套「先向量化成功、才刪除舊版」保證。寫成測試而不是只靠推論。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection(existing=[
            {"url": "https://example.com/hole-fail", "original_title": "破洞文章",
             "chunk_index": 1, "total_chunks": 3},
        ])
        article = {
            "title": "破洞文章", "content": "第一句。" * 200, "source": "衛福部闢謠網站",
            "url": "https://example.com/hole-fail",
            "published_at": "2025-12-01", "updated_at": "2026-08-01",
        }

        upload_to_mongodb([article], collection, embed_fn=make_failing_embed(2))

        self.assertEqual(collection.deleted_filters, [],
                         "向量化失敗時不得刪除舊版本")
        self.assertEqual(collection.inserted_batches, [], "也不應寫入新版本")
        self.assertTrue(any(d.get("url") == "https://example.com/hole-fail"
                            for d in collection.docs),
                        "舊版本必須原封不動留在庫中")

    def test_20_partial_insert_is_rolled_back(self):
        """insert_many 寫到一半失敗時，殘留的切片必須被清除。

        pymongo 的 insert_many 預設 ordered=True：伺服器逐筆寫入，中途出錯
        只中止「剩下的」，已經寫進去的不會回滾。若放著不管，這一篇就會變成
        「宣告 N 塊、實際少於 N 塊」——正是本次變更要消滅的破洞形態。
        """
        from main_pipeline import upload_to_mongodb

        class PrefixInsertCollection(FakeCollection):
            """模擬真實 pymongo：寫入前兩筆之後才拋錯。"""

            def insert_many(self, docs):
                docs = list(docs)
                self.inserted_batches.append(docs)
                self.docs.extend(docs[:2])
                raise RuntimeError("模擬 Atlas 中途連線中斷")

        collection = PrefixInsertCollection([])
        article = {
            "source": "衛福部闢謠網站", "url": "https://example.tw/partial",
            "title": "會寫到一半的文章", "content": "內容" * 600,
            "updated_at": None,
        }

        new_count, write_failed = upload_to_mongodb(
            [article], collection, embed_fn=fake_embed_ok)

        self.assertTrue(write_failed)
        self.assertEqual(new_count, 0)
        self.assertEqual(
            [d for d in collection.docs
             if d.get("url") == "https://example.tw/partial"],
            [],
            "殘留的切片必須被清除，否則會留下宣告與實際不符的破洞")

    def test_21_hole_is_repaired_even_after_updated_at_is_set(self):
        """完整性檢查每次執行都跑，不只在尚未補日期的文章上。

        若只在 `updated_at is None` 時檢查，這道防線在首次執行之後就變成
        死程式碼；而寫入中途失敗留下的破洞會帶著 updated_at，
        於是永遠被判定「已存在」而跳過。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([
            {"url": "https://example.tw/later", "original_title": "後來破的文章",
             "chunk_content": "第一塊", "chunk_index": 1, "total_chunks": 4,
             "embedding": [0.1], "updated_at": "2024/06/01"},
        ])
        article = {
            "source": "衛福部闢謠網站", "url": "https://example.tw/later",
            "title": "後來破的文章", "content": "重新取得的完整內容",
            "updated_at": "2024/06/01",
        }

        new_count, write_failed = upload_to_mongodb(
            [article], collection, embed_fn=fake_embed_ok)

        self.assertFalse(write_failed)
        self.assertEqual(new_count, 1, "日期相同但切片數不符時仍須修復")
        self.assertEqual(len(collection.deleted_filters), 1)
        self.assertEqual(collection.docs[0]["total_chunks"], len(collection.docs))

    def test_22_systematic_embedding_failure_is_reported(self):
        """有嘗試但一篇都沒成功時必須回報失敗（配額用盡的情境）。

        爬蟲成功、三個來源都有文章，所以來源檢查看不到這個問題。
        沒有這道判斷，知識庫可以連續數週停止更新而 CI 一路綠燈。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([])
        articles = [
            {"source": "衛福部闢謠網站", "url": f"https://example.tw/{i}",
             "title": f"文章{i}", "content": "內容", "updated_at": None}
            for i in range(3)
        ]

        new_count, write_failed = upload_to_mongodb(
            articles, collection, embed_fn=lambda text: [])

        self.assertEqual(new_count, 0)
        self.assertTrue(write_failed,
                        "全數向量化失敗必須讓 job() 以非零狀態碼結束")

    def test_23_single_embedding_failure_does_not_fail_the_run(self):
        """單篇偶發向量化失敗不應讓整次執行紅燈。

        每天為了一篇失敗而紅燈，只會訓練維護者忽略 CI。
        那一篇下次執行就會補上。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([])
        articles = [
            {"source": "衛福部闢謠網站", "url": "https://example.tw/a",
             "title": "會失敗的文章", "content": "內容", "updated_at": None},
            {"source": "衛福部闢謠網站", "url": "https://example.tw/b",
             "title": "會成功的文章", "content": "內容", "updated_at": None},
        ]

        new_count, write_failed = upload_to_mongodb(
            articles, collection, embed_fn=make_failing_embed(1))

        self.assertEqual(new_count, 1)
        self.assertFalse(write_failed, "只要有文章成功寫入就不算系統性失敗")

    def test_24_empty_title_article_is_written_at_most_once(self):
        """標題為空字串的文章不得每天重複寫入。

        食藥署的 706 篇文章 url 全為 None，標題是唯一的去重鍵。
        若把空字串當成「沒有標題」濾掉，這種文章每次執行都會被當成新文章，
        知識庫會無上限地累積重複切片，直接汙染下游 RAG 的檢索結果。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([])
        article = {
            "source": "食藥署闢謠專區", "url": None,
            "title": "", "content": "有內容但沒有標題", "updated_at": None,
        }

        upload_to_mongodb([article], collection, embed_fn=fake_embed_ok)
        first_run = len(collection.docs)
        upload_to_mongodb([dict(article)], collection, embed_fn=fake_embed_ok)

        self.assertEqual(len(collection.docs), first_run,
                         "第二次執行不應再寫入一次")

    # ------------------------------------------------------------------
    # 寫入文件的內容
    # ------------------------------------------------------------------

    def test_25_written_document_has_correct_shape(self):
        """寫入的每一個欄位都要對。

        在此之前沒有任何測試斷言過文件的「內容」——只斷言了筆數與是否寫入。
        突變測試證實：把 chunk_content 換成標題、embedding 換成空 list、
        source_name 換成 None、chunk_index 改為 0 起算，整套測試都照樣全綠。
        這些欄位全部是下游 CARE Backend 直接讀取的。
        """
        from main_pipeline import chunk_text, upload_to_mongodb

        collection = FakeCollection([])
        content = "衛教內容" * 200          # 800 字，確定會切成多塊
        article = {
            "source": "衛福部闢謠網站", "url": "https://example.tw/shape",
            "title": "欄位形狀測試", "content": content,
            "published_at": "2024/01/01", "updated_at": "2024/03/15",
        }

        upload_to_mongodb([article], collection, embed_fn=fake_embed_ok)

        expected_chunks = chunk_text(content)
        self.assertGreater(len(expected_chunks), 1, "測資本身要能切出多塊才有意義")
        self.assertEqual(len(collection.docs), len(expected_chunks))

        for i, (doc, expected_chunk) in enumerate(
                zip(collection.docs, expected_chunks)):
            self.assertEqual(doc["source_name"], "衛福部闢謠網站")
            self.assertEqual(doc["url"], "https://example.tw/shape")
            self.assertEqual(doc["original_title"], "欄位形狀測試")
            self.assertEqual(doc["chunk_content"], expected_chunk,
                             "chunk_content 必須是切片本身，不是標題或其他東西")
            self.assertEqual(doc["chunk_index"], i + 1,
                             "chunk_index 由 1 起算")
            self.assertEqual(doc["total_chunks"], len(expected_chunks))
            self.assertEqual(doc["embedding"], fake_embed_ok(""),
                             "embedding 必須是向量化的結果")
            self.assertEqual(doc["published_at"], "2024/01/01")
            self.assertEqual(doc["updated_at"], "2024/03/15")
            self.assertIsInstance(doc["uploaded_at"], float)

    # ------------------------------------------------------------------
    # 同一批次內的去重
    # ------------------------------------------------------------------

    def test_26_same_url_different_titles_in_one_batch(self):
        """同一批次內 url 相同、標題不同的兩篇只寫入一次。

        來源改標題或翻頁重疊都會產生這種情形。標題不同，所以標題去重救不了，
        必須靠批次內的 url 記錄。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([])
        base = {"source": "衛福部闢謠網站", "url": "https://example.tw/same",
                "content": "內容", "updated_at": None}
        articles = [dict(base, title="標題甲"), dict(base, title="標題乙")]

        new_count, _ = upload_to_mongodb(
            articles, collection, embed_fn=fake_embed_ok)

        self.assertEqual(new_count, 1, "同一個 url 在一個批次內只能寫入一次")
        self.assertEqual(len(collection.docs), 1)

    def test_27_url_none_same_title_in_one_batch(self):
        """同一批次內 url 皆為 None、標題相同的兩篇只寫入一次。

        食藥署 706 篇文章的 url 全為 None，標題是唯一的去重鍵；
        少了批次內的標題記錄，同一批次的重複會直接變成重複切片。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([])
        base = {"source": "食藥署闢謠專區", "url": None,
                "title": "同一篇文章", "content": "內容", "updated_at": None}

        new_count, _ = upload_to_mongodb(
            [dict(base), dict(base)], collection, embed_fn=fake_embed_ok)

        self.assertEqual(new_count, 1, "url 為 None 時標題就是去重鍵")
        self.assertEqual(len(collection.docs), 1)

    def test_28_existing_url_with_changed_title_is_skipped(self):
        """庫中已有這個 url 時就跳過，即使來源這次給的標題不同。

        驗證的是「url 集合真的有從資料庫載入」——標題不同，
        所以標題去重不會誤打誤撞地讓這個測試通過。
        """
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([
            {"url": "https://example.tw/renamed", "original_title": "舊標題",
             "chunk_content": "舊內容", "chunk_index": 1, "total_chunks": 1,
             "embedding": [0.1]},
        ])
        article = {
            "source": "衛福部闢謠網站", "url": "https://example.tw/renamed",
            "title": "改過的新標題", "content": "內容", "updated_at": None,
        }

        new_count, _ = upload_to_mongodb(
            [article], collection, embed_fn=fake_embed_ok)

        self.assertEqual(new_count, 0, "url 已存在就該跳過")
        self.assertEqual(len(collection.docs), 1)

    # ------------------------------------------------------------------
    # job() 的退出碼
    # ------------------------------------------------------------------

    def _all_source_articles(self):
        """每個 EXPECTED_SOURCES 各一篇。新增來源時這裡必須跟著加，
        否則 find_missing_sources 會判定該來源全滅而讓退出碼變 1。"""
        return [
            {"source": "食藥署闢謠專區",
             "url": "https://www.fda.gov.tw/TC/newsContent.aspx?cid=5049&id=1",
             "title": "闢謠專區文章", "content": "內容", "updated_at": None},
            {"source": "食藥署公告", "url": None, "title": "食藥署公告文章",
             "content": "內容", "updated_at": None},
            {"source": "衛福部闢謠網站", "url": "https://example.tw/hpa",
             "title": "衛福部文章", "content": "內容", "updated_at": None},
            {"source": "台灣事實查核中心", "url": "https://example.tw/tfc",
             "title": "查核中心文章", "content": "內容", "updated_at": None},
        ]

    def test_29_job_returns_zero_when_everything_succeeds(self):
        """每個來源都有產出且寫入成功時，退出碼為 0。

        這是防止「修過頭」的守衛：若有人讓 job() 永遠回傳 1，CI 會天天紅燈，
        維護者很快就會忽略它。
        """
        from main_pipeline import job

        collection = FakeCollection([])
        articles = self._all_source_articles()

        rc = job(fetchers=(lambda: articles,),
                 collection_factory=lambda: collection,
                 embed_fn=fake_embed_ok)

        self.assertEqual(rc, 0)
        self.assertEqual(len(collection.docs), len(articles))

    def test_30_job_returns_one_on_missing_source_but_still_writes_others(self):
        """來源全滅時退出碼為 1，但其餘來源的文章仍照常寫入。

        兩件事要同時成立：資料面 fail-open（不因一個來源失效就整批中止）、
        訊號面 fail-loud（CI 必須紅燈）。少了任何一半都是回歸。
        """
        from main_pipeline import job

        collection = FakeCollection([])
        articles = [a for a in self._all_source_articles()
                    if a["source"] != "台灣事實查核中心"]

        rc = job(fetchers=(lambda: articles,),
                 collection_factory=lambda: collection,
                 embed_fn=fake_embed_ok)

        self.assertEqual(rc, 1, "有來源一篇都沒抓到必須讓 CI 紅燈")
        self.assertEqual(len(collection.docs), len(articles),
                         "其餘來源的文章仍須照常寫入——不得提早 return")

    def test_31_job_returns_one_when_collection_is_unreachable(self):
        """連不上 MongoDB 時退出碼為 1。"""
        from main_pipeline import job

        def unreachable():
            raise RuntimeError("模擬 Atlas 連線失敗")

        rc = job(fetchers=(lambda: self._all_source_articles(),),
                 collection_factory=unreachable,
                 embed_fn=fake_embed_ok)

        self.assertEqual(rc, 1)

    def test_32_job_returns_one_when_a_write_fails(self):
        """upload_to_mongodb 回報寫入失敗時，job() 必須據以回傳 1。"""
        from main_pipeline import job

        class FailingInsert(FakeCollection):
            def insert_many(self, docs):
                raise RuntimeError("模擬 Atlas 寫入失敗")

        collection = FailingInsert([])

        rc = job(fetchers=(lambda: self._all_source_articles(),),
                 collection_factory=lambda: collection,
                 embed_fn=fake_embed_ok)

        self.assertEqual(rc, 1, "job() 不得忽略 upload_to_mongodb 回報的失敗")

    def test_33_ci_mode_propagates_the_exit_code(self):
        """GITHUB_ACTIONS 模式必須把 job() 的退出碼交回給作業系統。

        整條「失敗必須可見」的鏈條，最後一環就是這裡：job() 算出 1，
        但如果沒有一路傳到 sys.exit()，Actions 依然是綠燈。
        常駐模式的無限迴圈無法在測試中執行，所以只測 CI 這一支。
        """
        from main_pipeline import main

        self.assertEqual(
            main(env={"GITHUB_ACTIONS": "true"}, job_fn=lambda: 1), 1,
            "job() 回傳 1 時 CI 模式必須也回傳 1")
        self.assertEqual(
            main(env={"GITHUB_ACTIONS": "true"}, job_fn=lambda: 0), 0,
            "成功時不得誤報失敗")


if __name__ == '__main__':
    print("==================================================")
    print(" 🏥 ETL 資料管線 - 單元測試與一致性驗證啟動")
    print("==================================================\n")
    # verbosity=2 會印出每一條詳細的測試名稱，展示給教授看非常加分
    unittest.main(verbosity=2)