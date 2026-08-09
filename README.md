# CARE-data

**CARE-data** 是 **LINE 健康闢謠機器人** 的後台 ETL（Extract, Transform, Load）資料管線。

本專案採用 **Microservices（微服務）架構**，將對外提供即時服務的 LINE Bot 與耗時的資料蒐集、清洗、向量化流程完全解耦。

系統每日自動從政府公開 API 與台灣事實查核中心（TFC）取得最新健康闢謠文章，經過 NLP 切片、Gemini Embedding 向量化後，寫入 MongoDB，提供前端 Bot 作為 Retrieval-Augmented Generation（RAG）的知識庫。

---

# 系統特色

## Serverless 自動化 ETL

- 使用 GitHub Actions 建立 CI/CD 與排程流程
- 每日早上 **08:00（台灣時間）** 自動啟動 ETL
- 不需維護本地伺服器
- 可降低目標網站封鎖固定 IP 的風險

---

## Incremental Update（增量更新）

系統以**文章**為單位判斷是否需要處理：每次執行先一次取回知識庫中既有的 url 與標題集合，
再逐篇比對。已存在且未改版的文章直接跳過，不重複呼叫 Embedding API。

衛福部闢謠網站的 API 提供「修改日期」，文章改版時會以新版本取代既有內容；
食藥署與台灣事實查核中心沒有對應欄位，維持「已存在即跳過」。

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

ETL 在以下情況會以**非零狀態碼**結束，讓 GitHub Actions 顯示紅燈：

- 三個來源中有任一個本次完全沒有取得文章（爬蟲失效、來源改版、網路或憑證問題）
- 知識庫寫入階段失敗

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
│       └── etl_pipeline.yml      # GitHub Actions 排程
├── openspec/                     # 規格與進行中的變更（spec-driven 工作流程）
├── certs/                        # 釘選的中繼憑證（公開資料）
├── main_pipeline.py              # ETL 主流程
├── scraper_api.py                # 政府 API 爬蟲
├── scraper_tfc.py                # 台灣事實查核中心爬蟲
├── ca_bundle.py                  # TLS 憑證鏈：certifi 根憑證庫 + 釘選的中繼憑證
├── utils.py                      # 共用工具（HTML 清洗等）
├── test_system.py                # 單元測試與資料一致性驗證
├── requirements.txt              # Python 套件
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

無論本地開發或 GitHub Actions 部署，都需要設定以下環境變數。

| 變數 | 說明 |
|------|------|
| `GEMINI_API_KEY` | Google Gemini API 金鑰，用於文字向量化 |
| `MONGO_URI` | MongoDB Atlas 連線字串 |

本地可建立 `.env`：

```env
GEMINI_API_KEY=YOUR_API_KEY
MONGO_URI=mongodb+srv://<user>:<password>@cluster...
```

GitHub 部署請於：

```
Settings
→ Secrets and variables
→ Actions
```

新增相同名稱的 Secret。

---

# 本地開發

## 1. 安裝套件

```bash
pip install -r requirements.txt
```

---

## 2. 執行單元測試

建議每次修改程式後先執行測試。

```bash
python test_system.py
```

---

## 3. 執行 ETL

```bash
python main_pipeline.py
```

本地模式預設為常駐排程執行。

---

# 自動部署

本專案採用 GitHub Actions 自動部署。

只要將程式 Push 至 `main` 分支，即可自動更新。

## 定時執行

依照 `.github/workflows/etl_pipeline.yml` 設定：

- UTC：00:00
- 台灣時間（UTC+8）：08:00

每天自動執行一次 ETL。

---

## 手動執行

GitHub 專案頁面：

```
Actions
→ Daily Health ETL Pipeline
→ Run workflow
```

即可立即執行最新 ETL。

---

# 技術架構

- Python
- GitHub Actions
- MongoDB Atlas
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
