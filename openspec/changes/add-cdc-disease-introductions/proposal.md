# 一般健康衛教資料來源擴充：疾管署疾病介紹

## 動機

現有官方來源以闢謠、新聞稿為主。第一階段加入疾管署官方疾病介紹及介紹正文中的
預防衛教，讓 CARE 的一般健康問答可檢索並引用官方原文。

## 變更

- 新增 `scraper_cdc.py`，每日從傳染病索引探索疾病專頁及中文疾病介紹。
- 來源名稱固定為「疾管署疾病介紹」，以疾病名稱組合標題並保留官方介紹 URL。
- 接入 `job()` 與 `EXPECTED_SOURCES`；零產出回報失敗，其餘來源仍寫入。
- 增加離線解析與入庫測試、官方來源動態驗證、README 操作說明。
- 修正測試直接執行入口的位置，讓 README 與 CI 指令都涵蓋所有測試類別。

## 下游影響與範圍

沿用 `CARE_database.health_articles_chunks` 與 PostgreSQL `health_articles_chunks`。
既有來源的 `chunk_content` 清洗、chunker 與 embedding 輸入不變；新來源新增自己的
正文切片。不改 schema、向量維度、查核白名單或 CARE Backend；不使既有 embedding
失效，不需全量重建、資料遷移或 cutover。

一般衛教不提供 `claim`／`verdict`／`verdict_slug`，入庫為 `None`。「疾管署闢謠專區」
仍由 `scraper_mohw.py` 負責；`daily_health_news` 不在本次範圍。第一階段不擴抓疾病
新聞、Q&A、疫情統計、教材、PDF／圖片或外站內容。

程式開發測試使用假件。後續依使用者指示完成單篇與全來源正式入庫，實測結果見
[入庫驗證](ingestion-verification.md)。歷史文件的篇數不視為目前庫況；首次新增介紹
使用既有 embedding 額度，額度耗盡時依原流程分日接續。
