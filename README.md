# CARE-data

**CARE-data** 是 **LINE 健康闢謠機器人** 的後台 ETL（Extract, Transform, Load）資料管線。

本專案採用 **Microservices（微服務）架構**，將對外提供即時服務的 LINE Bot 與耗時的資料蒐集、清洗、向量化流程完全解耦。

系統每日自動從政府公開 API 與台灣事實查核中心（TFC）取得最新健康闢謠文章，經過 NLP 切片、Gemini Embedding 向量化後，內文寫入 MongoDB、向量寫入 care-vm 上的 pgvector，提供前端 Bot 作為 Retrieval-Augmented Generation（RAG）的知識庫。

## 資料來源

| 來源名稱 | 模組 | 取得方式 | 有文章網址 | 可偵測改版 | 有判定標籤 | 上游狀態 |
|---|---|---|---|---|---|---|
| 食藥署闢謠專區 | `scraper_fda.py` | 爬 `news.aspx?cid=5049` | ✅ | ✅ 維護日期 | ❌ | ⚠️ 實質停更 |
| 食藥署公告 | `scraper_api.py` | `DataAction` API | ❌ 上游未提供 | ❌ | ❌ | ✅ 正常 |
| 國健署新聞 | `scraper_api.py` | `newsapi.ashx` API | ✅ | ✅ 修改日期 | ❌ | ⚠️ 只回最近 1000 筆 |
| 台灣事實查核中心 | `scraper_tfc.py` | 爬網頁列表 | ✅ | ✅ JSON-LD | ✅ **五分類** | ✅ 正常 |
| 衛福部真相說明 | `scraper_mohw.py` | 爬 `lp-4343-1.html` 彙整頁 | ✅ | ❌ | ❌ | ✅ 正常 |
| 國健署真相與闢謠 | `scraper_mohw.py` | 同上，連結指向 `hpa.gov.tw` | ✅ | ❌ | ❌ | ✅ 正常 |
| 疾管署闢謠專區 | `scraper_mohw.py` | 同上，連結指向 `cdc.gov.tw` | ✅ | ❌ | ❌ | ⚠️ 停在 110 年 |

`scraper_mohw.py` 爬的是衛福部「真相說明」——一個**跨機關的彙整頁**，本身不放
內文，每一列連到發布機關自己的網站。因此它一支爬蟲產出三個來源名：來源名必須
與實際發布機關一致，全部標成「衛福部」就是引用錯機關，而使用者點進連結會看到
別的網域。

2026-09-09 實測全部 54 頁共 1,023 筆的分派：

| 網域 | 筆數 | 處理 |
|---|---|---|
| `mohw.gov.tw/cp-4343-*` | 445 | 解析 → 衛福部真相說明 |
| `hpa.gov.tw/Pages/Detail.aspx` | 341 | 解析 → 國健署真相與闢謠 |
| `fda.gov.tw` | 199 | **排除**：與 `scraper_fda` 重複，且 url 形式不同（`http://` + 小寫 `/tc/` + utm 參數）會讓 ETL 的去重失效而存兩份 |
| `cdc.gov.tw/Bulletin/Detail/*` | 24 | 解析 → 疾管署闢謠專區 |
| `nhi.gov.tw` | 14 | **排除**：健保署站台對程式化請求回 403（WAF），帶完整瀏覽器 headers 重試仍是 403 |

未知網域一律**排除並記 log**，不是靜默跳過——站方日後新增轉載機關時，log 是
唯一會讓我們知道的訊號。（`nhi.gov.tw` 就是這樣被發現的：原始設計的網域表只列
了 4 個網域共 1,009 筆，實測是 5 個網域共 1,023 筆。）

「疾管署闢謠專區」**不列入** `EXPECTED_SOURCES`：那 24 筆全部是 110 年 COVID
時期的舊文，抓不到時不值得讓整條 ETL 以非零狀態碼結束。

**來源名必須與實際發布機關一致。** 這個欄位會出現在 CARE 回覆的參考來源清單與
分享給家人的消息卡上，是收件人唯一能自行查證的東西。已經踩過兩次：`DataAction`
被標成「食藥署闢謠專區」（2026-08-16 修正），`hpa.gov.tw` 被標成「衛福部闢謠網站」
（2026-09-09 修正——它是**國民健康署**，衛福部本部是 `mohw.gov.tw`）。

### 上游狀態的兩個已知限制（2026-09-09 實測）

**食藥署闢謠專區實質停更。** 2022 年 22 篇、2023 年 31 篇、2024 年 23 篇，
2025 年只有 3 篇、2026 年只有 1 篇（`2026-07-16`，站上最新一篇）。爬蟲本身正常
（列表頁 200、詳細頁解析無誤），是這個專區沒人維護了。既有 587 篇仍有檢索價值，
但不能再指望它供應新內容。

**國健署 `newsapi.ashx` 只回最近 1000 筆，且不接受任何分頁參數。**
`top` / `rows` / `count` / `pageSize` / `page` / `pn` 六個常見參數全部試過，一律
回同樣的 1000 筆、最舊都停在 `2021-08-16`。它是滾動窗口不是全量：每進一篇新的
就掉一篇舊的。DB 裡目前有 1,013 篇，多出來的 13 篇已經滑出窗口、只存在資料庫裡。

**兩者合起來的後果：`health_articles_chunks` 無法從來源完整重建。** 知識庫若重灌，
國健署那批只救得回當時窗口內的 1000 筆，而且不會有任何錯誤訊息——ETL 會照常
「成功」，只是內容變少。備份見 [`scripts/backup_knowledge_base.sh`](scripts/backup_knowledge_base.sh)。

## 判定標籤（僅 TFC）

TFC 是四個來源裡唯一本來就在做查核的——其餘三個是政府機關發布衛教資訊與新聞稿。
因此 TFC 的文章額外帶三個欄位，寫入每一個 chunk：

| 欄位 | 說明 |
|---|---|
| `verdict` | `錯誤`／`部分錯誤`／`正確`／`事實釐清`／`證據不足` |
| `verdict_slug` | 上述的機器可讀識別碼（`incorrect`、`partially-incorrect`…） |
| `claim` | 被查核的主張本身，即「網傳『⋯』？」那一句 |

判定取自頁面上的分類連結 **slug** 而非顯示文字——slug 在站方調整文案時不會變。
分類定義以 [TFC 官方查核指標說明](https://tfc-taiwan.org.tw/fact-checking-indicators-explanation/)
為準。其他三個來源的這三個欄位恆為 `None`。

「食藥署闢謠專區」與「食藥署公告」是**兩批完全不重疊**的資料，不要混為一談：

- **闢謠專區**才是真正的闢謠內容（網傳／是真的嗎），每篇都有可點的網址。
- **公告**是食藥署全站新聞稿 feed。2026-08-16 之前它被誤標成「食藥署闢謠專區」，
  但 706 篇裡只有 6 篇標題含「謠」字，約兩成是法規預告、研討會、表揚典禮這類
  行政公告（現已於來源端過濾，見 `scraper_api.ADMIN_NOISE_KEYWORDS`）。
  保留它是因為其中確實有用藥安全、藥品保存與丟棄等衛教內容。
  已知限制：該 API 結構上不提供文章網址，所以答案中無法附上可查證的連結。

---

# 系統特色

## 在 care-vm 上每天自動執行

- CARE-infra 的 `care-etl` CronJob 每天 **05:00（台灣時間）** 啟動一次
- 2026-09-22 之前跑在 GitHub Actions；搬進叢集是因為向量要直接寫叢集內的
  pgvector（ClusterIP，叢集外連不到），理由見 `vector_store.py`
- 每次執行結尾會把 PG 與 Mongo 對帳：刪掉找不到內文的孤兒向量、把 Mongo 的
  判定同步進 PG（查核比對是在 PG 篩判定的）

---

## Incremental Update（增量更新）

系統以**文章**為單位判斷是否需要處理：每次執行先一次取回知識庫中既有的 url 與標題集合，
再逐篇比對。已存在且未改版的文章直接跳過，不重複呼叫 Embedding API。

國健署新聞的 API 提供「修改日期」、食藥署闢謠專區的詳細頁提供「維護日期」，
文章改版時會以新版本取代既有內容；食藥署公告與台灣事實查核中心沒有對應欄位，
維持「已存在即跳過」。

> 早期版本採用 Early Stopping（遇到第一篇已存在的文章就停止該來源的後續爬取）。
> 這個做法在來源列表不是嚴格依時間排序、或中間某篇曾寫入失敗時，會讓後續的新文章
> 永遠補不回來，因此已改為上述的逐篇比對。

---

## Data Integrity Test

提供完整的 `unittest` 測試，包含：

- HTML 清洗邊界測試
- 文章內容解析測試
- 動態一致性驗證

系統可即時比對來源網站與爬取結果，確保資料維持一致。

---

## 失敗必須可見

ETL 在以下情況會以**非零狀態碼**結束，讓 CronJob 的 Job 標成失敗：

- 四個來源中有任一個本次完全沒有取得文章（爬蟲失效、來源改版、網路或憑證問題）
- 知識庫寫入階段失敗（Mongo 或 pgvector 任一邊）
- 向量庫對帳失敗，或孤兒超過 PG 的一半而拒絕刪除（多半是 Mongo 查錯了）

資料面仍然盡力而為：單一來源或單篇文章失敗不會阻擋其餘資料寫入，
只是該次執行會被標記為失敗。本機常駐排程模式不因單次失敗終止程序。

---

## Gemini API Rate Limit Handling

當 Gemini API 回傳 **HTTP 429**（Rate Limit）時，系統會：

- 自動偵測限制
- 啟動 Cooldown Backoff
- 延遲後重新送出請求

提升大量向量化時的穩定性與成功率。

---

# 專案架構

```text
CARE-data/
├── .github/
│   └── workflows/
│       └── test.yml              # 單元測試（ETL 本身由 care-etl CronJob 執行）
├── openspec/                     # 規格與進行中的變更（spec-driven 工作流程）
├── certs/                        # 釘選的中繼憑證（公開資料，見 certs/README.md）
├── migrations/                   # 一次性資料遷移腳本（用過即成為歷史紀錄）
├── main_pipeline.py              # ETL 主流程
├── vector_store.py               # pgvector 寫入與 PG／Mongo 對帳
├── claim_tagger.py               # 替政府闢謠文章補 claim／verdict
├── Dockerfile                    # care-etl 映像（CARE-infra 的 cicd build）
├── scraper_api.py                # 政府 API 爬蟲（食藥署公告、衛福部）
├── scraper_fda.py                # 食藥署闢謠專區網頁爬蟲
├── scraper_tfc.py                # 台灣事實查核中心爬蟲
├── ca_bundle.py                  # TLS 憑證鏈：certifi 根憑證庫 + 釘選的中繼憑證
├── utils.py                      # 共用工具（HTML 清洗等）
├── test_system.py                # 單元測試與資料一致性驗證
├── pyproject.toml                # Python 套件（uv 管理）
├── uv.lock                       # 相依鎖定（含傳遞相依，須進版控）
├── .gitignore
└── README.md
```

---

# ETL 流程

```text
政府 API / TFC
        │
        ▼
    爬蟲取得文章
        │
        ▼
    HTML 清洗
        │
        ▼
    NLP 切片
        │
        ▼
 Gemini Embedding
        │
        ▼
    MongoDB
        │
        ▼
LINE Bot (RAG)
```

---

# 環境變數

| 變數 | 說明 |
|------|------|
| `GEMINI_API_KEY` | Google Gemini API 金鑰，用於文字向量化 |
| `MONGO_URI` | MongoDB Atlas 連線字串 |
| `PGVECTOR_SYNC_DSN` | pgvector 的寫入帳號（`care_sync`）。沒設 ETL 會直接失敗，不會退回把向量寫進 Atlas |
| `ETL_RUN_ONCE` | 設 `1` 為單次執行（CronJob 用）；不設則是本機常駐排程 |

叢集上這些值來自 `care-backend-secret`（由 CARE-infra 的 cicd 從 GitHub Secrets 建立），
`MONGO_URI` 對應其中的 `MONGODB_URI`。

本地可建立 `.env`：

```env
GEMINI_API_KEY=YOUR_API_KEY
MONGO_URI=mongodb+srv://<user>:<password>@cluster...
```


---

# 本地開發

## 1. 安裝套件

相依由 [uv](https://docs.astral.sh/uv/) 管理，會照 `.python-version` 自備 CPython 3.10：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # 首次：安裝 uv（或 brew install uv）
uv sync                                            # 建 .venv 並照 uv.lock 安裝
```

---

## 2. 執行單元測試

建議每次修改程式後先執行測試。

```bash
uv run python test_system.py
```

---

## 3. 執行 ETL

```bash
uv run python main_pipeline.py
```

本地模式預設為常駐排程執行。注意 pgvector 在叢集內（ClusterIP），本機連不到，
所以本機只能跑測試；要實際寫入就到叢集手動觸發 CronJob（見下方）。

---

# 自動部署

推上 `main` 後，`trigger-deploy.yml` 通知 CARE-infra 重新 build `yanagi0912/care-etl`
映像並部署；CronJob 下一次執行就是新版。

## 定時執行

`care-etl` CronJob（CARE-infra `helm/care/templates/etl-cronjob.yaml`），每天
台灣時間 05:00。

## 手動執行

在 care-vm 上：

```bash
kubectl create job -n care-dev --from=cronjob/care-etl care-etl-manual-$(date +%s)
kubectl logs -n care-dev -f job/<上面建立的名稱>
```

---

# 技術架構

- Python
- Kubernetes CronJob（K3s on care-vm）
- MongoDB Atlas（內文、BM25）
- PostgreSQL + pgvector（向量）
- Google Gemini Embedding API
- BeautifulSoup
- Requests
- Selenium
- unittest

---

# 系統定位

本專案專注於 **資料蒐集（ETL）**，負責：

- 爬取健康闢謠資料
- 清洗與格式化內容
- NLP 文字切片
- 向量化（Embedding）
- 寫入 MongoDB

前端 **LINE Bot** 則負責：

- 使用者互動
- RAG 檢索
- Gemini 回答生成

兩者透過 MongoDB 完全解耦，可獨立部署與維護。
