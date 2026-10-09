## ADDED Requirements

### Requirement: 疾管署疾病介紹探索與正文收錄

系統 SHALL 從疾管署官方傳染病索引探索疾病專頁，只跟隨疾病資訊區的中文疾病介紹
入口，收錄正文及其中的預防衛教。系統 SHALL NOT 收錄側欄、頁尾、頁面控制項、
新聞、統計、Q&A、教材、PDF／圖片附件或外站。缺少有效正文時 SHALL 記錄並跳過。

#### Scenario: 分類與注音索引重複

- **WHEN** 同一疾病 URL 在多個索引分類出現
- **THEN** 系統只處理一次，文章引用 URL 指向官方介紹明細

#### Scenario: 介紹頁同時包含導航與預防方法

- **WHEN** 疾病介紹含有症狀、預防方法與分享控制項
- **THEN** 症狀與預防方法留在 content，導航、控制項與頁尾不進入 content

### Requirement: 一般衛教來源與查核資料分開

系統 SHALL 以「疾管署疾病介紹」標示來源，標題 SHALL 包含疾病名稱，使不同疾病
不會因共用「疾病介紹」標題被去重。系統 SHALL 沿用 health_articles_chunks 的
Mongo 內文與 PG 向量契約，SHALL NOT 寫入 daily_health_news 或加入查核標籤候選。

#### Scenario: 兩個疾病各有介紹

- **WHEN** 索引包含兩個不同疾病及其介紹明細
- **THEN** 兩篇均入庫，source_name 為疾管署疾病介紹，PG id 與 Mongo _id 對應
- **AND** claim、verdict、verdict_slug 為 None，不成為 claim_tagger 候選

### Requirement: 明確日期與既有增量機制

系統 SHALL 僅解析文章日期區明確標示的發布／更新日期並正規化為 YYYY-MM-DD。
缺漏或非法日期 SHALL 為 None，SHALL NOT 以抓取日期、更新日或頁尾日期推測發布日。
系統 SHALL 沿用既有更新日期、切法版本與切片完整性判定，不重建其他來源向量。

#### Scenario: 日期未改變的重跑

- **WHEN** 已存在介紹的更新日期、切法版本與切片數皆未改變
- **THEN** 系統跳過介紹，不呼叫 embedding、不新增重複切片

#### Scenario: 疾病介紹更新

- **WHEN** 已存在介紹的明確更新日期改變
- **THEN** 新版所有切片成功向量化後依既有流程替換該篇，清除舊向量
- **AND** 不影響其他疾病與其他來源的內文或向量

### Requirement: 疾病介紹來源失效可見

系統 SHALL 在 CDC_DISEASE_ENABLED 啟用時將疾管署疾病介紹納入來源健康檢查，預設停用。
只有合法批次 offset 已到末端可視為正常無待辦；全部失敗或被排除不豁免。單疾病失敗 SHALL 記錄並繼續，
來源零產出 SHALL 讓 ETL 回傳非零退出碼，其他來源仍照常寫入。

#### Scenario: 闢謠有資料但疾病介紹零產出

- **WHEN** 本次取得疾管署闢謠專區資料，但疾病介紹沒有任何有效文章
- **THEN** 系統仍報疾管署疾病介紹失效，不能以闢謠資料替代其健康檢查
- **AND** 其餘來源仍正常入庫

### Requirement: 衛教 metadata 與缺日期更新

每個 Mongo chunk SHALL 保存 content_type、language、jurisdiction、license、license_url、
attribution、retrieved_at；CDC 缺更新日期時 SHALL 比較清理後標題／正文 content_hash。
舊資料缺欄位 SHALL 保持相容，SHALL NOT 觸發全庫回填或其他來源重算。

#### Scenario: 只有取得時間改變

- **WHEN** URL 與正文未變，只有 retrieved_at 改變
- **THEN** 更新 metadata 而不再次 embedding 或新增切片
