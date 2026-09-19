import re
import unittest
import requests
from bs4 import BeautifulSoup
from ca_bundle import get_ca_bundle
from utils import clean_html
from main_pipeline import CHUNKER_VERSION as _CURRENT_CHUNKER
from scraper_api import get_api_articles, is_admin_notice
from scraper_fda import get_fda_articles
from scraper_tfc import get_tfc_articles
import scraper_mohw
import scraper_media
import main_pipeline


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
        hpa_first_article = next(art for art in articles if art["source"] == "國健署新聞")

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
             "updated_at": "2026-08-01", "chunker_version": _CURRENT_CHUNKER},
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

        without_hpa = [a for a in full if a["source"] != "國健署新聞"]
        self.assertEqual(find_missing_sources(without_hpa), {"國健署新聞"},
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
             "embedding": [0.1], "chunker_version": _CURRENT_CHUNKER},
        ])
        article = {
            "source": "國健署新聞", "url": "https://example.tw/a",
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
             "chunk_index": 1, "total_chunks": 1, "embedding": [0.1],
             "chunker_version": _CURRENT_CHUNKER},
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
            {"source": "國健署新聞", "url": "https://example.tw/bad",
             "title": "會失敗的文章", "content": "內容", "updated_at": None},
            {"source": "國健署新聞", "url": "https://example.tw/ok",
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
             "embedding": [0.1], "updated_at": "2024/01/01",
             "chunker_version": _CURRENT_CHUNKER},
        ])
        article = {
            "source": "國健署新聞", "url": "https://example.tw/e",
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
            "source": "國健署新聞", "url": "https://example.tw/hole",
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
             "embedding": [0.1], "chunker_version": _CURRENT_CHUNKER},
            {"url": "https://example.tw/ok", "original_title": "完整文章",
             "chunk_content": "第二塊", "chunk_index": 2, "total_chunks": 2,
             "embedding": [0.1], "chunker_version": _CURRENT_CHUNKER},
        ])
        article = {
            "source": "國健署新聞", "url": "https://example.tw/ok",
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
            "title": "破洞文章", "content": "第一句。" * 200, "source": "國健署新聞",
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
            "source": "國健署新聞", "url": "https://example.tw/partial",
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
             "embedding": [0.1], "updated_at": "2024/06/01",
             "chunker_version": _CURRENT_CHUNKER},
        ])
        article = {
            "source": "國健署新聞", "url": "https://example.tw/later",
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
            {"source": "國健署新聞", "url": f"https://example.tw/{i}",
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
            {"source": "國健署新聞", "url": "https://example.tw/a",
             "title": "會失敗的文章", "content": "內容", "updated_at": None},
            {"source": "國健署新聞", "url": "https://example.tw/b",
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
            "source": "國健署新聞", "url": "https://example.tw/shape",
            "title": "欄位形狀測試", "content": content,
            "published_at": "2024/01/01", "updated_at": "2024/03/15",
        }

        upload_to_mongodb([article], collection, embed_fn=fake_embed_ok)

        expected_chunks = chunk_text(content)
        self.assertGreater(len(expected_chunks), 1, "測資本身要能切出多塊才有意義")
        self.assertEqual(len(collection.docs), len(expected_chunks))

        for i, (doc, expected_chunk) in enumerate(
                zip(collection.docs, expected_chunks)):
            self.assertEqual(doc["source_name"], "國健署新聞")
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
        base = {"source": "國健署新聞", "url": "https://example.tw/same",
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
             "embedding": [0.1], "chunker_version": _CURRENT_CHUNKER},
        ])
        article = {
            "source": "國健署新聞", "url": "https://example.tw/renamed",
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
            {"source": "國健署新聞", "url": "https://example.tw/hpa",
             "title": "國健署新聞文章", "content": "內容", "updated_at": None},
            {"source": "台灣事實查核中心", "url": "https://example.tw/tfc",
             "title": "查核中心文章", "content": "內容", "updated_at": None},
            {"source": "衛福部真相說明", "url": "https://example.tw/mohw",
             "title": "真相說明文章", "content": "內容", "updated_at": None},
            {"source": "國健署真相與闢謠", "url": "https://example.tw/hpa-truth",
             "title": "真相與闢謠文章", "content": "內容", "updated_at": None},
            # Cofacts 帶著自己的判定進來（其他來源的 verdict 由 claim_tagger
            # 事後補，它是唯一在爬蟲階段就有判定的非 TFC 來源）。
            {"source": "Cofacts 真的假的", "url": "https://cofacts.tw/article/x",
             "title": "協作查核文章", "content": "內容", "updated_at": None,
             "verdict": "錯誤", "verdict_slug": "incorrect", "claim": "吃鳳梨心可以治痛風"},
            # 「疾管署闢謠專區」刻意不在這裡：它不在 EXPECTED_SOURCES 內，
            # 加進來不會影響退出碼，但會讓這份 fixture 與那份集合失去對應。
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

        ok = lambda: 0  # noqa: E731 —— 媒體 job 的替身，這支只測官方那條
        self.assertEqual(
            main(env={"GITHUB_ACTIONS": "true"}, job_fn=lambda: 1, media_job_fn=ok), 1,
            "job() 回傳 1 時 CI 模式必須也回傳 1")
        self.assertEqual(
            main(env={"GITHUB_ACTIONS": "true"}, job_fn=lambda: 0, media_job_fn=ok), 0,
            "成功時不得誤報失敗")

    def test_33a_media_failure_turns_ci_red_but_official_still_runs(self):
        """媒體失敗要讓 Actions 紅燈，但官方 ETL 仍照常執行（反之亦然）。

        兩支都要跑完才決定退出碼。若任一支失敗就提早結束，一個來源的暫時
        問題會讓另一個來源當天沒更新——那正是 job() 刻意避免的事。
        """
        from main_pipeline import main
        calls = []

        def official():
            calls.append("official"); return 0

        def media():
            calls.append("media"); return 1

        self.assertEqual(main(env={"GITHUB_ACTIONS": "true"},
                              job_fn=official, media_job_fn=media), 1)
        self.assertEqual(calls, ["media", "official"], "媒體失敗時官方仍必須執行")

        calls.clear()
        self.assertEqual(main(env={"GITHUB_ACTIONS": "true"},
                              job_fn=lambda: (calls.append("official"), 1)[1],
                              media_job_fn=lambda: (calls.append("media"), 0)[1]), 1)
        self.assertEqual(calls, ["media", "official"])

    def test_33b_media_runs_before_official(self):
        """媒體只要十幾秒、官方約一小時；媒體排後面就要等官方跑完才寫得進去。"""
        from main_pipeline import main
        calls = []
        main(env={"GITHUB_ACTIONS": "true"},
             job_fn=lambda: (calls.append("official"), 0)[1],
             media_job_fn=lambda: (calls.append("media"), 0)[1])
        self.assertEqual(calls[0], "media")


if __name__ == '__main__':
    print("==================================================")
    print(" 🏥 ETL 資料管線 - 單元測試與一致性驗證啟動")
    print("==================================================\n")
    # verbosity=2 會印出每一條詳細的測試名稱，展示給教授看非常加分
    unittest.main(verbosity=2)

class TestTFCLegacyVerdict(unittest.TestCase):
    """舊站遷移文章的判定來自標題前綴，不是分類連結。"""

    def _soup(self, html):
        return BeautifulSoup(html, "html.parser")

    def test_classification_link_wins_over_title_prefix(self):
        """兩者都在時以分類連結為準——那是站方的機器可讀識別碼。"""
        from scraper_tfc import _extract_verdict
        html = ('<a href="/fact-check-report-classification/partially-incorrect/">'
                '部分錯誤</a>')
        slug, verdict = _extract_verdict(self._soup(html), "【錯誤】網傳「X」？")
        self.assertEqual(slug, "partially-incorrect")
        self.assertEqual(verdict, "部分錯誤")

    def test_title_prefix_used_when_no_classification_link(self):
        """舊站文章（/migration-11252）沒有分類連結，判定在標題前綴。"""
        from scraper_tfc import _extract_verdict
        slug, verdict = _extract_verdict(self._soup("<p>沒有分類連結</p>"),
                                         "【錯誤】網傳「X」？")
        self.assertEqual(verdict, "錯誤")
        self.assertTrue(slug.startswith("legacy:"),
                        "來自標題前綴的判定要標示來源，讓下游分得出差異")

    def test_legacy_only_labels_map_to_official_five(self):
        """舊站用過但現行分類表沒有的標籤，依 TFC 官方定義歸併。"""
        from scraper_tfc import _extract_verdict
        cases = {
            "假借冠名": "錯誤",      # 官方「錯誤」的定義明列「假借冠名的言論」
            "詐騙": "錯誤",
            "易生誤解": "部分錯誤",  # 官方「部分錯誤」涵蓋「片面事實、脈絡有誤」
        }
        for prefix, expected in cases.items():
            with self.subTest(prefix=prefix):
                _, verdict = _extract_verdict(self._soup("<p/>"), f"【{prefix}】網傳「X」？")
                self.assertEqual(verdict, expected)

    def test_unknown_prefix_yields_no_verdict(self):
        """認不得的前綴回 None，不要硬猜——寧可沒有判定也不要給錯的。"""
        from scraper_tfc import _extract_verdict
        slug, verdict = _extract_verdict(self._soup("<p/>"), "【某個新標籤】網傳「X」？")
        self.assertIsNone(verdict)
        self.assertIsNone(slug)


class TestDailyQuotaExhaustion(unittest.TestCase):
    """每日額度用盡與每分鐘超速是兩回事，處置必須不同。

    2026-08-17 那次執行因為把兩者當同一件事，撞到額度上限後仍每 40 秒重試，
    空轉 4 小時、觸發 357 次、沒寫進任何一筆，最後被 GitHub Actions 的
    6 小時上限砍掉。
    """

    def test_quota_exhaustion_stops_remaining_articles(self):
        """額度用盡就停止整批——今天剩下的每一次呼叫都注定失敗。"""
        from main_pipeline import upload_to_mongodb, DailyQuotaExhausted

        collection = FakeCollection([])
        articles = [
            {"source": "測試", "url": f"https://example.tw/{i}", "title": f"文章{i}",
             "content": "內容。" * 50, "updated_at": None}
            for i in range(5)
        ]

        calls = {"n": 0}

        def embed_until_quota(text):
            calls["n"] += 1
            if calls["n"] > 3:
                raise DailyQuotaExhausted("quota")
            return [0.1] * 3072

        upload_to_mongodb(articles, collection, embed_fn=embed_until_quota)

        # 額度用盡後不應再為後續文章呼叫 API
        self.assertLessEqual(calls["n"], 4,
                             "額度用盡後仍繼續呼叫，等同 2026-08-17 的空轉")

    def test_progress_is_kept_when_quota_runs_out_midway(self):
        """中途額度用盡：已完成的照常寫入，未完成的整篇不留。"""
        from main_pipeline import upload_to_mongodb, DailyQuotaExhausted

        collection = FakeCollection([])
        articles = [
            {"source": "測試", "url": "https://example.tw/a", "title": "第一篇",
             "content": "短內容。", "updated_at": None},
            {"source": "測試", "url": "https://example.tw/b", "title": "第二篇",
             "content": "短內容。", "updated_at": None},
        ]

        calls = {"n": 0}

        def embed_second_fails(text):
            calls["n"] += 1
            if calls["n"] > 1:
                raise DailyQuotaExhausted("quota")
            return [0.1] * 3072

        new_count, _ = upload_to_mongodb(articles, collection,
                                         embed_fn=embed_second_fails)

        self.assertEqual(new_count, 1, "第一篇已完成，應照常寫入")
        urls = {d["url"] for d in collection.docs}
        self.assertEqual(urls, {"https://example.tw/a"},
                         "第二篇未完成，不得留下半截切片")

    def test_transient_rate_limit_still_retries(self):
        """每分鐘超速仍要重試——不能因為修了額度就把這條路也關掉。"""
        import main_pipeline

        calls = {"n": 0}
        posted = []

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            posted.append(url)
            if calls["n"] == 1:
                return FakeResp({"error": {"message": "Quota exceeded ... 429"}})
            return FakeResp({"embedding": {"values": [0.5] * 3072}})

        orig_post, orig_sleep = main_pipeline.requests.post, main_pipeline.time.sleep
        main_pipeline.requests.post = fake_post
        main_pipeline.time.sleep = lambda s: None
        try:
            vector = main_pipeline.get_embedding("測試")
        finally:
            main_pipeline.requests.post = orig_post
            main_pipeline.time.sleep = orig_sleep

        self.assertEqual(len(vector), 3072, "第二次就成功了，不該放棄")
        self.assertEqual(calls["n"], 2)

    def test_persistent_quota_error_raises(self):
        """連續重試都撞同一面牆 → 判定為額度用盡，拋例外而非無限重試。"""
        import main_pipeline
        from main_pipeline import DailyQuotaExhausted

        class FakeResp:
            def json(self):
                return {"error": {"message": "Quota exceeded for metric: "
                                             "embed_content_free_tier_requests, limit: 1000"}}

        orig_post, orig_sleep = main_pipeline.requests.post, main_pipeline.time.sleep
        main_pipeline.requests.post = lambda *a, **k: FakeResp()
        main_pipeline.time.sleep = lambda s: None
        try:
            with self.assertRaises(DailyQuotaExhausted):
                main_pipeline.get_embedding("測試")
        finally:
            main_pipeline.requests.post = orig_post
            main_pipeline.time.sleep = orig_sleep


class TestTFCClaimExtraction(unittest.TestCase):
    """舊站文章的主張在標題裡，不在內文。

    那些頁面的內文只有查核結論（「⋯因此，傳言為『部分錯誤』訊息。」），沒有獨立
    的主張段落。存錯的後果不只是欄位髒：下游的主張同一性驗證會拿使用者的主張去
    比一句沒有主題的結論句，判成「不同主張」而放棄一則明明查過的謠言。
    """

    def _soup(self, html):
        return BeautifulSoup(html, "html.parser")

    def test_legacy_article_takes_claim_from_title_not_conclusion(self):
        from scraper_tfc import _extract_claim
        conclusion = ("<p>傳言說法缺乏醫學根據，過度誇大喝水可治病，"
                      "因此，為「錯誤」訊息。</p>")
        title = "【錯誤】網傳「喝水溫度決定壽命，僅用水就能治療心臟病」？"
        claim = _extract_claim(self._soup(conclusion), title)
        self.assertEqual(claim, "網傳「喝水溫度決定壽命，僅用水就能治療心臟病」？")
        self.assertNotIn("因此", claim)

    def test_new_article_without_prefix_falls_back_to_body(self):
        """新站文章的標題沒有前綴，主張在內文的獨立段落裡。"""
        from scraper_tfc import _extract_claim
        body = "<p>網傳「疫苗是人口滅絕工具」？</p><p>其他段落</p>"
        claim = _extract_claim(self._soup(body), "疫苗相關查核報告")
        self.assertEqual(claim, "網傳「疫苗是人口滅絕工具」？")

    def test_prefix_present_but_nothing_after_it_falls_back_to_body(self):
        from scraper_tfc import _extract_claim
        body = "<p>網傳「某個說法」？</p>"
        claim = _extract_claim(self._soup(body), "【錯誤】")
        self.assertEqual(claim, "網傳「某個說法」？")

    def test_no_claim_anywhere_returns_empty(self):
        from scraper_tfc import _extract_claim
        self.assertEqual(_extract_claim(self._soup("<p>無關內容</p>"), "沒有前綴"), "")


class TestBoundaryAwareChunking(unittest.TestCase):
    """切塊要在語意邊界上斷開，不要從句子中間切。

    舊版是 `text[start:start+500]` 的硬切：線上 TFC 那批平均每篇 1,578 字、
    切成 4 片，每一片都從句子中間斷開。語意殘缺的片段直接進向量空間，也直接
    被下游拿去改寫理由。
    """

    def test_short_text_is_one_chunk(self):
        from main_pipeline import chunk_text
        self.assertEqual(chunk_text("很短的一句話。"), ["很短的一句話。"])

    def test_empty_input_returns_empty_list(self):
        from main_pipeline import chunk_text
        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text(None), [])

    def test_chunks_end_at_sentence_boundaries(self):
        """這是本次變更的重點：切片不得從句子中間斷開。"""
        from main_pipeline import chunk_text
        text = "".join(f"這是第{i}句話，內容大約二十個字左右填充。" for i in range(1, 60))
        chunks = chunk_text(text, chunk_size=200)
        self.assertGreater(len(chunks), 1, "測資應該要被切成多片")
        for chunk in chunks:
            with self.subTest(chunk=chunk[-12:]):
                self.assertTrue(chunk.rstrip().endswith(("。", "！", "？", "；", "，")),
                                f"切片結尾不在標點上：{chunk[-16:]!r}")

    def test_no_chunk_exceeds_size(self):
        from main_pipeline import chunk_text
        text = "".join(f"句子{i}。" for i in range(1, 400))
        for chunk in chunk_text(text, chunk_size=300):
            self.assertLessEqual(len(chunk), 300)

    def test_paragraph_boundary_preferred_over_sentence(self):
        """有段落分隔時應優先在段落切，而不是先切句子。"""
        from main_pipeline import chunk_text
        para = "第一段的句子。" * 12          # 約 84 字
        text = para + "\n\n" + para
        chunks = chunk_text(text, chunk_size=100)
        self.assertTrue(any(c.rstrip().endswith("\n\n") or c.endswith("。")
                            for c in chunks))
        self.assertTrue(all("\n\n" not in c.strip() for c in chunks),
                        "段落分隔不該留在切片中間")

    def test_single_oversized_sentence_falls_back_to_hard_cut(self):
        """單一句子就超過上限時仍要能切——硬切是最後一層，不是常態。"""
        from main_pipeline import chunk_text
        text = "字" * 700 + "。"
        chunks = chunk_text(text, chunk_size=300)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 300)
        self.assertEqual("".join(chunks), text, "硬切不得遺失字元")

    def test_no_content_is_lost(self):
        """切片接回來要等於原文——切塊不得遺失或重複內容。

        舊版有 50 字 overlap 所以接不回原文；新版不再重疊，這條斷言因此成立，
        也順便鎖住「重疊被悄悄加回來」這種回歸。
        """
        from main_pipeline import chunk_text
        text = "".join(f"第{i}句內容在這裡。" for i in range(1, 120))
        self.assertEqual("".join(chunk_text(text, chunk_size=250)), text)

    def test_whitespace_only_pieces_are_dropped(self):
        from main_pipeline import chunk_text
        self.assertEqual(chunk_text("   \n\n   "), [])


class TestChunkerVersioning(unittest.TestCase):
    """切法改變時，既有文章要逐步重切——不需要人工全刪重灌。

    ETL 以 url 判定「已存在」會直接跳過，沒有這個標記的話切法一改，舊資料就
    永遠留在舊邊界上。
    """

    def test_old_chunker_version_triggers_rewrite(self):
        from main_pipeline import upload_to_mongodb, CHUNKER_VERSION

        collection = FakeCollection([
            {"url": "https://example.tw/a", "original_title": "舊文",
             "chunk_content": "舊切法的殘句", "chunk_index": 1, "total_chunks": 1,
             "embedding": [0.1], "updated_at": "2026-01-01",
             "chunker_version": CHUNKER_VERSION - 1},
        ])
        article = {"source": "測試", "url": "https://example.tw/a", "title": "舊文",
                   "content": "第一句。第二句。", "updated_at": "2026-01-01"}

        upload_to_mongodb([article], collection, embed_fn=fake_embed_ok)

        self.assertTrue(collection.deleted_filters, "應刪除舊切片後重寫")
        self.assertTrue(collection.inserted_batches, "應寫入新切片")
        self.assertTrue(
            all(d["chunker_version"] == CHUNKER_VERSION
                for d in collection.inserted_batches[0]),
            "新切片要標上目前的切法版本")

    def test_missing_chunker_version_is_treated_as_v1(self):
        """本次變更之前寫入的文件沒有這個欄位，要被視為最舊的版本。"""
        from main_pipeline import upload_to_mongodb

        collection = FakeCollection([
            {"url": "https://example.tw/b", "original_title": "無版本欄位",
             "chunk_content": "內容", "chunk_index": 1, "total_chunks": 1,
             "embedding": [0.1], "updated_at": "2026-01-01"},
        ])
        article = {"source": "測試", "url": "https://example.tw/b",
                   "title": "無版本欄位", "content": "第一句。第二句。",
                   "updated_at": "2026-01-01"}

        upload_to_mongodb([article], collection, embed_fn=fake_embed_ok)
        self.assertTrue(collection.inserted_batches, "缺欄位應視為 v1 而重切")

    def test_same_chunker_version_does_not_rewrite(self):
        """版本相同且無其他重寫理由時不得重算——那會浪費整批 embedding 額度。"""
        from main_pipeline import upload_to_mongodb, CHUNKER_VERSION

        collection = FakeCollection([
            {"url": "https://example.tw/c", "original_title": "現行版本",
             "chunk_content": "內容", "chunk_index": 1, "total_chunks": 1,
             "embedding": [0.1], "updated_at": "2026-01-01",
             "chunker_version": CHUNKER_VERSION},
        ])
        article = {"source": "測試", "url": "https://example.tw/c",
                   "title": "現行版本", "content": "第一句。第二句。",
                   "updated_at": "2026-01-01"}

        def must_not_embed(text):
            raise AssertionError("版本相同不應重新向量化")

        upload_to_mongodb([article], collection, embed_fn=must_not_embed)
        self.assertEqual(collection.inserted_batches, [])


class TestEmbedThrottle(unittest.TestCase):
    """節流間隔要小到能在時間上限內跑滿當日額度。

    綁住 ETL 的是每天 1,000 次的上限，不是速率。舊值 2 秒讓跑滿額度就要 33
    分鐘，加上約 43 分鐘的爬蟲，2026-08-23 那次重切因此在還沒用完額度（只用了
    978 次）就撞到 workflow 時間上限被砍。
    """

    def test_interval_allows_daily_quota_within_timeout(self):
        from main_pipeline import EMBED_CALL_INTERVAL_SECONDS
        DAILY_QUOTA = 1000
        SCRAPE_MINUTES = 45      # 四個來源實測約 43 分鐘，取整數上界
        TIMEOUT_MINUTES = 300

        embed_minutes = DAILY_QUOTA * EMBED_CALL_INTERVAL_SECONDS / 60
        self.assertLess(
            SCRAPE_MINUTES + embed_minutes, TIMEOUT_MINUTES * 0.5,
            "跑滿當日額度應在時間上限的一半以內，留足重切期間的餘裕")

    def test_interval_stays_under_a_conservative_rpm_ceiling(self):
        """不要為了快而把間隔壓到可能觸發每分鐘限制的程度。

        Google 沒有公開列出 embedding 在免費方案的每分鐘上限，100 RPM 是常見值，
        這裡以它為保守天花板。猜錯的代價由既有重試路徑吸收（40 秒 × 2 次足以
        跨過一個分鐘視窗），但仍不該主動貼著天花板跑。
        """
        from main_pipeline import EMBED_CALL_INTERVAL_SECONDS
        rpm = 60 / EMBED_CALL_INTERVAL_SECONDS
        self.assertLess(rpm, 100, "間隔太小，可能主動觸發每分鐘限制")

    def test_transient_rate_limit_backoff_spans_a_minute_window(self):
        """重試的等待總和要足以跨過一個分鐘視窗。

        這是節流猜錯時的安全網：若 0.7 秒確實觸發了每分鐘限制，重試必須等得夠久
        才能恢復，否則會被誤判成每日額度耗盡而讓本次執行提早結束。
        """
        import inspect
        import main_pipeline
        source = inspect.getsource(main_pipeline.get_embedding)
        self.assertIn("time.sleep(40)", source)
        # max_retries=3 → 兩次 40 秒的等待，總和 80 秒 > 60 秒視窗
        self.assertEqual(
            inspect.signature(main_pipeline.get_embedding).parameters["max_retries"].default,
            3)


class TestMohwTruthClarification(unittest.TestCase):
    """衛福部「真相說明」爬蟲。

    這一頁是跨機關的彙整頁，所以測試的重點不是「解析一種版面」，而是
    **分派是否正確、以及該排除的有沒有真的被排除**。靜默多收一個網域會產生
    重複文件（fda 那 199 筆），靜默少收一個網域會讓 14 篇文章消失而沒人知道
    （nhi 那批就是這樣差點被漏掉——原始設計的網域表根本沒列到它）。
    """

    def test_roc_year_is_converted_to_gregorian(self):
        """民國轉西元。三位數（含）以下才視為民國年。"""
        self.assertEqual(scraper_mohw.roc_to_gregorian("115-09-01"), "2026-09-01")
        self.assertEqual(scraper_mohw.roc_to_gregorian("102-07-01"), "2013-07-01")
        self.assertEqual(scraper_mohw.roc_to_gregorian("110-06-13"), "2021-06-13")

    def test_gregorian_year_is_left_alone(self):
        """已經是西元的原樣回傳——四位數年份不可能是民國年。"""
        self.assertEqual(scraper_mohw.roc_to_gregorian("2026-09-01"), "2026-09-01")

    def test_unparseable_date_returns_none_not_a_guess(self):
        """抽不到日期時回 None，不猜。

        `published_at` 是 Tier 2 選材排序與 Tier 1 時效門檻的依據，塞一個猜
        出來的日期進去，錯誤會安靜地傳到推播端。
        """
        for bad in ("", None, "abc", "115-09", "115/09/01"):
            self.assertIsNone(scraper_mohw.roc_to_gregorian(bad), f"{bad!r} 應該回 None")

    def test_fda_and_nhi_are_excluded_by_domain(self):
        """食藥署與健保署在分派表上是明確排除，不是未知網域。

        兩者的排除理由不同，但都必須是**有意識的決定**：fda 是因為 url 形式
        不同會讓 ETL 的去重失效（`http://` + 小寫 `/tc/` + utm 參數），
        nhi 是因為站台回 403。
        """
        self.assertIn("www.fda.gov.tw", scraper_mohw._EXCLUDED)
        self.assertIn("www.nhi.gov.tw", scraper_mohw._EXCLUDED)
        self.assertNotIn("www.fda.gov.tw", scraper_mohw._DISPATCH)
        self.assertNotIn("www.nhi.gov.tw", scraper_mohw._DISPATCH)

    def test_dispatch_covers_exactly_the_three_parsed_domains(self):
        """分派表釘住三個來源名。

        來源名必須與實際發布機關一致——這一頁上的文章分別掛在三個站上，全部
        標成「衛福部」就是引用錯機關，而使用者點進連結會看到別的網域。
        """
        self.assertEqual(
            {host: name for host, (name, _, _) in scraper_mohw._DISPATCH.items()},
            {
                "www.mohw.gov.tw": "衛福部真相說明",
                "www.hpa.gov.tw": "國健署真相與闢謠",
                "www.cdc.gov.tw": "疾管署闢謠專區",
            },
        )

    def test_cdc_is_not_in_expected_sources(self):
        """疾管署刻意不列入 EXPECTED_SOURCES，另外兩個要列入。

        24 篇四年前的疫情舊文抓不到時，不值得讓整條 ETL 以非零狀態碼結束。
        """
        self.assertIn("衛福部真相說明", main_pipeline.EXPECTED_SOURCES)
        self.assertIn("國健署真相與闢謠", main_pipeline.EXPECTED_SOURCES)
        self.assertNotIn("疾管署闢謠專區", main_pipeline.EXPECTED_SOURCES)

    def test_page_limit_is_not_hardcoded_to_the_measured_value(self):
        """翻頁上限不得寫死 54——那是 2026-09-09 的實測值，站方增刪就會變。"""
        import inspect
        params = inspect.signature(scraper_mohw.get_mohw_articles).parameters
        self.assertGreater(params["max_pages"].default, 54)

    def test_list_page_yields_title_date_and_link(self):
        """對真實列表頁驗證選擇器仍有效（動態一致性驗證）。

        比照 `test_02b_fda_data_integrity` 的做法：站方改版時，這支測試是唯一
        會讓我們知道的訊號。
        """
        rows = scraper_mohw._list_rows(1)
        self.assertEqual(len(rows), 20, "真相說明列表頁每頁應有 20 筆")
        for url, title, published_at in rows:
            self.assertTrue(url.startswith("http"), f"{url!r} 不是網址")
            self.assertTrue(title.strip(), f"{url} 沒有標題")
            self.assertRegex(published_at, r"^\d{4}-\d{2}-\d{2}$",
                             f"{url} 的日期沒有轉成西元：{published_at!r}")

    def test_scraper_produces_only_dispatched_sources(self):
        """冒煙測試：實跑第一頁，產出的來源名必須都在分派表裡。"""
        articles = scraper_mohw.get_mohw_articles(test_mode=True)
        self.assertTrue(articles, "真相說明一篇都沒抓到")
        allowed = {name for name, _, _ in scraper_mohw._DISPATCH.values()}
        for art in articles:
            self.assertIn(art["source"], allowed)
            self.assertTrue(art["url"], f"《{art['title']}》沒有 url")
            self.assertTrue(art["content"].strip(), f"《{art['title']}》內容是空的")
            self.assertRegex(art["published_at"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertNotIn("fda.gov.tw", art["url"])
            self.assertNotIn("nhi.gov.tw", art["url"])


class TestHealthMedia(unittest.TestCase):
    """健康媒體（元氣網）ETL。

    重點不是解析一種 XML，而是兩個邊界：寫進**另一個** collection（RAG 檢索
    沒有依來源過濾，寫錯地方就會變成闢謠引用來源），以及一篇都沒抓到時要紅燈。
    """

    SITEMAP = """<urlset><url><!-- cate：焦點 | sub：用藥停看聽 -->
<loc>https://health.udn.com/health/story/6012/9751804</loc>
<news:news><news:publication><news:name>udn 元氣網</news:name></news:publication>
<news:publication_date>2026-09-13T16:51:16+08:00</news:publication_date>
<news:title><![CDATA[吃止痛藥後血壓升高又多吃降壓藥？]]></news:title></news:news></url>
<url><loc>https://health.udn.com/health/story/5999/1</loc>
<news:news><news:publication_date>2026-09-13T08:00:00+08:00</news:publication_date>
<news:title><![CDATA[沒有分類註解的條目]]></news:title></news:news></url>
<url><loc>https://health.udn.com/health/story/5999/2</loc>
<news:news><news:title><![CDATA[沒有發布時間的條目]]></news:title></news:news></url></urlset>"""

    def test_parses_title_date_category_and_channel(self):
        entries = scraper_media.parse_gnews_entries(self.SITEMAP)
        first = entries[0]
        self.assertEqual(first["title"], "吃止痛藥後血壓升高又多吃降壓藥？")
        self.assertEqual(first["published_at"], "2026-09-13")
        self.assertEqual(first["category"], "焦點")
        self.assertEqual(first["subcategory"], "用藥停看聽")
        self.assertEqual(first["channel"], "6012")
        self.assertEqual(first["source_name"], "udn 元氣網")

    def test_published_at_is_the_taipei_date(self):
        """站方給的是 +08:00，日期部分就是台北日期，不得轉 UTC。

        轉成 UTC 會讓台北清晨 0～8 點發布的文章掉到前一天，推播端「今天或昨天」
        的時效判斷就會錯一天。
        """
        xml = self.SITEMAP.replace("2026-09-13T16:51:16", "2026-09-14T06:30:00")
        self.assertEqual(scraper_media.parse_gnews_entries(xml)[0]["published_at"], "2026-09-14")

    def test_missing_category_is_kept_as_none(self):
        """沒有分類註解的條目照樣存，分類為 None——要不要推由 CARE 端決定。"""
        entries = scraper_media.parse_gnews_entries(self.SITEMAP)
        self.assertIsNone(entries[1]["category"])

    def test_entry_without_date_is_dropped(self):
        """沒有發布時間的條目不存：推播端的時效判斷完全依賴這個欄位。"""
        titles = [e["title"] for e in scraper_media.parse_gnews_entries(self.SITEMAP)]
        self.assertNotIn("沒有發布時間的條目", titles)

    def test_media_goes_to_its_own_collection(self):
        """寫進 daily_health_news，絕不是 health_articles_chunks。"""
        self.assertEqual(scraper_media.COLLECTION_NAME, "daily_health_news")
        self.assertNotEqual(scraper_media.COLLECTION_NAME, "health_articles_chunks")

    def test_empty_fetch_is_a_failure(self):
        """48 小時窗裡一篇都沒有，幾乎只可能是站方改版或網路／憑證問題。"""
        rc = scraper_media.media_job(fetch=lambda: [], collection_factory=lambda: None)
        self.assertEqual(rc, 1)

    def test_upsert_is_idempotent(self):
        """sitemap 是 48 小時窗，同一篇會連兩天出現；第二次只更新、不新增。"""
        coll = _FakeUpsertCollection()
        article = scraper_media.parse_gnews_entries(self.SITEMAP)[0]
        self.assertEqual(scraper_media.upsert_media_articles([article], coll), 1)
        self.assertEqual(scraper_media.upsert_media_articles([article], coll), 0)
        self.assertEqual(len(coll.docs), 1)

    def test_successful_job_returns_zero(self):
        coll = _FakeUpsertCollection()
        entries = scraper_media.parse_gnews_entries(self.SITEMAP)
        rc = scraper_media.media_job(fetch=lambda: entries, collection_factory=lambda: coll)
        self.assertEqual(rc, 0)
        self.assertEqual(len(coll.docs), len(entries))


class _FakeUpsertCollection:
    """只實作 upsert_media_articles 用到的兩個方法。"""

    def __init__(self):
        self.docs = {}

    def create_index(self, *args, **kwargs):
        return None

    def update_one(self, query, update, upsert=False):
        url = query["url"]
        is_new = url not in self.docs
        doc = self.docs.setdefault(url, {})
        doc.update(update["$set"])
        if is_new:
            doc.update(update.get("$setOnInsert", {}))

        class _Result:
            upserted_id = url if is_new else None
        return _Result()


class TestMohwRetry(unittest.TestCase):
    """真相說明爬蟲的重試與「列表失敗不整批中止」。

    2026-09-13 GitHub Actions：第 5 頁列表 RemoteDisconnected，沒有重試、直接
    break，那一輪只進 67 篇（應約 810 篇）。
    """

    MOHW = "https://www.mohw.gov.tw/cp-4343-{}-1.html"

    @staticmethod
    def _http_error(status):
        resp = requests.Response()
        resp.status_code = status
        return requests.HTTPError(response=resp)

    def test_transient_disconnect_is_retried(self):
        calls, slept = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise requests.ConnectionError("Remote end closed connection")
            return "ok"

        self.assertEqual(scraper_mohw.with_retries(flaky, sleep=slept.append), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept, [2, 5])

    def test_gives_up_after_three_attempts(self):
        calls = []

        def down():
            calls.append(1)
            raise requests.ConnectionError("down")

        with self.assertRaises(requests.ConnectionError):
            scraper_mohw.with_retries(down, sleep=lambda s: None)
        self.assertEqual(len(calls), 3)

    def test_404_is_not_retried(self):
        """404 不重試：重打只是多等，而且 404 本來就不代表下架。"""
        calls = []

        def missing():
            calls.append(1)
            raise self._http_error(404)

        with self.assertRaises(requests.HTTPError):
            scraper_mohw.with_retries(missing, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_503_is_retried(self):
        calls = []

        def busy():
            calls.append(1)
            if len(calls) == 1:
                raise self._http_error(503)
            return "ok"

        self.assertEqual(scraper_mohw.with_retries(busy, sleep=lambda s: None), "ok")

    def _detail(self, url):
        return {"title": "t", "content": "c", "source": "衛福部真相說明"}

    def test_one_failed_list_page_does_not_stop_the_crawl(self):
        """第 2 頁失敗，第 3 頁照樣要抓——以前是 break，後面全部丟掉。"""
        pages = {
            1: [(self.MOHW.format(1), "一", "2026-09-01")],
            3: [(self.MOHW.format(3), "三", "2026-08-01")],
        }

        def list_rows(page):
            if page == 2:
                raise requests.ConnectionError("Remote end closed connection")
            return pages.get(page, pages[3])  # 第 4 頁起重複第 3 頁＝翻過頭

        articles = scraper_mohw.get_mohw_articles(
            list_rows=list_rows, parse_detail=self._detail, sleep=lambda s: None)
        self.assertEqual([a["title"] for a in articles], ["一", "三"])

    def test_consecutive_list_failures_stop_the_crawl(self):
        """站台整個掛掉時，連續 3 頁失敗就停，不對剩下上百頁各重試一輪。"""
        calls = []

        def list_rows(page):
            calls.append(page)
            raise requests.ConnectionError("down")

        articles = scraper_mohw.get_mohw_articles(
            list_rows=list_rows, parse_detail=self._detail, sleep=lambda s: None)
        self.assertEqual(articles, [])
        self.assertEqual(calls, [1, 2, 3])


class FakeResponse:
    """只帶 headers 與 content 的假回應，給 make_soup 用。"""

    def __init__(self, content: bytes, content_type: str, encoding: str | None = None):
        self.content = content
        self.headers = {"content-type": content_type}
        self.encoding = encoding


class TestMakeSoupEncoding(unittest.TestCase):
    """頁面沒有 <meta charset> 時，編碼要以 HTTP 標頭為準。

    衛福部有一批頁面正是這樣：標頭寫 charset=utf-8、HTML 裡沒有 meta。
    BeautifulSoup 自己猜會猜成西里爾語系，整篇存成亂碼而且不會報錯——
    2026-09-19 在知識庫裡找到 10 篇這種文章。
    """

    HTML = "<html><body><div id='x'>疾病管制署今日表示</div></body></html>"

    def test_header_charset_wins_when_page_has_no_meta(self):
        from utils import make_soup

        response = FakeResponse(
            self.HTML.encode("utf-8"), "text/html; charset=utf-8", "utf-8"
        )
        soup = make_soup(response)
        self.assertEqual(soup.select_one("#x").get_text(), "疾病管制署今日表示")

    def test_defaults_to_utf8_when_header_has_no_charset(self):
        from utils import make_soup

        response = FakeResponse(self.HTML.encode("utf-8"), "text/html", None)
        soup = make_soup(response)
        self.assertEqual(soup.select_one("#x").get_text(), "疾病管制署今日表示")


class TestClaimTagger(unittest.TestCase):
    """政府闢謠文章的標籤抽取與過濾。"""

    def test_claim_from_title_strips_leadin_and_agency(self):
        from claim_tagger import claim_from_title

        cases = {
            "(疾管署) 網路謠傳，台灣愛滋防治政策失效導致感染數持續上升?": "台灣愛滋防治政策失效導致感染數持續上升",
            "「皮蛋是用馬尿浸泡製成的」，這是真的嗎？": "皮蛋是用馬尿浸泡製成的",
            "有關網路社群謠傳「高雄登革熱防治經費0元」，是真的嗎？": "高雄登革熱防治經費0元",
            "(健康署) 確診或疑似感染COVID-19的婦女可以母乳哺育嗎？": "確診或疑似感染COVID-19的婦女可以母乳哺育嗎",
        }
        for title, expected in cases.items():
            self.assertEqual(claim_from_title(title), expected, title)

    def test_should_tag_keeps_short_but_real_rumors(self):
        """短不是問題：「可樂會殺精」「吃正露丸會致癌」正是長輩最常轉傳的句型。

        2026-09-19 原本設 8 字門檻，刷掉的多半是真謠言，已改成只擋 4 字以下。
        名詞片語擋不擋交給查核管線的同一性驗證，不在這裡多設一道。
        """
        from claim_tagger import should_tag

        for claim in ("可樂會殺精", "吃正露丸會致癌", "無花果不能吃", "吃肉桂能降血糖"):
            row = {
                "source": "食藥署闢謠專區",
                "title": f"{claim}，這是真的嗎？",
                "claim": claim,
                "verdict": "錯誤",
            }
            self.assertTrue(should_tag(row), claim)

    def test_should_tag_rejects_policy_responses(self):
        """衛福部真相說明有一半在回應媒體報導，不是謠言；CARE 不對政策爭議發判定。"""
        from claim_tagger import should_tag

        policy = {
            "source": "衛福部真相說明",
            "title": "回應「申請長照平均耗32天」報導：持續強化家庭照顧者支持資源",
            "claim": "申請長照平均耗32天，照顧者憂淪長照難民",
            "verdict": "事實釐清",
        }
        rumor = {
            "source": "衛福部真相說明",
            "title": "網傳「每人補助疫情援助金1萬元」為假訊息",
            "claim": "每人補助疫情援助金1萬元",
            "verdict": "錯誤",
        }
        self.assertFalse(should_tag(policy))
        self.assertTrue(should_tag(rumor))

    def test_should_tag_requires_both_fields(self):
        from claim_tagger import should_tag

        base = {"source": "食藥署闢謠專區", "title": "「吃木瓜可以豐胸」，這是真的嗎？"}
        self.assertFalse(should_tag({**base, "claim": "吃木瓜可以豐胸", "verdict": ""}))
        self.assertFalse(should_tag({**base, "claim": "", "verdict": "錯誤"}))
        self.assertTrue(should_tag({**base, "claim": "吃木瓜可以豐胸", "verdict": "錯誤"}))


class TestCofactsScraper(unittest.TestCase):
    """Cofacts 的品質門檻與內文組法。"""

    @staticmethod
    def _node(text, replies):
        return {
            "id": "abc",
            "text": text,
            "createdAt": "2026-09-01T00:00:00.000Z",
            "articleReplies": replies,
        }

    @staticmethod
    def _reply(rtype="RUMOR", positive=5, negative=0, reference="https://ref.tw/1"):
        return {
            "positiveFeedbackCount": positive,
            "negativeFeedbackCount": negative,
            "reply": {
                "id": "r1",
                "type": rtype,
                "text": "假的，沒有這回事。",
                "reference": reference,
            },
        }

    def _fetch(self, nodes):
        import scraper_cofacts

        pages = iter([{"ListArticles": {
            "pageInfo": {"lastCursor": None},
            "edges": [{"node": n} for n in nodes],
        }}, {"ListArticles": {"pageInfo": {"lastCursor": None}, "edges": []}}])

        def post(query, variables, timeout=60):
            try:
                return next(pages)
            except StopIteration:
                return {"ListArticles": {"pageInfo": {"lastCursor": None}, "edges": []}}

        return scraper_cofacts.get_cofacts_articles(
            categories=["medical"], post=post, sleep=lambda s: None)

    def test_skips_opinionated_and_unsourced_and_low_feedback(self):
        """三種都不是「有人背書的真假判定」，收進來只會讓判定卡失去可信度。"""
        import scraper_cofacts

        cases = {
            "個人意見": self._reply(rtype="OPINIONATED"),
            "沒附出處": self._reply(reference=""),
            "正評不足": self._reply(positive=2),
            "負評蓋過": self._reply(positive=4, negative=3),
        }
        for name, reply in cases.items():
            with self.subTest(name):
                self.assertIsNone(
                    scraper_cofacts.pick_reply([reply]), name)

    def test_skips_url_only_and_too_short_messages(self):
        """實測 8～22% 的回報只有一條 YouTube 連結，那種當主張沒有意義。"""
        rows = self._fetch([
            self._node("https://youtu.be/abcdefg", [self._reply()]),
            self._node("太短", [self._reply()]),
        ])
        self.assertEqual(rows, [])

    def test_maps_reply_type_to_verdict(self):
        rumor = self._fetch([self._node("網傳吃鳳梨心可以治好痛風不用看醫生真的假的", [self._reply()])])
        self.assertEqual(rumor[0]["verdict"], "錯誤")
        self.assertEqual(rumor[0]["verdict_slug"], "incorrect")

        truth = self._fetch([
            self._node("疾管署說流感疫苗每年都要打才有保護力這是真的嗎",
                       [self._reply(rtype="NOT_RUMOR")])])
        self.assertEqual(truth[0]["verdict"], "正確")

    def test_content_starts_with_the_original_message(self):
        """比對打在切片向量上；只放反駁那段，使用者的謠言原文對不上。"""
        rows = self._fetch([self._node("網傳吃鳳梨心可以治好痛風不用看醫生真的假的", [self._reply()])])
        content = rows[0]["content"]
        self.assertTrue(content.startswith("網傳訊息：網傳吃鳳梨心"), content[:40])
        self.assertIn("查核回覆：", content)
        self.assertIn("出處：", content)

    def test_carries_licence_attribution(self):
        """CC BY-SA 4.0 要求顯示時標明社群與授權。"""
        import scraper_cofacts

        rows = self._fetch([self._node("網傳吃鳳梨心可以治好痛風不用看醫生真的假的", [self._reply()])])
        self.assertEqual(rows[0]["attribution"], scraper_cofacts.COFACTS_ATTRIBUTION)
        self.assertIn("CC BY-SA 4.0", rows[0]["attribution"])


class TestMyGoPenScraper(unittest.TestCase):
    """MyGoPen 的標題解析與「只取標題與連結」的授權界線。"""

    @staticmethod
    def _entry(title, url="https://www.mygopen.com/2026/09/x.html"):
        return {
            "title": {"$t": title},
            "published": {"$t": "2026-09-01T10:00:00.000+08:00"},
            "link": [{"rel": "alternate", "href": url}],
            "content": {"$t": "<p>完整內文不該被存下來</p>"},
        }

    def _fetch(self, entries):
        import scraper_mygopen

        pages = [{"entry": entries}, {"entry": []}]

        def fetch(start_index):
            return pages.pop(0) if pages else {"entry": []}

        return scraper_mygopen.get_mygopen_articles(fetch=fetch, sleep=lambda s: None)

    def test_parses_verdict_prefix(self):
        from scraper_mygopen import parse_title

        self.assertEqual(
            parse_title("【錯誤】網傳「吃鳳梨心可以治痛風」？"),
            ("錯誤", "incorrect", "網傳「吃鳳梨心可以治痛風」"),
        )
        # MyGoPen 特有的前綴依語意歸併到 CARE 認得的五個判定
        self.assertEqual(parse_title("【易生誤解】某某說法")[0], "部分錯誤")
        self.assertEqual(parse_title("【詐騙】假冒衛福部簡訊")[0], "錯誤")
        # 認不得的前綴不猜
        self.assertIsNone(parse_title("【活動】謠言惑眾獎票選"))
        self.assertIsNone(parse_title("沒有前綴的標題"))

    def test_stores_claim_and_link_only(self):
        """授權界線：MyGoPen 沒有開放授權聲明，在取得授權前不存內文。"""
        rows = self._fetch([self._entry("【錯誤】網傳「吃鳳梨心可以治痛風」？")])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["content"], row["claim"])
        self.assertNotIn("完整內文", row["content"])
        self.assertTrue(row["url"].startswith("https://www.mygopen.com/"))

    def test_skips_entries_without_recognised_verdict(self):
        rows = self._fetch([
            self._entry("【活動】2026 教師研習營"),
            self._entry("【錯誤】網傳某某說法", url="https://www.mygopen.com/2026/09/y.html"),
        ])
        self.assertEqual([r["url"] for r in rows], ["https://www.mygopen.com/2026/09/y.html"])
