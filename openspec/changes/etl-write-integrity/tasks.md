## 1. 寫入改為全有或全無，去重降到文章粒度

- [x] 1.1 `main_pipeline.py`：`upload_to_mongodb` 改為先確認一篇文章的**所有** chunk 都成功取得向量後，才以單次批次寫入該篇；任一 chunk 向量化失敗時，該篇 0 個 chunk 被寫入，並保留在未寫入狀態以待下次執行重試
- [x] 1.2 `main_pipeline.py`：去重判定粒度由「來源」改為「文章」——一次以 `collection.distinct()` 取回既有的 url／title 鍵值集合供去重比對（改版偵測與完整性檢查仍逐篇查詢，見 design.md 的 D2）；移除 `skipped_sources` 機制，單篇已存在不得再導致同來源後續文章被跳過
- [x] 1.3 `upload_to_mongodb` 對外部呼叫（向量化）新增依賴注入介面，供測試以假件取代真實 API
- [x] 1.4 測試（純函式／寫入邏輯單元測試——依賴注入假件，不得發出真實網路請求）：`test_system.py` 新增 `test_04_partial_embedding_failure_writes_nothing`、`test_05_all_chunks_succeed_writes_once`、`test_06_existing_article_does_not_skip_rest_of_source`

## 2. 存日期並依修改日期重寫改版文章

- [x] 2.1 `scraper_api.py`：`get_api_articles` 回傳的文章 dict 新增 `published_at` / `updated_at` 兩個鍵；衛福部闢謠網站（HPA）來源填入 API 回應中的修改日期欄位，食藥署無對應欄位時填 `None`
- [x] 2.2 `scraper_tfc.py`：回傳的文章 dict 同步補上 `published_at` / `updated_at`（皆為 `None`，該來源不提供修改日期），讓兩支爬蟲回傳的鍵集合一致
- [x] 2.3 `main_pipeline.py`：`upload_to_mongodb` 寫入的文件新增 `published_at` / `updated_at` 欄位；當來源提供 `updated_at` 且與資料庫中既有值不同時，先刪除該篇既有的所有 chunk，再以新內容重新寫入
- [x] 2.4 測試（純函式／寫入邏輯單元測試——依賴注入假件，不得發出真實網路請求）：`test_system.py` 新增 `test_08_updated_article_replaces_old_chunks`、`test_09_unchanged_article_is_skipped`

## 3. 以釘選中繼憑證的 CA bundle 取代 `verify=False`

- [x] 3.1 新增 `ca_bundle.py`：`get_ca_bundle()` 將 certifi 的根憑證庫與 `certs/*.pem`
  合併成一份暫存 CA bundle，per-process 快取、`atexit` 清理，暫存檔權限 0600
- [x] 3.2 新增 `certs/twca_secure_ssl_ca.pem`：衛福部伺服器未附的中繼憑證
  `TWCA Secure SSL Certification Authority`（公開資料，有效期至 2030-10-16）
- [x] 3.3 `scraper_api.py`、`scraper_tfc.py`、`test_system.py` 共五處呼叫移除
  `verify=False`，改傳 `verify=get_ca_bundle()`；`urllib3.disable_warnings(...)`
  一併清理
- [x] 3.4 `requirements.txt` 新增 `certifi>=2024.2.2`（刻意不釘死版本，見 design.md 的 D4）
- [x] 3.5 測試：新增 `test_11_ca_bundle_contains_pinned_intermediate`（離線，驗證釘選的
  中繼憑證確實被併入 bundle）；既有 `test_02`／`test_03` 的線上一致性驗證涵蓋實際連線

## 4. 刪除過時副本 `test_pipeline.py`

- [x] 4.1 確認 `test_pipeline.py` 除自身外無任何引用（`grep` 全 repo 的 `.py`／`.yml`／`.md`）
- [x] 4.2 刪除 `test_pipeline.py`——它是 `main_pipeline.py` 加入 early-stopping 之前的舊副本，內含各自一份不同步的 `chunk_text`／`get_embedding`／`upload_to_mongodb`，不含任何 assertion、`unittest` 不會收集它、CI 也未執行它

## 5. Definition of Done

- [x] 5.1 `python test_system.py` 全綠，共 33 個測試（含 `test_02`／`test_03` 的線上一致性驗證，執行時需要網路連線）
- [x] 5.2 有清楚的 git commit；本 change 對下游 CARE Backend 影響極小且不需 cutover——
  不改變 `chunk_content` 內容或 embedding 產生方式，既有向量完全有效；既有文件會新增
  `published_at` / `updated_at` 兩個欄位，並一次性重寫 71 篇既有的不完整文章（詳見 proposal.md）

## 6. 來源全滅與寫入失敗必須讓 CI 紅燈

- [x] 6.1 `main_pipeline.py`：新增 `EXPECTED_SOURCES`（三個來源的正式名稱）與純函式 `find_missing_sources(articles, expected=EXPECTED_SOURCES)`，回傳本次完全沒有產出任何文章的來源名稱集合；刻意只看「有沒有產出」而不看數量，避免文章數自然波動造成假警報
- [x] 6.2 `main_pipeline.py`：`job()` 簽名改變為回傳 0（成功）或 1（有來源全滅或 MongoDB 連線／寫入失敗）；`__main__` 區塊在 `GITHUB_ACTIONS=true` 的單次執行模式下 `sys.exit(job())`，讓非零退出碼使 CI 紅燈；常駐排程模式刻意不因單次失敗而終止程序
- [x] 6.3 測試（純函式測試，不得發出網路請求）：`test_system.py` 新增 `test_12_find_missing_sources`，涵蓋三個來源都有產出、單一來源全滅、完全沒有任何文章三種情境；既有 `test_02`／`test_03` 之線上一致性測試維持不變

## 7. 收尾修正（全分支 review 後）

- [x] 7.1 `main_pipeline.py`：既有文件沒有 `updated_at` 時只 `update_many` 補上日期欄位，
  不重新向量化（見 design.md 的 D6）
- [x] 7.2 `main_pipeline.py`：既有文章的實際切片數與宣告的 `total_chunks` 不符時重寫修復
  （見 design.md 的 D7）
- [x] 7.3 `main_pipeline.py`：`upload_to_mongodb` 每篇文章各自 `try`／`except`，
  單篇失敗只跳過該篇，並以回傳值 `(new_count, write_failed)` 讓 `job()` 退出碼為 1；
  `delete_many` 之後若 `insert_many` 失敗會明確指名該篇 URL
- [x] 7.4 `test_system.py` 的釘選憑證路徑改為以 `ca_bundle._PINNED_DIR` 組出，
  不再依賴從 repo 根目錄執行
- [x] 7.5 測試：新增既有資料補日期、破洞修復、單篇寫入失敗不中止整批、
  同批次重複文章只寫一次、改版空內容不刪舊版等情境

## 8. 對抗式驗證後的修正

- [x] 8.1 `main_pipeline.py`：`insert_many` 中途失敗時清除該篇殘留的切片
  （見 design.md 的 D8）
- [x] 8.2 `main_pipeline.py`：完整性檢查改為每次執行都跑，不再只在尚未補日期的
  文章上（見 design.md 的 D7）
- [x] 8.3 `main_pipeline.py`：「有嘗試但一篇都沒成功」時回報失敗，讓退出碼為 1
  （見 design.md 的 D9）
- [x] 8.4 `main_pipeline.py`：`existing_titles` 改用 `is not None` 判斷，
  空字串標題不再每次執行重複寫入；`scraper_api.py` 的守衛由 `and` 改為 `or`，
  標題或內容任一為空的記錄不進入管線
- [x] 8.5 測試：新增 `test_20_partial_insert_is_rolled_back`、
  `test_21_hole_is_repaired_even_after_updated_at_is_set`、
  `test_22_systematic_embedding_failure_is_reported`、
  `test_23_single_embedding_failure_does_not_fail_the_run`、
  `test_24_empty_title_article_is_written_at_most_once`

## 9. 突變測試與補上的覆蓋

- [x] 9.1 對整套測試做突變測試，逐一注入缺陷確認測試會紅（見 design.md 的 D10）
- [x] 9.2 `main_pipeline.py`：`job()` 新增 `fetchers` / `collection_factory` /
  `embed_fn` 三個依賴注入點，讓退出碼的判定能在無網路下驗證
- [x] 9.3 `main_pipeline.py`：`__main__` 的環境偵測抽成 `main(env, job_fn)`，
  把不可測的 wiring 縮到 `sys.exit(main())` 一行
- [x] 9.4 測試：新增 `test_25`（寫入文件的每個欄位）、`test_26`／`test_27`
  （批次內以 url／標題去重）、`test_28`（DB 端 url 集合真的有載入）、
  `test_29` 至 `test_32`（`job()` 的四種退出碼情境）、
  `test_33`（CI 模式把退出碼傳回作業系統）
- [x] 9.5 `test_11` 改為解碼 PEM 後比對 SHA-256 指紋，擋得住位元層級的竄改
