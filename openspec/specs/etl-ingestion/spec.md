# etl-ingestion Specification

## Purpose
TBD - created by archiving change etl-write-integrity. Update Purpose after archive.
## Requirements
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

### Requirement: 來源失效必須可見

當任一資料來源在一次執行中完全沒有產出任何文章，或知識庫寫入階段失敗時，系統 SHALL 以非零狀態碼結束該次執行（CI 單次執行模式），SHALL NOT 以成功狀態結束。常駐排程模式 SHALL NOT 因單次失敗而終止程序。

#### Scenario: 單一來源完全失效

- **WHEN** 三個來源中有一個在本次執行未產出任何文章
- **THEN** 其餘來源的文章仍照常寫入知識庫，且該次執行以非零狀態碼結束並指名該來源

#### Scenario: 寫入階段失敗

- **WHEN** MongoDB 連線或寫入拋出例外
- **THEN** 該次執行以非零狀態碼結束

### Requirement: 寫入中途失敗不得留下殘骸

當一篇文章的寫入在中途失敗（`insert_many` 預設 `ordered=True`，已寫入的文件不會回滾）時，系統 SHALL 清除該篇殘留的所有 chunk，使該篇於知識庫中不存在，並於下次執行以全新文章重新寫入。系統 SHALL NOT 讓宣告的 `total_chunks` 與實際筆數不符的文件留在知識庫中。

系統 SHALL 於每次執行檢查既有文章的實際 chunk 數是否與其宣告的 `total_chunks` 相符，不符者 SHALL 重寫修復。此檢查 SHALL NOT 僅在文章尚未具備 `updated_at` 時進行。

#### Scenario: 寫入寫到一半失敗

- **WHEN** 某篇文章切出 3 個 chunk，`insert_many` 寫入前 2 筆後拋出例外
- **THEN** 該篇在知識庫中的 chunk 數為 0，且該次執行以非零狀態碼結束

#### Scenario: 已具備修改日期的破洞仍會被修復

- **WHEN** 某篇文章已有 `updated_at`，且其實際 chunk 數少於宣告的 `total_chunks`
- **THEN** 系統重寫該篇，且重寫後 `total_chunks` 與實際筆數一致

### Requirement: 系統性向量化失敗必須可見

當一次執行中有文章進入向量化階段，但沒有任何一篇成功寫入時，系統 SHALL 以非零狀態碼結束。單篇文章的向量化失敗 SHALL NOT 單獨導致該次執行失敗。

#### Scenario: 向量化配額用盡

- **WHEN** 本次有 3 篇文章進入向量化階段，且全部因取得向量失敗而未寫入
- **THEN** 該次執行以非零狀態碼結束

#### Scenario: 單篇偶發失敗

- **WHEN** 本次有 2 篇文章進入向量化階段，其中 1 篇失敗、1 篇成功寫入
- **THEN** 該次執行不因此失敗，失敗的那篇留待下次執行重試

