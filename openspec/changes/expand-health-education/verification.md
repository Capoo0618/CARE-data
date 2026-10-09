# 官方衛教首批正式驗證（2026-10-09）

## 範圍與授權

依使用者最新指示將官方來源加入現有 RAG；本輪允許正式 embedding／MongoDB／pgvector 寫入，不自動合併、永久部署或修改另一 repository。PMDA 條款禁止自動巡迴下載，保持停用、零入庫；不繞過。

完整收錄範圍、實測入口、授權／robots 與啟用操作見 [官方衛教接入](../../../docs/sources/health-education.md)。HPA 只代表目前首批選取的衛教，不宣稱四個主題或全站已全部遍歷。

## 程式與測試

- 分支：`feat/cdc-disease-education`；PR #10 的 scope 擴充為本次官方衛教接入。
- 程式提交 `ae49bb8`：來源接入、授權／robots 指紋、metadata、開關、URL／hash、只讀預覽及來源限定批次。
- 範圍修正提交 `70b5950`：排除 HPA 社區健走步道（nodeid=332）與個別場地路線；快取入庫亦拒絕，來源健康統計只取本批選取來源。
- Python 3.10.21；無新增依賴。
- `python test_system.py`：174 項通過（既有官方公開頁動態測試保留）。
- 最終 `python -m unittest test_system test_education test_education_ingest`：198 項通過，42.086 秒。
- 最後補強來源所有權：相同 URL 的其他來源切片不參與本來源的 metadata 更新、替換與讀回統計；寫入失敗只清理本篇新建的精確 `_id`。以混合來源假資料與 PG 失敗介面驗證。
- CI 對 `70b5950` 的 push／PR 測試皆通過；正式部署 job 跳過。
- 多輪唯讀審查已修正授權否定句、明確重用限制漏攔、醫師許可誤排及零產出假正常；最後審查無阻擋問題。

## 正式批次與修正紀錄

暫時 Job 使用現有 ETL 映像與 Secret 注入，掛載已審核程式及當日公開文章。只調用來源限定入庫工具，不調用完整 `job()`，不做媒體推播、政府查核標記或全庫對帳刪除。

第一個批次 `care-education-20261009095828` 使用 `ae49bb8`，原始抓取 HPA 209、ECDC 41、MHLW 4、CDC 96。實際運動分類包含公園／步道路線與交通資料；發現後立即 suspend 批次，將這些記錄改列排除，不把場地名錄當一般衛教。

只對本 Job 開始後新建、來源為國健署主題衛教、精確路線 URL／正文 hash／ObjectId 時間相符的記錄處理：先完整備份 Mongo 文件與 PG 向量，再下載核對 SHA256 與 ID；再次確認文件未改變後，精確清除本次誤收的 135 切片與 135 向量。讀回兩邊目標殘留均為 0，未刪除既有資料或其他來源。備份留在 ignored `backups/education-routes-2026-10-09.json.gz`，SHA256 `173af0378805c6d0995bcce6df4bbfa071fcfd19c5732c3129e4248ebd478c18`。

接續 Job `care-education-resume-20261009102022` 使用 `70b5950` 及已限縮的 HPA 99、ECDC 41、MHLW 4、CDC 96，呼叫預算 1500。已完成文章不重複向量化；來源政策／robots 在寫入前再次核對。

目前批次讀回已確認：

| 來源 | 文章 | Mongo 切片／PG 向量 | 查验 |
| --- | ---: | ---: | --- |
| 疾管署疾病介紹 | 96 | 524 | 7 個授權／來源 metadata 與正文 hash 已補齊，本輪未重算向量 |
| 國健署主題衛教 | 99 | 197 | 正文、metadata、切片順序／數量、PG ID／3072 維一致 |
| 厚生勞動省健康與疾病預防 | 4 | 111 | 同上；保留日文與日本適用地區 |
| ECDC 疾病衛教 | 41 | 1355 | 同上；保留英文／歐盟 EEA／CC BY 4.0 |
| PMDA | 0 | 0 | 禁止自動巡迴，未入庫 |

合計 240 篇、2187 個 Mongo 切片與 PG 向量，其中新來源 144 篇／1663 切片；CDC 本輪僅更新 metadata、沒有重新向量化。接續 Job 正常結束，各來源 remaining_urls 與 integrity_problems 均為空。

2026-10-09 10:52:56 UTC 再以獨立、唯讀後端連線讀回：逐篇重組正文 hash、檢查切片順序／数量、7 個 metadata、一般衛教 claim／verdict 為 None、PG ID／3072 維與 half vector。metadata_and_hash_problems 與 missing_vector_ids 均為空；HPA 場地記錄與 PMDA 記錄均為 0。

至少 1798 次成功文件 embedding（1663 保留切片 + 135 誤收後清除切片），另有 4 次檢索查詢 embedding；接續批次記錄 1352 次文件 embedding。中途停止時可能有未完成文章的呼叫，不能把成功切片數當完整 API 帳單。本次費用不能從無權限的 GCP 帳務帳戶確認；清除誤收資料不代表取消其向量化計費。

3 個暫時 Job、11 個公開資料／程式 ConfigMap 及後端測試向量快取已清除，讀回確認沒有本批資源殘留。正式 CronJob 與其他應用保持原部署。完整批次、獨立讀回、查詢與清理證據保存於 ignored `backups/education-expansion-2026-10-09.json`。

## 檢索與尚未驗證

四個中文查詢使用現有後端 embedding 設定預先向量化，後續實際 `HybridRetriever` 檢查重用同一組向量，不再呼叫模型。每題取 40 個候選，四題均召回指定來源；排名是候選召回排名，未執行最終回答生成／重排。

| 可直接測試的中文問題 | 首個對應來源候選排名 | 命中文章 |
| --- | ---: | --- |
| 依國健署的主題衛教，預防腦中風可以從哪些生活習慣做起？ | 7 | 腦血管疾病 |
| 外食族在早餐店要怎麼吃，才能符合我的餐盤均衡飲食？ | 1 | 均衡飲食菜單－早餐篇 |
| 根據 ECDC，肉毒桿菌病（botulism）的症狀與預防措施有哪些？ | 5 | Factsheet for health professionals about botulism |
| 依日本厚生勞動省，家庭食物中毒預防的三個原則是什麼？ | 1 | 家庭での食中毒予防 |

未執行完整 LINE 回覆／付費回答生成；後端需配合投影與顯示授權、加工聲明與外國適用地區，本輪只列需求，未修改後端 repository。正式 CronJob 尚未部署分支版本或啟用新來源開關；資料入庫與永久排程是不同狀態。
