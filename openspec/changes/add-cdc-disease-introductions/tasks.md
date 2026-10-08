# 實作與驗證

- [x] 核對 README、OpenSpec 與指定程式的最新版本，列出修改範圍。
- [x] 新增 `scraper_cdc.py` 與精簡 HTML fixtures；先執行失敗測試再實作。
  - `TestCDCDiseaseScraper.test_index_discovers_unique_official_diseases_only`
  - `test_follows_only_the_intro_card_not_sidebar_or_news`
  - `test_detail_keeps_prevention_and_excludes_page_controls`
  - `test_explicit_dates_are_normalized_and_never_guessed`
  - `test_missing_or_empty_body_and_wrong_page_are_rejected`
  - `test_retryable_http_failure_is_retried`
  - 其餘 crawl／失效／共用 URL／test_mode 案例。
- [x] 接入 job 與 EXPECTED_SOURCES，維持 Mongo／pgvector、查核及媒體資料邊界。
  - `TestCDCDiseaseIntegration.test_missing_disease_source_is_reported_separately_from_debunk`
  - `test_missing_cdc_turns_job_red_but_other_sources_still_write`
  - `test_cdc_crawl_flows_through_job_into_both_stores`
  - `test_repeat_and_update_reuse_existing_incremental_contract`
  - `test_general_education_never_becomes_a_claim_tagger_candidate`
- [x] 移動 test_system 直接執行入口至所有類別之後，補來源 fixture。
- [x] 更新 README、OpenSpec config 與本變更 proposal／design／delta spec。
- [x] Python 3.10 全套 `python test_system.py` 與 `python -m unittest test_system` 通過，
  含 `TestCDCDiseaseLive.test_live_index_and_first_three_introductions` 動態驗證。
- [x] 完成唯讀程式審查：核對官方 DOM、既有資料契約與新增測試，無重要問題。
- [ ] commit、推送 feature branch 與建立 PR；合併後由維護者 archive 本變更。

## 驗證紀錄（2026-10-08）

- Python 3.10.21，uv sync --locked，無新增依賴。
- 離線 CDC：19 項通過；與既有基準的 119 項合計，加入 CDC 線上驗證後全套 139 項。
- `python -m unittest test_system -v`：139 項通過，41.188 秒。
- `python test_system.py`：139 項通過，36.981 秒。
- 第一輪完整驗證的既有 `test_02_api_data_integrity` 曾因國健署 DNS 暫時解析失敗而
  拋 StopIteration；未修改／略過該測試，後續上述兩輪完整驗證皆通過。
- `python scraper_cdc.py`：官方前三篇成功，無抓取／解析失敗；另核對登革熱官方
  HTML，正文含預防方法。網站索引數量不是正式資料庫篇數。
- compileall 與 git diff --check 通過。正式 Mongo／PG／Gemini 未讀寫。
