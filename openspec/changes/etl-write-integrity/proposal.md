## Why

1. **部分寫入留下永久破洞**：`main_pipeline.py:87-88` 在某個 chunk 的 `get_embedding()` 失敗時只印警告並跳過該塊，其餘照常寫入。實例：線上 `pid=16703` 這篇 `total_chunks` 宣告 4，但庫中 `chunk_index` 只有 `[1, 3, 4]`。之後該文章會被 `find_one` 判定「已存在」而永遠跳過，破洞不會補。
2. **early stopping 會整批漏抓**：`main_pipeline.py:63-67` 一旦遇到單篇已存在，就 `skipped_sources.add(source_name)` 放棄該來源後續所有文章。若來源非嚴格時間排序、或中間某篇曾寫入失敗，後續新文章永遠補不回來。實測佐證：HPA API 回傳 1,000 筆，DB 只有 910 個 URL。

## What Changes

- 寫入改為全有或全無：整篇文章所有 chunk 向量化都成功才 `insert_many`，任一失敗整篇不寫、留待下次
- early stopping 改為集合差集（一次 `distinct` 取既有 url／title，逐篇比對），只跳過該篇不跳過整個來源
- 寫入 `published_at` / `updated_at`；HPA 有 `修改日期` 時據以判斷是否重寫該篇
- 恢復 TLS 憑證驗證：五處呼叫的 `verify=False` 全部移除，改用 `ca_bundle.py` 組出的
  CA bundle（certifi 根憑證庫 + 釘選的 TWCA 中繼憑證）。衛福部伺服器未附中繼憑證，
  瀏覽器與 curl 會自動補抓（AIA），Python 的 `ssl` 不會——見 design.md 的 D4
- 刪除過時副本 `test_pipeline.py`
- 來源全滅或知識庫寫入失敗時以非零狀態碼結束，讓 CI 紅燈（見 design.md 的 D5）；
  資料面維持 fail-open——單一來源或單篇文章失敗不會阻擋其餘資料寫入
- **完整性自癒**：既有文章的實際切片數與宣告的 `total_chunks` 不符時重寫該篇，
  每次執行都檢查。線上實測 71 篇、遺失 141 個切片，全部是舊版逐塊寫入留下的破洞；
  寫入中途失敗留下的殘留切片也會被清除（見 design.md 的 D7、D8）
- **非 BREAKING**：不改變 `chunk_content` 內容與 embedding 產生方式，**既有向量完全不受影響**

## Capabilities

### New Capabilities

- `etl-ingestion`：定義知識庫寫入的完整性保證（全有或全無）、去重與更新判定、以及來源掃描策略。

### Modified Capabilities

- （無）

## Impact

- **程式**：`main_pipeline.py`、`scraper_api.py`、`scraper_tfc.py`，新增 `ca_bundle.py` 與
  `certs/twca_secure_ssl_ca.pem`
- **刪除**：`test_pipeline.py`（過時副本，見 `tasks.md` 第 4 節）
- **測試**：`test_system.py` 新增寫入邏輯的單元測試（對外部呼叫以依賴注入傳入假件，不打真實網路），
  共 24 個測試
- **相依**：新增 `certifi>=2024.2.2`（提供根憑證庫給 D4 的 CA bundle）。
  刻意不釘死版本——釘死等於凍結根憑證庫，對安全性反而更糟
- **對下游 CARE Backend 的影響：極小且不需 cutover**
  - 不改變 `chunk_content` 的內容，也不改變 embedding 的產生方式，**既有向量完全有效**
  - 衛福部的既有文件會新增 `published_at` / `updated_at` 兩個欄位（目前 4,339 筆全部沒有）。
    食藥署與台灣事實查核中心的來源不提供日期，那 1,499 個切片不會有這兩個欄位。
    CARE Backend 不讀這兩個欄位，新增欄位不影響任何查詢
  - 合併後首次執行會重寫既有的不完整文章（見 What Changes 的「一次性修復」）。
    線上實測有 71 篇，其中 **70 篇**在本次 HPA API 回傳的 1,000 筆視窗內、會被修復；
    1 篇（`pid=16219`）已經退出該視窗，這個機制補不到它。
    宣告切片數合計 260、預估 10 分鐘。其餘完好的文章只補日期欄位、不重新向量化
