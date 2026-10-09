# 官方一般健康衛教接入

確認日期：2026-10-09。本文的篇數、切片數只在驗證紀錄中代表當次查驗，不是目前資料庫實況。

## 收錄與授權

| 來源 | 官方入口／範圍 | 適用條款 | 語言／地區 |
| --- | --- | --- | --- |
| 疾管署疾病介紹 | [傳染病索引](https://www.cdc.gov.tw/Disease/Index) → `Disease/SubIndex` → 疾病資訊區的中文疾病介紹 `Category/Page`；同頁症狀、傳播、預防文字 | [政府網站資料開放宣告](https://www.cdc.gov.tw/Category/FPage/TxkBIR9agw_IBRRmvn9TcQ)，不自行改標 CC BY／OGDL | zh-TW／臺灣 |
| 國健署主題衛教 | HTML 分類：[慢性病 nodeid=46](https://www.hpa.gov.tw/Pages/List.aspx?nodeid=46)、[營養 nodeid=36](https://www.hpa.gov.tw/Pages/List.aspx?nodeid=36)、[體能 nodeid=37](https://www.hpa.gov.tw/Pages/List.aspx?nodeid=37)、[銀髮族 nodeid=39](https://www.hpa.gov.tw/Pages/List.aspx?nodeid=39)；正文分類區內最多四層，`TopicList.aspx?idx=N&nodeid=...` 實際翻頁 | [本署宣告](https://www.hpa.gov.tw/Pages/Detail.aspx?nodeid=92&pid=5141) 明列[政府資料開放授權條款第1版](https://data.gov.tw/license)；不擷取特別限制／第三方素材 | zh-TW／臺灣 |
| ECDC 疾病衛教 | [Public health topics](https://www.ecdc.europa.eu/en/all-topics) → 一層疾病專區 → 官方導覽中的 `facts`／`factsheet`／`prevention-and-control` HTML；不擴及 surveillance／出版品／期刊 | [ECDC intellectual property notices](https://www.ecdc.europa.eu/en/ecdc-intellectual-property-notices) 的機構自有文字適用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)；須署名、連結授權及說明加工 | en／歐盟、歐洲經濟區 |
| 厚生勞動省健康與疾病預防 | 指定四頁：[家庭食中毒預防](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/shokuhin/syokuchu/01_00008.html)、[腸管出血性大腸菌 Q&A](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000177609.html)、[2026 冬季 ARI Q&A](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/kenkou/kekkaku-kansenshou/infulenza/QA2026.html)、[身體活動／運動](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/kenkou/undou/index.html)；不擴抓整個政策站 | [機關利用規約](https://www.mhlw.go.jp/chosakuken/index.html) 採[公共データ利用規約 PDL 1.0](https://www.digital.go.jp/resources/open_data/public_data_license_v1.0)。已核對[排除別紙](https://www.mhlw.go.jp/chosakuken/exhibit.html)，不收標誌、勞動委員會資料及其他另有條款內容 | ja／日本 |
| PMDA 用藥安全衛教 | 候選為機構自有[用藥 Q&A](https://www.pmda.go.jp/safety/consultation-for-patients/on-drugs/qa/0014.html)；**停用、不入庫** | [網站政策](https://www.pmda.go.jp/0048.html) 禁止自動巡迴下載；不能用一般重用授權推導出自動抓取許可。需先確認允許的官方取得方式或提供文件，再評估指定欄目；藥廠仿單、RMP、患者藥品指南等權利未確認文件排除 | ja／日本，尚無入庫資料 |

官方開放條款並不代表機關個別核准、背書或認可 CARE。外國原文保留原語言，不自動翻譯或把外國疫苗／疾病指引標成臺灣規範。

每輪先讀 robots.txt，再確認授權正文的已審核 SHA256。快照為 `tests/fixtures/cdc/license.html` 和 `tests/fixtures/education/*-policy.html`；只去除排版空白，正文變更即停止，需人工審閱後才可更新指紋。不能以「關鍵字仍存在」判斷授權有效。HPA 指紋只取條款容器，排除文章上下則導覽；MHLW 的排除別紙亦每輪核對完整正文指紋（`mhlw-annex.html`）。

robots 實測限制：CDC `/Uploads/`、`/LogFiles/`、`/TTS/`、`/File/`；HPA `/File`、`/Pages/ashx/File.ashx`、`/Pages/ashx/GetFile.ashx`；ECDC 管理、搜尋、登入、oembed、core／profiles 等；MHLW `/cgi-bin/`、`/images/` 及指定藥品回收路徑。上述收錄入口未被禁止。PMDA robots 回 404，但網站政策已禁止自動巡迴，不以缺少 robots 當作可抓取。

所有來源排除行政／新聞列表、個資、第三方或另行限制素材、PDF／Office 附件、圖片 OCR、影音與外站；純導覽／附件且缺正文不以整頁文字補救。連結只走既定分類區，站外與非法路徑在發請求前拒絕，redirect 每跳再檢查 robots／白名單。遇到 403 或封鎖不繞過。

## 資料與增量規則

每個 MongoDB `CARE_database.health_articles_chunks` chunk 都保存 `content_type=health_education`、`language`、`jurisdiction`、`license`、`license_url`、`attribution`、UTC `retrieved_at` 和正文 `content_hash`；pgvector 保持既有 3072 維、ID 對應 Mongo `_id`。`claim`／`verdict`／`verdict_slug` 固定 None，來源不加入政府查核標記器。

有可信更新日先依日期判定；缺更新日則比較清理後標題／正文 SHA256。抓取時間、署名與授權等 metadata 不參與雜湊，更新這些欄位不消耗 embedding。舊 CDC 缺 hash 時從實際完整切片重建基準，不拿新正文的 hash 貼給舊內容。日期未變但正文變動的既有日期優先行為保持不變。

新來源以正規化 URL 判重，同名不同 URL 都可收錄。只移除列明的 `utm_*`、`fbclid`、`gclid` 追蹤鍵與 fragment，未知 query 保留。同 URL 已由其他來源保存時保留原資料，不改標來源。其他既有來源的識別／更新規則不變，也不執行全庫 metadata 回填或全庫重算。

既有資料只具備原有 HTML／日期等處理，並非已有這套授權 metadata。先前 2026-10-08 的 CDC 入庫紀錄亦不代表新欄位已補齊，需以本輪讀回驗證確認。

## 預覽、啟用與分批操作

預覽永遠不匯入 DB／embedding 模組，不需 env，不消耗模型 API 額度：

```bash
uv run python scraper_cdc.py --preview --limit 2
uv run python scraper_education.py --source hpa --limit 3
uv run python scraper_education.py --source ecdc --limit 3
uv run python scraper_education.py --source mhlw --limit 3
uv run python scraper_education.py --source pmda --limit 1  # blocked，exit 1，零 HTTP
uv run python ingest_education.py --source ecdc --limit 3 --output /tmp/ecdc-preview.json
```

输出包含標題、URL、長度、日期、授權／署名與成功／排除／失敗統計。單來源預覽最多五篇。CDC 25 秒 timeout、0.4 秒間隔；其他新來源沿用 25 秒 timeout、0.5 秒間隔。HTTP 重試最多三次、2／5 秒退避；本輪最多 500 個請求（含重試與 redirect）、最多 200 篇。已到合法 offset 末端可回正常無待辦；本輪全部失敗／排除不能假裝成功。

排程接入開關預設都為 false：`CDC_DISEASE_ENABLED`、`HPA_THEME_ENABLED`、`ECDC_EDUCATION_ENABLED`、`MHLW_EDUCATION_ENABLED`。正式驗證與部署後才在既有 CronJob 設為 true；只有啟用來源參與健康檢查。`PMDA_EDUCATION_ENABLED` 不會突破停用限制。

每個開關前綴有 `_MAX_ARTICLES`（1..200）及 `_START_AT`（非負）設定，例如 `HPA_THEME_MAX_ARTICLES=20`、`HPA_THEME_START_AT=0`。使用預覽／批次報告 `next_offset` 分批探索；HPA／ECDC 的 offset 為探索順序中候選文章位置，CDC 為疾病位置。目錄可能變動，定期從 0 增量重抓可補中間失敗的文章；`limited=true` 表示尚未走完受限目錄，不可宣稱全站完整。

HPA 同時收錄分類頁本身 `.RLintro .htmlBlock` 的有效衛教介紹；只含導覽不當文章。ECDC 純疾病導覽卡片不當正文，僅再跟隨實測的四個指定明細白名單（Ebola factsheet／Q&A、流感個人預防／抗病毒治療），深度最多三層；病媒昆蟲目錄不擴展成整個昆蟲資料庫。

**以下正式指令只在已授權的 care-vm 叢集批次環境使用**，會消耗 embedding 並寫入 Mongo／PG。所需 `MONGO_URI`、`GEMINI_API_KEY`、`PGVECTOR_SYNC_DSN` 使用現有 Secret 注入，不能把秘密貼进 JSON 或文件：

```bash
uv run python ingest_education.py --source hpa --source ecdc --source mhlw --source cdc \
  --write --limit 200 --max-embedding-calls 800 --output /tmp/education-report.json
# 用工具產生的 24 小時內公開文章 JSON 接續入庫；寫入前仍確認最新 robots／授權
uv run python ingest_education.py --input /tmp/education-report.json --source ecdc \
  --write --limit 200 --max-embedding-calls 200 --output /tmp/ecdc-ingestion.json
```

此工具直接沿用 `upload_to_mongodb`，不呼叫完整 `job()`，不執行媒體、查核標記或全庫對帳刪除。保留既有全篇向量化成功才入庫契約；預算／API 配額耗盡時未完成文章不保存，報告列出剩餘 URL，下次重跑已完成文章不再向量化。讀回驗證包含本文、chunk index／count、必要 metadata 與 PG ID／3072 維向量；寫入／一致性失敗退出非零，不宣稱已完成。

停用來源將对应 `*_ENABLED=false`，不刪已入庫文字或向量。尚未合併／部署的分支程式不會自動改變正式排程。

## 後端引用需求與驗證

Mongo／PG retriever 的 metadata 投影需保留上述欄位；引用／分享畫面需顯示機構、原文 URL、授權 URL，以及「CARE 擷取、清洗、切片／整理」等加工說明。ECDC 必須提供 CC BY 4.0 連結。外國指引須標明日本或歐盟／EEA 適用範圍，回答臺灣規範時優先核對臺灣來源。此任務只列需求，不修改 CARE Backend repository；端到端引用顯示需另外驗證。

```bash
uv run python test_system.py
uv run python -m unittest test_system test_education test_education_ingest
```

離線測試用 HTML fixtures、假的 DB／embedding；既有來源動態公開頁驗證保留。實際入庫與檢索結果另記於 OpenSpec change 的驗證紀錄；未完成正式查驗不得用離線通過代替。
