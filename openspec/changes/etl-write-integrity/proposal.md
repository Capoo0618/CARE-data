## Why

1. **部分寫入留下永久破洞**：`main_pipeline.py:87-88` 在某個 chunk 的 `get_embedding()` 失敗時只印警告並跳過該塊，其餘照常寫入。實例：線上 `pid=16703` 這篇 `total_chunks` 宣告 4，但庫中 `chunk_index` 只有 `[1, 3, 4]`。之後該文章會被 `find_one` 判定「已存在」而永遠跳過，破洞不會補。
2. **early stopping 會整批漏抓**：`main_pipeline.py:63-67` 一旦遇到單篇已存在，就 `skipped_sources.add(source_name)` 放棄該來源後續所有文章。若來源非嚴格時間排序、或中間某篇曾寫入失敗，後續新文章永遠補不回來。實測佐證：HPA API 回傳 1,000 筆，DB 只有 910 個 URL。

## What Changes

- 寫入改為全有或全無：整篇文章所有 chunk 向量化都成功才 `insert_many`，任一失敗整篇不寫、留待下次
- early stopping 改為集合差集（一次 `distinct` 取既有 url／title，逐篇比對），只跳過該篇不跳過整個來源
- 寫入 `published_at` / `updated_at`；HPA 有 `修改日期` 時據以判斷是否重寫該篇
- 移除三處 `verify=False`
- 刪除過時副本 `test_pipeline.py`
- **非 BREAKING**：不改變 `chunk_content` 內容與 embedding 產生方式，**既有向量完全不受影響**

## Capabilities

### New Capabilities

- `etl-ingestion`：定義知識庫寫入的完整性保證（全有或全無）、去重與更新判定、以及來源掃描策略。

### Modified Capabilities

- （無）

## Impact

- **程式**：`main_pipeline.py`、`scraper_api.py`、`scraper_tfc.py`
- **刪除**：`test_pipeline.py`（過時副本，見 `tasks.md` 第 4 節）
- **測試**：`test_system.py` 新增寫入邏輯的單元測試（對外部呼叫以依賴注入傳入假件，不打真實網路）
- **相依**：無新增套件
- **對下游 CARE Backend 的影響：無**——不改變任何既有欄位內容或向量，不需重建、不需 cutover。
