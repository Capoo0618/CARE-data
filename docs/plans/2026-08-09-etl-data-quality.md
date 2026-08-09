# CARE-data ETL 資料品質改善 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修好 CARE-data ETL 的寫入完整性與內容品質，讓下游 CARE 的 RAG 知識庫拿到語意完整的 chunk。

**Architecture:** 分兩個 openspec change。`etl-write-integrity`（批次 1）只動寫入邏輯、不改任何 chunk 內容或向量，可獨立上線；`etl-content-quality`（批次 2）同時改清洗、切分、embedding 三處，**全部會使既有向量失效**，因此五項綁在一次全量重建裡做，並以新 collection + cutover 的方式遷移。

**Tech Stack:** Python 3.10、requests、BeautifulSoup4、Gemini Embedding API、pymongo、unittest、GitHub Actions

## 來源

問題清單與實測數據見下游 CARE repo 的 `docs/care-data-issues.md`（8 項 + 7b + 次要項）。
本計畫是那份報告的實作版，不重複論證，只在每個 task 標明對應項次。

## Global Constraints

- 面向使用者文件與 commit 描述使用**繁體中文**
- **測試分兩類，不要混**（來源 `openspec/config.yaml` 的 `rules.tasks`）：
  - 純函式／寫入邏輯 → 依賴注入假件，**測試中不得發出真實網路請求**
  - 既有 `test_02_api_data_integrity` / `test_03_tfc_data_integrity` → 刻意打真實網路，**保留其線上特性，不要改成 mock**
- Definition of Done：`python test_system.py` 全綠
- **下游契約**：CARE Backend 讀 `chunk_content` / `embedding`(3072) / `source_name` / `url` / `original_title` / `chunk_index` / `total_chunks`。**批次 2 完成前不得改動線上 collection `health_articles_chunks`**——批次 2 一律寫入新 collection
- 不要為了讓既有測試通過而改回舊行為；既有測試若編碼了舊行為，更新它並在報告中說明理由
- 每個 change 合併後執行 `openspec archive <change>`

## 執行順序

```
批次 1（etl-write-integrity）—— 不動向量，可獨立上線
  Task 1  openspec change 骨架
  Task 2  全有或全無寫入 + early-stopping 改集合差集      (#7, #7b)
  Task 3  存日期並依修改日期更新                          (#8)
  Task 4  移除 verify=False                               (次要項)
  Task 5  刪除過時副本 test_pipeline.py

批次 2（etl-content-quality）—— 會使既有向量失效，需全量重建
  Task 6  openspec change 骨架
  Task 7  clean_html 改 BS4 並保留段落                    (#3, #4)
  Task 8  chunk_text 改句界切分                           (#5)
  Task 9  標題落地 chunk_content + taskType               (#2, #1)
  Task 10 改用批次 embedding 端點
  Task 11 遷移腳本：v2 collection + 雙來源回填
```

**非範圍**：食藥署 URL（#6）已決定採 B 案（接受無 URL，下游已支援「來源名｜標題」顯示），不做。
CARE 端的三個 cutover 改動（rerank 雙前綴防護、`rag_tighten_golden.py` 吃空白修正、collection 切換）屬另一個 repo，不在本計畫。

---

### Task 1: openspec change `etl-write-integrity` 骨架

**Files:**
- Create: `openspec/changes/etl-write-integrity/.openspec.yaml`
- Create: `openspec/changes/etl-write-integrity/proposal.md`
- Create: `openspec/changes/etl-write-integrity/design.md`
- Create: `openspec/changes/etl-write-integrity/tasks.md`
- Create: `openspec/changes/etl-write-integrity/specs/etl-ingestion/spec.md`

**Interfaces:**
- Consumes: 無
- Produces: 供 Task 2–5 勾選的 `tasks.md`

- [ ] **Step 1: `.openspec.yaml`**

```yaml
schema: spec-driven
created: 2026-08-09
```

- [ ] **Step 2: `proposal.md`**

`## Why` — 兩個實測問題：

1. **部分寫入留下永久破洞**：`main_pipeline.py:87-88` 在某個 chunk 的 `get_embedding()` 失敗時只印警告並跳過該塊，其餘照常寫入。實例：線上 `pid=16703` 這篇 `total_chunks` 宣告 4，但庫中 `chunk_index` 只有 `[1, 3, 4]`。之後該文章會被 `find_one` 判定「已存在」而永遠跳過，破洞不會補。
2. **early stopping 會整批漏抓**：`main_pipeline.py:63-67` 一旦遇到單篇已存在，就 `skipped_sources.add(source_name)` 放棄該來源後續所有文章。若來源非嚴格時間排序、或中間某篇曾寫入失敗，後續新文章永遠補不回來。實測佐證：HPA API 回傳 1,000 筆，DB 只有 910 個 URL。

`## What Changes`：
- 寫入改為全有或全無：整篇文章所有 chunk 向量化都成功才 `insert_many`，任一失敗整篇不寫、留待下次
- early stopping 改為集合差集（一次 `distinct` 取既有 url／title，逐篇比對），只跳過該篇不跳過整個來源
- 寫入 `published_at` / `updated_at`；HPA 有 `修改日期` 時據以判斷是否重寫該篇
- 移除三處 `verify=False`
- 刪除過時副本 `test_pipeline.py`
- **非 BREAKING**：不改變 `chunk_content` 內容與 embedding 產生方式，**既有向量完全不受影響**

`## Capabilities`：
```markdown
### New Capabilities

- `etl-ingestion`：定義知識庫寫入的完整性保證（全有或全無）、去重與更新判定、以及來源掃描策略。

### Modified Capabilities

- （無）
```

`## Impact` — 程式：`main_pipeline.py`、`scraper_api.py`、`scraper_tfc.py`；刪除 `test_pipeline.py`；
測試：`test_system.py` 新增寫入邏輯的單元測試（依賴注入假件）；無新增相依套件。
**對下游 CARE Backend 的影響：無**——不改變任何既有欄位內容或向量，不需重建、不需 cutover。

- [ ] **Step 3: `design.md`**

`## Context` / `## Goals / Non-Goals` / `## Decisions`，`## Decisions` 需涵蓋：

- **D1 為何是「全有或全無」而非「補寫缺塊」**：補寫需要知道哪些塊缺了，但現行 schema 的
  `total_chunks` 只是宣告值、不保證與實際筆數一致（`pid=16703` 即為反例），且部分寫入的文章
  在語意上本來就不完整（缺的那塊可能正好是答案）。整篇重來成本低（單篇僅數個 chunk），
  正確性明確。
- **D2 為何用集合差集而非移除去重**：去重仍然需要（避免每日重跑整批重嵌入），
  只是判定粒度要從「來源」降到「文章」。一次 `collection.distinct()` 取回既有鍵，
  記憶體成本可忽略（約 1,600 篇），換掉逐篇 `find_one` 的 N 次往返。
- **D3 更新判定只用 `修改日期`**：只有 HPA API 提供該欄位；食藥署沒有，其文章視為不可偵測更新，
  維持「已存在即跳過」。不要用內容雜湊比對——那會讓每次清洗邏輯微調都觸發全量重寫。

- [ ] **Step 4: `tasks.md`**

`## 1. …` / `- [ ] 1.1 …` 格式，對應 Task 2–5，每節引用 `test_system.py` 中對應的測試名稱，
最後一節含 Definition of Done（`python test_system.py` 全綠）。

- [ ] **Step 5: `specs/etl-ingestion/spec.md`**

**用 `## ADDED Requirements`**（`openspec/specs/` 目前為空，沒有任何既有 requirement 可 MODIFY）：

```markdown
## ADDED Requirements

### Requirement: 寫入完整性

系統 SHALL 僅在一篇文章的**所有** chunk 都成功取得向量後，才將該篇寫入知識庫。任一 chunk 向量化失敗時，系統 SHALL NOT 寫入該篇的任何 chunk，並 SHALL 保留該篇於未寫入狀態，使下次執行得以重試。

#### Scenario: 單一 chunk 向量化失敗

- **WHEN** 某篇文章切出 4 個 chunk，其中第 2 個取得向量失敗
- **THEN** 該篇文章的 0 個 chunk 被寫入知識庫，且下次執行時該篇會被重新嘗試

#### Scenario: 全部成功才寫入

- **WHEN** 某篇文章所有 chunk 都成功取得向量
- **THEN** 該篇所有 chunk 以單次批次寫入，且 `total_chunks` 與實際寫入筆數一致

### Requirement: 去重與更新判定

系統 SHALL 以**文章**為單位判定是否已存在，SHALL NOT 因為某篇已存在而跳過同來源的其他文章。當來源提供修改日期且與知識庫中記錄的值不同時，系統 SHALL 以新版本取代該篇的所有既有 chunk。

#### Scenario: 已存在的文章不影響同來源其他文章

- **WHEN** 某來源回傳 100 篇文章，其中第 3 篇已存在於知識庫
- **THEN** 系統跳過第 3 篇，但仍檢查並處理第 4 至第 100 篇

#### Scenario: 來源文章改版

- **WHEN** 某篇文章的 `修改日期` 與知識庫中記錄的 `updated_at` 不同
- **THEN** 系統刪除該篇既有的所有 chunk，並以新內容重新寫入
```

跑 `openspec validate etl-write-integrity --strict` 確認通過。

- [ ] **Step 6: Commit**

```bash
git add openspec/changes/etl-write-integrity
git commit -m "docs(openspec): 新增 etl-write-integrity change 提案"
```

---

### Task 2: 全有或全無寫入 + early-stopping 改集合差集

**Files:**
- Modify: `main_pipeline.py`（`upload_to_mongodb`，第 49–94 行整段）
- Test: `test_system.py`

**Interfaces:**
- Consumes: 無
- Produces: `upload_to_mongodb(articles, collection, *, embed_fn=get_embedding) -> int`
  —— 新增 keyword-only 參數 `embed_fn`，供測試注入假件（Task 3 也會用到）

- [ ] **Step 1: 寫失敗測試**

加到 `test_system.py` 的 `TestHealthETLPipeline`。這是本 repo 第一個「不打網路」的單元測試，
需要兩個假件；請放在檔案上方、`TestHealthETLPipeline` 之前：

```python
class FakeCollection:
    """記錄呼叫的假 collection，讓寫入邏輯可在無網路下測試。"""

    def __init__(self, existing=None):
        self.docs = list(existing or [])
        self.inserted_batches = []
        self.deleted_filters = []

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


def fake_embed_ok(text):
    return [0.1, 0.2, 0.3]


def make_failing_embed(fail_on_nth):
    """第 fail_on_nth 次呼叫回傳空 list（模擬向量化失敗）。"""
    state = {"n": 0}

    def _embed(text):
        state["n"] += 1
        return [] if state["n"] == fail_on_nth else [0.1, 0.2, 0.3]

    return _embed
```

測試本體：

```python
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
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python test_system.py TestHealthETLPipeline.test_04_partial_embedding_failure_writes_nothing -v`
Expected: FAIL — `TypeError: upload_to_mongodb() got an unexpected keyword argument 'embed_fn'`

- [ ] **Step 3: 重寫 `upload_to_mongodb`**

把 `main_pipeline.py` 第 49–94 行整段換成：

```python
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

        if (url and url in existing_urls) or title in existing_titles:
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
            }
            for i, (chunk, vector) in enumerate(zip(chunks, vectors))
        ]
        collection.insert_many(docs)
        print(f"    ✅ 成功寫入 {len(docs)} 個切片")

        # 讓同一批次內的重複文章也能被擋掉
        if url:
            existing_urls.add(url)
        existing_titles.add(title)
        new_count += 1

    return new_count
```

- [ ] **Step 3b: 補 `url=None` 去重的守護測試**

食藥署那批文章 `url` 恆為 `None`（`scraper_api.py:44-45` 的 fallback 鏈條在該來源必然落空），
去重只能靠標題。Task 3 會再次修改 `upload_to_mongodb`，需要一個守護測試防止這條路徑被靜默破壞：

```python
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
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python test_system.py TestHealthETLPipeline.test_04_partial_embedding_failure_writes_nothing TestHealthETLPipeline.test_05_all_chunks_succeed_writes_once TestHealthETLPipeline.test_06_existing_article_does_not_skip_rest_of_source -v`
Expected: 3 tests PASS

- [ ] **Step 5: 跑全套確認無回歸**

Run: `python test_system.py -v`
Expected: 全部通過（test_02 / test_03 會打真實網路，需要網路連線）

- [ ] **Step 6: Commit**

```bash
git add main_pipeline.py test_system.py
git commit -m "fix(etl): 寫入改為全有或全無，去重判定由來源降到文章"
```

---

### Task 3: 存日期並依修改日期更新

**Files:**
- Modify: `scraper_api.py`（`get_api_articles`，兩支 API 的欄位對應）
- Modify: `main_pipeline.py`（`upload_to_mongodb`）
- Test: `test_system.py`

**Interfaces:**
- Consumes: Task 2 的 `upload_to_mongodb(..., embed_fn=...)`
- Produces: 文章 dict 新增 `published_at` / `updated_at` 兩個鍵；
  寫入文件新增同名兩個欄位

- [ ] **Step 1: 寫失敗測試**

```python
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
```

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

- [ ] **Step 2: 執行測試確認失敗**

Run: `python test_system.py TestHealthETLPipeline.test_08_updated_article_replaces_old_chunks -v`
Expected: FAIL — 舊 chunk 未被刪除（`deleted_filters` 為空）

- [ ] **Step 3: `scraper_api.py` 帶出日期欄位**

在 `cleaned_articles.append({...})`（約第 49–54 行）中補兩個鍵：

```python
                cleaned_articles.append({
                    "title": raw_title.strip(),
                    "content": clean_html(raw_content),
                    "source": source["name"],
                    "url": raw_url,
                    # 兩支 API 都有「發布日期」；只有 HPA 有「修改日期」，
                    # 食藥署取不到時為 None，代表該來源無法偵測更新。
                    "published_at": item.get("發布日期") or item.get("PublishDate"),
                    "updated_at": item.get("修改日期"),
                })
```

`scraper_tfc.py` 的 `cleaned_articles.append({...})` 也補上 `"published_at": None, "updated_at": None`，
讓兩支爬蟲回傳同樣的鍵集合。

- [ ] **Step 4: `upload_to_mongodb` 加入更新判定**

**刪除必須延後到「確定寫得出新版本」之後** —— 否則若刪完之後某個 chunk 向量化失敗，
舊 chunk 已消失、新的寫不進去，線上該篇文章直接不見。這會違反本 change 自己的 spec
（`specs/etl-ingestion/spec.md` 的「寫入完整性」requirement）。

在 Task 2 版本的「已存在則跳過」那段之前，插入**判定**（不刪除）：

```python
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
```

接著把原本的「已存在則跳過」條件改成讓改版文章通過：

```python
        if not needs_rewrite and ((url and url in existing_urls) or title in existing_titles):
            print(f"  ⏭️ 已存在，跳過: {title[:15]}...")
            continue
```

最後在 `collection.insert_many(docs)` **之前**才真正刪除舊版：

```python
        if needs_rewrite:
            collection.delete_many({"url": url})
        collection.insert_many(docs)
```

這樣所有的提早離開路徑（`if not chunks: continue`、`if failed: continue`）
都發生在刪除之前，舊資料不會在沒有替代品的情況下消失。
也因為不再需要 `discard`，順帶消除了「改版同時改標題時，舊標題殘留在
`existing_titles`」這個邊角問題。

並在 `docs` 的每個 dict 裡補兩個欄位：

```python
                "published_at": article.get("published_at"),
                "updated_at": article.get("updated_at"),
```

- [ ] **Step 5: 執行測試確認通過**

Run: `python test_system.py -v`
Expected: 全部通過

- [ ] **Step 6: Commit**

```bash
git add main_pipeline.py scraper_api.py scraper_tfc.py test_system.py
git commit -m "feat(etl): 保存發布／修改日期，並依修改日期重寫改版文章"
```

---

### Task 4: 移除 `verify=False`

**Files:**
- Modify: `scraper_api.py:32`
- Modify: `scraper_tfc.py:37,86`
- Test: 既有 `test_02` / `test_03` 覆蓋（它們會實際連線）

**Interfaces:** Consumes/Produces：無

- [ ] **Step 1: 移除三處 `verify=False` 與相關的警告抑制**

三支檔案中把 `requests.get(..., verify=False)` 的 `verify=False` 拿掉。
若移除後 `urllib3.disable_warnings(...)` 已無作用，一併移除該行與 `import urllib3`
（`test_system.py` 的 `urllib3` 匯入保留——它自己也有 `verify=False`，見 Step 3）。

- [ ] **Step 2: 執行測試確認來源網站憑證正常**

Run: `python test_system.py TestHealthETLPipeline.test_02_api_data_integrity TestHealthETLPipeline.test_03_tfc_data_integrity -v`
Expected: PASS。

**若任一來源出現 SSL 憑證錯誤：停下來回報，不要把 `verify=False` 加回去。**
政府網站憑證鏈不完整是真實情況，正確處理是指定 CA bundle 或記錄為已知限制，
而不是全域關閉驗證——把判斷交給人。

- [ ] **Step 3: `test_system.py` 內的 `verify=False` 一併處理**

該檔第 38、65 行的比對請求也用了 `verify=False`。若 Step 2 證實來源憑證正常，
一併移除，保持整個 repo 一致。

- [ ] **Step 4: Commit**

```bash
git add scraper_api.py scraper_tfc.py test_system.py
git commit -m "fix(etl): 移除 verify=False，恢復 TLS 憑證驗證"
```

---

### Task 5: 刪除過時副本 `test_pipeline.py`

**Files:**
- Delete: `test_pipeline.py`

**Interfaces:** Consumes/Produces：無

- [ ] **Step 1: 確認確實無人引用**

Run: `grep -rn "test_pipeline" --include='*.py' --include='*.yml' --include='*.yaml' --include='*.md' .`
Expected: 除了檔案自身之外沒有任何引用。**若有引用，停下來回報**。

- [ ] **Step 2: 說明為何刪除（寫進 commit message）**

`test_pipeline.py` 是 `main_pipeline.py` 的舊副本（早於 early-stopping 加入的版本），
內含各自一份 `chunk_text` 與 `get_embedding`。檔名叫 test 但不是測試——沒有 assertion、
`unittest` 不會收集它、CI 也沒有跑它。留著的具體風險：批次 2 會改 `chunk_text` 與
`get_embedding`，這個副本會靜默地保留舊行為，成為誤導下一個讀者的來源。

- [ ] **Step 3: 刪除並確認測試仍全綠**

```bash
git rm test_pipeline.py
python test_system.py -v
```

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: 刪除過時副本 test_pipeline.py

它是 main_pipeline.py 早期版本的複本（不含 early-stopping），
內有各自一份 chunk_text 與 get_embedding，無人引用、CI 未執行、
也沒有任何 assertion。批次 2 會改動這兩個函式，留著只會產生
一份靜默偏離的舊實作。"
```

- [ ] **Step 5: 收尾批次 1**

勾選 `openspec/changes/etl-write-integrity/tasks.md` 全部項目並 commit。
此時批次 1 可獨立開 PR 上線——**不影響任何既有向量**。

---

### Task 6: openspec change `etl-content-quality` 骨架

**Files:**
- Create: `openspec/changes/etl-content-quality/.openspec.yaml`
- Create: `openspec/changes/etl-content-quality/proposal.md`
- Create: `openspec/changes/etl-content-quality/design.md`
- Create: `openspec/changes/etl-content-quality/tasks.md`
- Create: `openspec/changes/etl-content-quality/specs/etl-content/spec.md`

**Interfaces:**
- Consumes: Task 1–5 完成的批次 1
- Produces: 供 Task 7–11 勾選的 `tasks.md`

- [ ] **Step 1: `.openspec.yaml`**（`schema: spec-driven` / `created: 2026-08-09`）

- [ ] **Step 2: `proposal.md`**

`## Why` — 三層問題，實測數據見下游 `docs/care-data-issues.md` 第 3、4、5、2、1 項：

- `utils.clean_html` 用 regex 去標籤，`<script>`/`<style>` 的**內容**會留成正文；
  且 `re.sub(r'\s+', ' ', ...)` 把換行壓成空白，段落結構全滅
- `chunk_text` 只能按固定 500 字元硬切（因為段落訊號已在上一步被抹掉），
  實測產生 127 筆 1 字元殘渣、480 筆 <100 字元（佔當時全庫 10.4%），
  且句子從中間斷開（例：`'元整及55萬8,000元。國民健康署呼籲...'`）
- 標題只進 embedding、沒進 `chunk_content`，導致下游 BM25 與 rerank 看不到標題
- embedding 未指定 `taskType`，實測等同 `RETRIEVAL_QUERY`（`cos(未指定, RETRIEVAL_QUERY) = 1.000000`）

`## What Changes`：清洗改 BS4 並保留段落、切分改句界、標題落地 `chunk_content`、
embedding 指定 `RETRIEVAL_DOCUMENT`、改用批次端點、以新 collection 遷移。

`## Capabilities`：
```markdown
### New Capabilities

- `etl-content`：定義清洗、切分與向量化產出的內容契約。

### Modified Capabilities

- （無）
```

`## Impact` — **BREAKING（對下游）**：
`chunk_content` 內容改變（含標題前綴）且 embedding 產生方式改變（taskType + 新切分），
**既有 3,000+ 筆向量全部失效**。需全量重建，寫入新 collection
`health_articles_chunks_v2`，並與 CARE Backend 協調 cutover（見 design.md 遷移策略）。
重建成本：約 2M tokens × $0.15/1M ≈ **$0.30**，改用批次端點後執行時間由 2.4 小時降至分鐘級。

- [ ] **Step 3: `design.md`**

`## Decisions` 需涵蓋：

- **D1 為何五項綁一次做**：這五項都會改變 `chunk_content` 或 embedding，任一項單獨上線
  都需要一次全量重嵌入。綁在一起 = 遷移一次。
- **D2 為何用新 collection 而非原地更新**：原地更新會讓 CARE 在重建期間讀到新舊混雜的
  向量（不同 taskType 的向量不可比），檢索品質會在重建的數小時內劣化。新 collection +
  cutover 讓切換是原子的，且保留回滾路徑。
- **D3 為何 `chunk_content` 存含標題的完整字串，而非另開欄位**：下游 BM25 索引
  （`care_text_index`）與 rerank 都直接讀 `chunk_content`；另開欄位需要下游同步改索引定義
  與程式。存同一份的代價是 chunk 略長（標題約 20–40 字），換取三個階段看到一致的文本。
  **下游 CARE 需同步防護雙前綴**（其 `rerank_document_text` 目前會再加一次 `主題：`）。
- **D4 句界切分的參數**：`max_chars=500` 維持不變（與現行相同量級，避免同時改動太多變因）；
  新增 `min_chars=30` 丟棄殘渣；超長單句才退回硬切。
- **D5 為何仍保留 overlap 的移除**：句界切分後，chunk 邊界落在句號上，相鄰 chunk 語意完整，
  原本 50 字元 overlap 的補償作用不再必要。移除可減少約 10% 的儲存與嵌入成本。

`## Risks` 需含：taskType 改變後排序品質的實際影響**未經 A/B 驗證**，
建議分兩階段重建（見 Task 11 Step 5）以取得歸因。

- [ ] **Step 4: `tasks.md`**（對應 Task 7–11，引用測試名稱，含 DoD）

- [ ] **Step 5: `specs/etl-content/spec.md`**

**用 `## ADDED Requirements`**，涵蓋三條 requirement：

```markdown
## ADDED Requirements

### Requirement: HTML 清洗保留段落結構

清洗 SHALL 移除 `<script>` 與 `<style>` 節點**及其內容**，SHALL NOT 僅移除標籤符號而保留其內文。清洗 SHALL 保留原始文件的段落分隔（以換行表示），SHALL NOT 將換行與行內空白一併壓縮為單一空白字元。

#### Scenario: script 內容不得混入正文

- **WHEN** 來源 HTML 含 `<script>var x = 1;</script>`
- **THEN** 清洗結果不包含 `var x = 1;`

#### Scenario: 段落分隔保留

- **WHEN** 來源 HTML 含兩個 `<p>` 段落
- **THEN** 清洗結果中兩段之間存在換行，而非單一空白字元

### Requirement: 切分依句界進行

切分 SHALL 優先在段落與句界（`。！？`）處切開，SHALL NOT 在句子中間切斷。切分結果 SHALL NOT 包含短於 `min_chars` 的殘片；長度不足者 SHALL 併入前一個 chunk。

#### Scenario: 不產生單字元殘渣

- **WHEN** 文章長度使最後一段僅剩數個字元
- **THEN** 該殘片併入前一個 chunk，切分結果中不存在長度小於 `min_chars` 的 chunk

### Requirement: 向量化與落地文本一致

系統 SHALL 以 `主題：{title}\n內容：{chunk}` 的格式產生向量，且 SHALL 將**同一份字串**寫入 `chunk_content`，使向量檢索、全文檢索與精排三個階段看到相同文本。向量化 SHALL 指定 `taskType` 為 `RETRIEVAL_DOCUMENT`。

#### Scenario: 落地文本與向量輸入相同

- **WHEN** 某 chunk 被寫入知識庫
- **THEN** 其 `chunk_content` 欄位的值，與產生其 `embedding` 時送給 API 的文本字串完全相同
```

跑 `openspec validate etl-content-quality --strict`。

- [ ] **Step 6: Commit**

```bash
git add openspec/changes/etl-content-quality
git commit -m "docs(openspec): 新增 etl-content-quality change 提案"
```

---

### Task 7: `clean_html` 改 BS4 並保留段落

**Files:**
- Modify: `utils.py`
- Modify: `scraper_tfc.py:89`（段落 join）
- Modify: `test_system.py`（既有 `test_01` 的案例 B 會失敗，需更新）

**Interfaces:**
- Consumes: 無
- Produces: `clean_html(raw_html) -> str`（行為改變：保留段落換行）

**⚠️ 既有測試會失敗，這是預期的**

`test_system.py:21` 現有案例：

```python
{"input": "  很多 \n\n 空白   和 \t 換行  ", "expected": "很多 空白 和 換行"},
```

這條測試把「換行被壓成空白」編碼成了**期望行為**——而那正是本 task 要修的問題。
必須更新這條案例，**不可以為了讓它通過而保留舊行為**。更新後應斷言換行被保留。

- [ ] **Step 1: 先寫新的失敗測試**

在 `test_01_clean_html_cases` 的 `cases` 中，把案例 B 改成兩條並新增案例 E：

```python
            # 案例 B：行內連續空白壓縮，但段落換行保留
            {"input": "第一段  有   空白\n\n第二段", "expected": "第一段 有 空白\n第二段"},
            # 案例 E：script／style 的內容不得留在正文
            {"input": "<p>正文</p><script>var x = 1;</script><style>.a{color:red}</style>",
             "expected": "正文"},
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python test_system.py TestHealthETLPipeline.test_01_clean_html_cases -v`
Expected: FAIL — 案例 B 得到 `第一段 有 空白 第二段`（換行被壓成空白）；
案例 E 得到含 `var x = 1;` 的字串

- [ ] **Step 3: 重寫 `utils.py`**

```python
# utils.py
import re
import html

from bs4 import BeautifulSoup


def clean_html(raw_html):
    """清洗 HTML，回傳保留段落結構的純文字。

    兩個刻意的行為：
    1. 用 BeautifulSoup 而非 regex —— regex 只能移除標籤符號，
       `<script>` / `<style>` 的內容會被當成正文留下來。
    2. 只壓縮「行內」連續空白，保留換行 —— 段落訊號是下游 chunk_text
       依句界／段落切分的依據，在這一步壓掉就再也救不回來。
    """
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    text = html.unescape(text)

    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t 　]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)
```

注意：`&nbsp;` 由 `html.unescape` 轉成 ` `，所以行內空白的 pattern 要含 ` `
（原程式是用 `.replace('&nbsp;', ' ')` 手動處理，改用 unescape 後要涵蓋這個字元）。

- [ ] **Step 4: 執行測試確認通過**

Run: `python test_system.py TestHealthETLPipeline.test_01_clean_html_cases -v`
Expected: PASS（含原有的案例 A、C、D）

- [ ] **Step 5: `scraper_tfc.py` 別在爬蟲端就壓掉段落**

第 89 行：

```python
raw_content = " ".join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20])
```

改為 `"\n".join([...])` —— 用換行接段落，讓 `clean_html` 與下游切分拿得到段落邊界。

- [ ] **Step 6: 跑全套並 Commit**

```bash
python test_system.py -v
git add utils.py scraper_tfc.py test_system.py
git commit -m "fix(etl): clean_html 改用 BeautifulSoup 並保留段落結構"
```

---

### Task 8: `chunk_text` 改句界切分

**Files:**
- Modify: `main_pipeline.py`（`chunk_text`）
- Test: `test_system.py`

**Interfaces:**
- Consumes: Task 7 保留下來的段落換行
- Produces: `chunk_text(text, max_chars=500, min_chars=30) -> list[str]`
  （簽名改變：移除 `overlap`，新增 `min_chars`）

- [ ] **Step 1: 寫失敗測試**

```python
    def test_09_chunking_respects_sentence_boundaries(self):
        """要求：不在句中切斷、不產生殘渣、不超過上限"""
        from main_pipeline import chunk_text

        text = "。".join([f"這是第{i}個句子" for i in range(1, 121)]) + "。"
        chunks = chunk_text(text, max_chars=500, min_chars=30)

        self.assertTrue(len(chunks) > 1, "測資應該長到需要切成多塊")
        for c in chunks:
            self.assertLessEqual(len(c), 500, "不得超過 max_chars")
            self.assertGreaterEqual(len(c), 30, "不得留下短於 min_chars 的殘渣")
            self.assertTrue(c.rstrip().endswith("。"),
                            f"每個 chunk 應結束在句界，而非切在句中：{c[-20:]!r}")
        self.assertEqual("".join(chunks), text, "切分不得遺失或重複任何字元")

    def test_10_chunking_merges_short_tail(self):
        """要求：不足 min_chars 的尾段併入前一塊，不獨立成 chunk"""
        from main_pipeline import chunk_text

        text = "很長的句子" * 90 + "。" + "短尾。"
        chunks = chunk_text(text, max_chars=500, min_chars=30)

        self.assertTrue(all(len(c) >= 30 for c in chunks),
                        f"出現短於 min_chars 的 chunk: {[len(c) for c in chunks]}")
        self.assertTrue(chunks[-1].endswith("短尾。"), "短尾應併入最後一塊")

    def test_11_chunking_handles_empty_and_short_input(self):
        from main_pipeline import chunk_text

        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text(None), [])
        self.assertEqual(chunk_text("短。"), ["短。"],
                         "整篇都短於 min_chars 時仍應回傳該內容，不得丟棄整篇")
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python test_system.py TestHealthETLPipeline.test_09_chunking_respects_sentence_boundaries -v`
Expected: FAIL — 現行實作按字元硬切，chunk 不會結束在句號

- [ ] **Step 3: 重寫 `chunk_text`**

```python
_SENT_SPLIT = re.compile(r"(?<=[。！？!?])")


def chunk_text(text: str, max_chars=500, min_chars=30) -> list:
    """依段落與句界切分，避免把句子從中間切開。

    原本的固定字元切分（500 字元、50 字元 overlap）會產生兩類問題：
    句子被切成兩半（下游的向量與精排都拿不到完整語意），以及尾段殘渣
    （實測產生過 127 筆 1 字元的 chunk）。

    overlap 在句界切分下不再需要——邊界落在句號上，相鄰 chunk 各自語意完整。
    """
    if not text:
        return []

    sentences = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        sentences.extend(s for s in _SENT_SPLIT.split(para) if s.strip())

    chunks, buf = [], ""
    for sent in sentences:
        if buf and len(buf) + len(sent) > max_chars:
            chunks.append(buf)
            buf = ""
        buf += sent
        while len(buf) > max_chars:      # 罕見的超長單句才退回硬切
            chunks.append(buf[:max_chars])
            buf = buf[max_chars:]

    if buf:
        if chunks and len(buf) < min_chars:
            chunks[-1] += buf            # 短尾併入前一塊，不獨立成殘渣
        else:
            chunks.append(buf)
    return chunks
```

檔頭需有 `import re`（原本沒有，要加）。

- [ ] **Step 4: 執行測試確認通過**

Run: `python test_system.py -v`
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
git add main_pipeline.py test_system.py
git commit -m "fix(etl): chunk_text 改依句界切分，消除斷句與殘渣"
```

---

### Task 9: 標題落地 `chunk_content` + `taskType`

**Files:**
- Modify: `main_pipeline.py`（`get_embedding`、`upload_to_mongodb`）
- Test: `test_system.py`

**Interfaces:**
- Consumes: Task 2 的 `upload_to_mongodb(..., embed_fn=...)`
- Produces: `get_embedding(text, *, task_type="RETRIEVAL_DOCUMENT")`；
  寫入的 `chunk_content` 改為含標題前綴的完整字串

- [ ] **Step 1: 寫失敗測試**

```python
    def test_12_chunk_content_matches_embedding_input(self):
        """要求：落地文本與送去向量化的文本必須是同一份字串"""
        from main_pipeline import upload_to_mongodb

        seen = []

        def recording_embed(text):
            seen.append(text)
            return [0.1, 0.2, 0.3]

        collection = FakeCollection()
        article = {
            "title": "標題X", "content": "內容一句。" * 60,
            "source": "來源", "url": "https://example.com/a",
        }

        upload_to_mongodb([article], collection, embed_fn=recording_embed)

        written = [d["chunk_content"] for d in collection.inserted_batches[0]]
        self.assertEqual(written, seen,
                         "chunk_content 必須與送去向量化的字串完全相同")
        self.assertTrue(all(t.startswith("主題：標題X\n內容：") for t in written),
                        "每個 chunk 都應帶標題前綴")

    def test_13_embedding_payload_declares_document_task_type(self):
        """要求：向量化必須指定 RETRIEVAL_DOCUMENT，不可留空"""
        import inspect
        from main_pipeline import get_embedding

        src = inspect.getsource(get_embedding)
        self.assertIn("RETRIEVAL_DOCUMENT", src,
                      "get_embedding 必須在 payload 指定 taskType")
        self.assertIn("taskType", src)
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python test_system.py TestHealthETLPipeline.test_12_chunk_content_matches_embedding_input -v`
Expected: FAIL — `chunk_content` 是不含標題的 chunk，與送去嵌入的字串不同

- [ ] **Step 3: `get_embedding` 加 `taskType`**

```python
def get_embedding(text: str, max_retries=3, *, task_type="RETRIEVAL_DOCUMENT") -> list:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={API_KEY}"
    payload = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
        # 不指定時 Gemini 預設等同 RETRIEVAL_QUERY（實測 cosine=1.000000），
        # 會讓文件與查詢落在同一個子空間，失去非對稱檢索的區辨力。
        "taskType": task_type,
    }
    ...  # 其餘重試邏輯不變
```

- [ ] **Step 4: `upload_to_mongodb` 讓兩者共用同一字串**

Task 2 版本裡已經是 `embed_fn(f"主題：{title}\n內容：{chunk}")`。
把該字串抽成變數，並讓 `chunk_content` 用同一個值：

```python
        contextual_chunks = [f"主題：{title}\n內容：{chunk}" for chunk in chunks]
        vectors = []
        failed = False
        for i, contextual in enumerate(contextual_chunks):
            vector = embed_fn(contextual)
            ...
        docs = [
            {
                ...
                "chunk_content": contextual,   # ← 與 embedding 輸入同一份
                ...
            }
            for i, (contextual, vector) in enumerate(zip(contextual_chunks, vectors))
        ]
```

- [ ] **Step 5: 跑全套並 Commit**

```bash
python test_system.py -v
git add main_pipeline.py test_system.py
git commit -m "feat(etl): 標題落地 chunk_content 並指定 RETRIEVAL_DOCUMENT"
```

---

### Task 10: 改用批次 embedding 端點

**Files:**
- Modify: `main_pipeline.py`（新增 `get_embeddings_batch`）
- Test: `test_system.py`

**Interfaces:**
- Consumes: 無
- Produces: `get_embeddings_batch(texts: list[str], *, task_type="RETRIEVAL_DOCUMENT") -> list[list[float]]`
  —— 回傳與輸入等長的向量列表；任一失敗時回傳 `[]`（讓 Task 2 的全有或全無邏輯接手）

- [ ] **Step 1: 寫失敗測試**

```python
    def test_14_batch_embedding_returns_one_vector_per_text(self):
        """要求：批次端點回傳數量必須與輸入一致，否則視為失敗"""
        from main_pipeline import parse_batch_embedding_response

        resp = {"embeddings": [{"values": [1, 2]}, {"values": [3, 4]}]}
        self.assertEqual(parse_batch_embedding_response(resp, expected=2),
                         [[1, 2], [3, 4]])

        # 數量不符 → 回傳空 list，交給呼叫端當成失敗處理
        self.assertEqual(parse_batch_embedding_response(resp, expected=3), [])
        self.assertEqual(parse_batch_embedding_response({"error": {"message": "x"}}, expected=2), [])
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python test_system.py TestHealthETLPipeline.test_14_batch_embedding_returns_one_vector_per_text -v`
Expected: FAIL — `ImportError: cannot import name 'parse_batch_embedding_response'`

- [ ] **Step 3: 實作純函式與批次呼叫**

先把回應解析抽成可測的純函式：

```python
def parse_batch_embedding_response(response_data: dict, *, expected: int) -> list:
    """解析 batchEmbedContents 的回應；數量不符或有 error 時回傳 []。

    數量不符必須當成失敗——若靜默接受短少的結果，會讓 chunk 與向量錯位，
    產生比缺漏更難察覺的資料損壞。
    """
    if not isinstance(response_data, dict) or "error" in response_data:
        return []
    embeddings = response_data.get("embeddings") or []
    vectors = [e.get("values") or [] for e in embeddings]
    if len(vectors) != expected or any(not v for v in vectors):
        return []
    return vectors
```

再寫批次呼叫（沿用既有的 429 退避邏輯）：

```python
def get_embeddings_batch(texts: list, max_retries=3, *, task_type="RETRIEVAL_DOCUMENT") -> list:
    if not texts:
        return []
    url = (f"https://generativelanguage.googleapis.com/v1beta/"
           f"models/gemini-embedding-001:batchEmbedContents?key={API_KEY}")
    payload = {
        "requests": [
            {
                "model": "models/gemini-embedding-001",
                "content": {"parts": [{"text": t}]},
                "taskType": task_type,
            }
            for t in texts
        ]
    }
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, timeout=60)
            data = response.json()
            if "error" in data:
                msg = data["error"].get("message", "未知錯誤")
                print(f"    [向量 API 錯誤] {msg}")
                if "Quota exceeded" in msg or "429" in str(msg):
                    print(f"    ⏳ 觸發 API 限制，等待 40 秒後重試... "
                          f"(第 {attempt+1}/{max_retries} 次)")
                    time.sleep(40)
                    continue
                return []
            return parse_batch_embedding_response(data, expected=len(texts))
        except Exception as e:
            print(f"    [向量例外錯誤] {e}")
            time.sleep(5)
    return []
```

**注意**：單篇文章的 chunk 數遠小於 API 的 batch 上限，故不需分批；
若日後遇到超長文章，再加分批邏輯。

- [ ] **Step 4: `upload_to_mongodb` 改用批次**

把 Task 9 的逐一迴圈換成一次呼叫，但**保留全有或全無語意**：

```python
        vectors = (embed_batch_fn or get_embeddings_batch)(contextual_chunks)
        if len(vectors) != len(contextual_chunks):
            print(f"    ⚠️ 向量化失敗——整篇跳過，留待下次執行重試")
            continue
```

`upload_to_mongodb` 的參數由 `embed_fn` 改為 `embed_batch_fn`；
**Task 2、3、9 的既有測試假件需同步更新為批次介面**（回傳 list of vectors），
在報告中逐一說明。

- [ ] **Step 5: 移除每塊 `sleep(2)` 並跑全套**

單篇改為一次呼叫後，原本每個 chunk `time.sleep(2)` 的節流不再需要。
保留 429 的 40 秒退避。

Run: `python test_system.py -v`

- [ ] **Step 6: Commit**

```bash
git add main_pipeline.py test_system.py
git commit -m "perf(etl): 改用 batchEmbedContents，單篇一次呼叫"
```

---

### Task 11: 遷移腳本 —— v2 collection + 雙來源回填

**Files:**
- Create: `migrate_rebuild.py`
- Test: `test_system.py`

**Interfaces:**
- Consumes: Task 7–10 的新清洗／切分／向量化
- Produces: `reconstruct_article_text(chunks: list[dict], overlap: int = 50) -> str`
  （純函式，可測）；以及 CLI `python migrate_rebuild.py --dry-run|--apply`

**背景**：HPA API 只回最新 1,000 筆，但線上庫有 910 個 HPA URL + 706 個食藥署標題，
其中一部分已掉出 API 視窗。因此重建需要兩個來源：
(a) 三個來源全量重跑新 ETL；(b) 舊庫中 API 已取不到的文章，從舊 chunk 重組原文再走新管線。
舊 chunk 可重組已驗證：`(url, chunk_index)` 排序後扣除固定 50 字元 overlap 即還原原文。

- [ ] **Step 1: 寫失敗測試**

```python
    def test_15_reconstruct_article_from_old_chunks(self):
        """要求：從舊 chunk 依 chunk_index 排序、扣除 overlap 還原原文"""
        from migrate_rebuild import reconstruct_article_text

        original = "".join(f"第{i:02d}段內容。" for i in range(1, 31))
        size, overlap = 50, 5
        raw, start = [], 0
        while start < len(original):
            raw.append(original[start:start + size])
            start += size - overlap

        chunks = [{"chunk_index": str(i + 1), "chunk_content": c}
                  for i, c in enumerate(raw)]
        self.assertEqual(reconstruct_article_text(chunks, overlap=overlap), original)

    def test_16_reconstruct_handles_unordered_and_string_index(self):
        from migrate_rebuild import reconstruct_article_text

        chunks = [
            {"chunk_index": "2", "chunk_content": "BBB"},
            {"chunk_index": "1", "chunk_content": "AAA"},
        ]
        self.assertEqual(reconstruct_article_text(chunks, overlap=0), "AAABBB")
        self.assertEqual(reconstruct_article_text([], overlap=0), "")
```

- [ ] **Step 2: 執行測試確認失敗**

Expected: `ModuleNotFoundError: No module named 'migrate_rebuild'`

- [ ] **Step 3: 實作 `migrate_rebuild.py`**

純函式部分：

```python
def reconstruct_article_text(chunks: list, overlap: int = 50) -> str:
    """把舊格式的 chunk 依 chunk_index 排序後還原成原文。

    舊 ETL 用固定 500 字元、50 字元 overlap 硬切，所以相鄰 chunk 的
    前 `overlap` 個字元與前一塊的結尾重複，扣掉即可無損還原。
    chunk_index 在舊資料裡是字串（'1'、'2'…），排序時要轉 int。
    """
    if not chunks:
        return ""
    ordered = sorted(chunks, key=lambda c: int(c.get("chunk_index") or 0))
    text = ordered[0].get("chunk_content") or ""
    for c in ordered[1:]:
        body = c.get("chunk_content") or ""
        text += body[overlap:] if overlap and len(body) > overlap else body
    return text
```

CLI 部分（`--dry-run` 為預設，`--apply` 才寫入）：
1. 連線舊 collection，取得所有 `(url, original_title)` 與其 chunk
2. 跑新 ETL 的三個爬蟲，得到「API 目前可取得」的文章集合
3. 差集 = 舊庫有、API 取不到 → 從舊 chunk 重組原文，組成與爬蟲相同格式的 article dict
   （`published_at` / `updated_at` 沿用舊文件的值，取不到則 `None`）
4. 兩批合流，一起走新的 `upload_to_mongodb` 寫入 `health_articles_chunks_v2`
5. 印出：新舊筆數、來源分佈、重組來源的篇數、失敗清單

**重組來的文章要標記**：寫入時加欄位 `rebuilt_from_chunks: True`，
讓下游能區分「原文重抓」與「舊 chunk 重組」（後者已失去段落結構，切分品質較低）。

- [ ] **Step 4: 執行測試確認通過**

Run: `python test_system.py -v`

- [ ] **Step 5: 分兩階段執行重建以取得歸因**

**這一步會呼叫真實 Gemini API 並寫入新 collection。先跑 dry-run。**

```bash
python migrate_rebuild.py --dry-run          # 確認筆數與來源分佈合理
python migrate_rebuild.py --apply            # 階段一：清洗 + 切分 + 標題落地
```

階段一完成後，請 CARE 端把 `MONGODB_COLLECTION` 指向 v2 跑一次 `scripts/rag_eval.py`，
記錄數字。接著把 `get_embedding` / `get_embeddings_batch` 的 `task_type` 改回
`RETRIEVAL_QUERY`（等同舊行為）再重建一次到 v3，比較兩者——
**這樣才能把「切分改善」與「taskType 修正」的貢獻分開**。

每次重建成本約 $0.30、數分鐘，值得為了歸因多跑一輪。

**若 dry-run 顯示的筆數與預期差距很大（例如重組來源超過三成），停下來回報**——
那代表 API 視窗比預期更窄，遷移策略需要重新評估。

- [ ] **Step 6: 記錄結果並 Commit**

把兩階段的 eval 數字寫進 `openspec/changes/etl-content-quality/design.md`，
**照實記錄**——變好、持平、變差都直接寫。

```bash
git add migrate_rebuild.py test_system.py openspec/changes/etl-content-quality/design.md
git commit -m "feat(migrate): 新增 v2 重建腳本與雙來源回填"
```

- [ ] **Step 7: 收尾批次 2**

勾選 `openspec/changes/etl-content-quality/tasks.md` 全部項目並 commit。

---

## 完成後

1. 兩個 change 的 `tasks.md` 全數勾選、`python test_system.py` 全綠
2. 開 PR 給 `Capoo0618/CARE-data`，PR 內文說明：批次 1 無下游影響可先合；
   批次 2 需與 CARE 協調 cutover
3. 合併後執行 `openspec archive etl-write-integrity` 與 `openspec archive etl-content-quality`
4. **CARE 端的三個配套改動**（另一個 repo，不在本計畫）：
   - `rerank_document_text` 防雙前綴（`chunk_content` 已含 `主題：`）
   - `scripts/rag_tighten_golden.py:102` 的 `re.sub(r"\s+", "", text)` 吃空白 bug
   - `MONGODB_COLLECTION` 切到 v2 並重跑 eval
5. 建議順帶（不在本計畫）：CI 目前只跑 `python main_pipeline.py`、**完全不跑測試**，
   可考慮加一個 PR 觸發的測試 job
