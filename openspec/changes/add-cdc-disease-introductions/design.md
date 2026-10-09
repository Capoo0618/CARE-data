# 設計與既有實作核對

## 現況

已讀取工作區指引、README、OpenSpec config／etl-ingestion 規格，以及
`main_pipeline.py`、`scraper_api.py`、`scraper_fda.py`、`scraper_mohw.py`、
`claim_tagger.py`、`vector_store.py`、`utils.py`、`test_system.py`。CARE-data 沒有
repo 專屬 AGENTS.md。主流程在 `452da8a`；README、註解與 config 有歷史描述，以程式
實際行為為準，並修正與本變更有關的資料契約／排程說明。

各 scraper 清洗後回傳文章 dict；`upload_to_mongodb` 統一切片、向量化與寫入。
Mongo 內文 `_id` 對應 PG 向量 `id`，PG upsert 使用交易，job 結尾 reconcile 清理孤兒
並同步判定。查核標籤只選取 `claim_tagger.SOURCES`；媒體另寫 `daily_health_news`。

## 抓取與解析

使用 requests、`get_ca_bundle()`、`make_soup()`，沿用 `scraper_mohw.with_retries`
及 headers。每次請求 25 秒逾時、0.4 秒節流；暫時失敗最多 3 次，退避 2／5 秒。
不新增依賴或第二套 HTTP 客戶端。

1. `Disease/Index` 的 `.infectious_disease_ul` 提供名稱及 `Disease/SubIndex` URL。
   以 URL 去掉分類／注音索引重複項；不使用歷史篇數作停止條件。
2. 每個疾病專頁只取 `.infectious_disease_box .disease .disease-heading` 中顯示文字
   為「疾病介紹」的卡片。禁止掃描整頁側欄，以免抓到其他疾病或新聞。
3. 只接受官方中文 `Category/Page` 明細，http／裸網域正規化至 `https://www.cdc.gov.tw`，
   只移除已列明追蹤 query／fragment，未知參數保留。外站、英文、附件與其他路徑排除。
4. 明細標題需為「疾病介紹」。標題 `.news-v3`、正文 `.m-t-30.m-b-30` 與 `.date`
   是同一 `.col-md-9` 下的兄弟節點。缺正文或錯誤版面時不以全頁文字補救。
5. 剝除 script／style 與控制項，保留正文段落、列表、表格文字；不更動共用
   `clean_html`，避免其他來源的清洗變動觸發向量不一致。

## 文章契約與增量更新

| 欄位 | 決策與理由 |
|---|---|
| `source` | `疾管署疾病介紹`，與闢謠來源分開，引用顯示實際機關 |
| `title` | `<疾病名稱>－疾病介紹`；避免 ETL 既有標題集合將所有介紹誤判為同一篇 |
| `url` | 官方介紹明細的正規 URL，既有 ETL 用此鍵比對與改版 |
| `content` | 官方正文純文字，包含介紹內的預防衛教，保留換行供既有 chunker 使用 |
| `published_at` | 文章日期區有明確標示才轉 ISO，否則 `None` |
| `updated_at` | 明確最後更新／更新日期轉 ISO，供既有改版判定；非法或缺漏為 `None` |
| 查核欄位 | 爬蟲不提供；既有入庫流程寫 `None`，不加入 tagger 白名單 |

啟用後每日重抓介紹，未改版不呼叫 embedding。2026-10-09 補強：缺更新日期時比較
清理後標題／正文 hash；有日期維持日期優先。URL 為新來源主要識別，同名不同 URL
不跳過，舊來源識別規則不變。每個 chunk 保存授權、署名、語言／地區及取得時間；
只對本次接觸的 CDC 補 metadata，不做全庫回填或全庫重算。開關預設 false。
不同疾病共用同一介紹 URL 時成功文章只收一次；首次抓取失敗不占用成功去重鍵。

## 失敗與操作

單疾病／明細失敗記錄網址，繼續其他疾病。零產出由主流程來源監測回傳 1，其餘來源
仍入庫；沿用既有切片全有或全無、配額停止、PG 失敗回復 Mongo 與結尾對帳契約。
404／解析失敗不視為下架，不刪庫。`test_mode` 最多 3 篇成功介紹，取得後停止抓取，
不需 Mongo／PG／Gemini 金鑰；完整 ETL 仍依現有 CronJob 部署流程執行。

2026-10-09 授權／robots、預覽、metadata 與來源限定入庫工具的完整操作規格見
[官方衛教接入](../../../docs/sources/health-education.md)及後續 `expand-health-education` change。
正式批次不使用完整 job()，避免帶入媒體、tagger 或全庫對帳刪除。

## 驗證

離線 fixtures 依 2026-10-08 官方 DOM 精簡並使用合成正文，覆蓋索引重複、入口限縮、
中文編碼、完整正文／預防章節、頁面雜訊、日期、空內容、重試、局部／整批失效。
注入假件串接真實 crawler → job → upload → reconcile，驗證兩邊 ID、衛教判定為空、
重跑不重算、改版只替換該篇，以及 tagger 查詢候選排除一般衛教。

動態驗證直接比對官方索引首筆與介紹正文首末段；和純離線測試分開，保留網路失效
訊號。完整測試採專案 Python 3.10，執行 `python test_system.py` 與 CI 的
`python -m unittest test_system`。正式資料庫寫入由假件驗證，不在開發時觸發全量 ETL。
