## 1. 寫入改為全有或全無，去重降到文章粒度

- [ ] 1.1 `main_pipeline.py`：`upload_to_mongodb` 改為先確認一篇文章的**所有** chunk 都成功取得向量後，才以單次批次寫入該篇；任一 chunk 向量化失敗時，該篇 0 個 chunk 被寫入，並保留在未寫入狀態以待下次執行重試
- [ ] 1.2 `main_pipeline.py`：去重判定粒度由「來源」改為「文章」——一次以 `collection.distinct()` 取回既有的 url／title 鍵值集合，取代逐篇 `find_one` 的 N 次資料庫往返；移除 `skipped_sources` 機制，單篇已存在不得再導致同來源後續文章被跳過
- [ ] 1.3 `upload_to_mongodb` 對外部呼叫（向量化）新增依賴注入介面，供測試以假件取代真實 API
- [ ] 1.4 測試（純函式／寫入邏輯單元測試——依賴注入假件，不得發出真實網路請求）：`test_system.py` 新增 `test_04_partial_embedding_failure_writes_nothing`、`test_05_all_chunks_succeed_writes_once`、`test_06_existing_article_does_not_skip_rest_of_source`

## 2. 存日期並依修改日期重寫改版文章

- [ ] 2.1 `scraper_api.py`：`get_api_articles` 回傳的文章 dict 新增 `published_at` / `updated_at` 兩個鍵；衛福部闢謠網站（HPA）來源填入 API 回應中的修改日期欄位，食藥署無對應欄位時填 `None`
- [ ] 2.2 `scraper_tfc.py`：回傳的文章 dict 同步補上 `published_at` / `updated_at`（皆為 `None`，該來源不提供修改日期），讓兩支爬蟲回傳的鍵集合一致
- [ ] 2.3 `main_pipeline.py`：`upload_to_mongodb` 寫入的文件新增 `published_at` / `updated_at` 欄位；當來源提供 `updated_at` 且與資料庫中既有值不同時，先刪除該篇既有的所有 chunk，再以新內容重新寫入
- [ ] 2.4 測試（純函式／寫入邏輯單元測試——依賴注入假件，不得發出真實網路請求）：`test_system.py` 新增 `test_07_updated_article_replaces_old_chunks`、`test_08_unchanged_article_is_skipped`

## 3. 移除 `verify=False`

- [ ] 3.1 `scraper_api.py:32`：移除 `requests.get` 呼叫中的 `verify=False`
- [ ] 3.2 `scraper_tfc.py:37`、`scraper_tfc.py:86`：移除兩處 `requests.get` 呼叫中的 `verify=False`
- [ ] 3.3 `test_system.py` 中用於比對來源網站原始資料的請求一併移除 `verify=False`；若移除後 `urllib3.disable_warnings(...)` 已無作用，一併清理
- [ ] 3.4 測試（對來源網站／API 的動態一致性驗證——刻意打真實網路，保留其線上特性）：既有 `test_02_api_data_integrity`、`test_03_tfc_data_integrity` 覆蓋；若任一來源出現 TLS 憑證錯誤須停下回報，不得把 `verify=False` 加回去

## 4. 刪除過時副本 `test_pipeline.py`

- [ ] 4.1 確認 `test_pipeline.py` 除自身外無任何引用（`grep` 全 repo 的 `.py`／`.yml`／`.md`）
- [ ] 4.2 刪除 `test_pipeline.py`——它是 `main_pipeline.py` 加入 early-stopping 之前的舊副本，內含各自一份不同步的 `chunk_text`／`get_embedding`／`upload_to_mongodb`，不含任何 assertion、`unittest` 不會收集它、CI 也未執行它

## 5. Definition of Done

- [ ] 5.1 `python test_system.py` 全綠（含 `test_02`／`test_03` 的線上一致性驗證，執行時需要網路連線）
- [ ] 5.2 有清楚的 git commit；本 change 對下游 CARE Backend 無影響（不改變既有欄位內容或向量，不需重建、不需 cutover）
