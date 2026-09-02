# 衛福部「真相說明」爬蟲，並修正國健署的來源名誤標

## Why

### 知識庫缺了一整個闢謠專區

衛福部本部的「真相說明」（`https://www.mohw.gov.tw/lp-4343-1.html`）是跨機關的闢謠彙整頁，共 1,009 筆、52 頁。它從 102 年累積至今，是台灣官方闢謠內容最完整的單一入口。CARE-data 完全沒有爬它。

**2026-09-02 實測全部 52 頁（零失敗）的組成：**

| 連結指向 | 筆數 | 佔比 | 是否已在知識庫 |
| --- | --- | --- | --- |
| 衛福部本部 `cp-4343-*` | 445 | 44.1% | 否 |
| 國民健康署 `Detail.aspx?nodeid=127/126` | 341 | 33.8% | **否** |
| 食藥署 `newsContent.aspx?cid=5049` | 199 | 19.7% | 是（`scraper_fda`） |
| 疾病管制署 `Bulletin/Detail/*` | 24 | 2.4% | 否 |

**810 / 1,009（80.3%）是知識庫沒有的闢謠內容。**

### 國健署那 341 筆與既有來源零重疊——這點與直覺相反

`scraper_api` 已經在爬 `hpa.gov.tw/wf/newsapi.ashx`，直覺上真相說明裡的國健署連結應該是重複的。實測不是：

```
HPA API 回傳的 nodeid 分佈  → 4705(217) 4576(212) 4878(209) 4809(205) 5020(86)
真相說明的國健署 nodeid     → 127(333)  126(8)
341 個 pid 與 API 的 1000 個 pid 取交集 → 0 筆（0.0%）
```

`nodeid=127` 是國健署網站的「真相與闢謠」專區，`newsapi.ashx` 回的是別的專區。兩者是不同的內容集合。

### 食藥署那 199 筆雖然重複，但跟著爬會產生重複文件

真相說明上的食藥署連結長這樣：

```
http://www.fda.gov.tw/tc/newsContent.aspx?cid=5049&id=31601&utm_source=rss&utm_medium=...
```

`http://`、小寫 `/tc/`、帶 utm 參數。而 `scraper_fda` 產生的是 `https://www.fda.gov.tw/TC/newsContent.aspx?cid=5049&id=31601`。ETL 的去重以 `url` 字串為鍵，兩者不相等——跟著這些連結爬會讓同一篇文章在知識庫裡存兩份。

### 附帶：`hpa.gov.tw` 被標成「衛福部闢謠網站」是誤標

`scraper_api.py:45` 把 `https://www.hpa.gov.tw/wf/newsapi.ashx` 的 `source_name` 設為「衛福部闢謠網站」。`hpa.gov.tw` 是**國民健康署**，衛福部的下屬機關，不是本部（本部是 `mohw.gov.tw`）。

這與 2026-08-16 修掉的「`DataAction` 被誤標成食藥署闢謠專區」是同一類錯誤：來源名與實際端點對不上。本次新增真正的衛福部本部來源後，兩個名稱會直接衝突，必須一併修正。

## What Changes

**新增 `scraper_mohw.py`**，爬「真相說明」列表，並依連結指向的網域分派給三個明細頁解析器：

- `mohw.gov.tw/cp-4343-*` → 標題取 `h2`，內文取 `article`
- `hpa.gov.tw/Pages/Detail.aspx` → 標題取 `h3`，內文取 `.htmlBlock`
- `cdc.gov.tw/Bulletin/Detail/*` → 解析器待實作時確認

**SHALL NOT 跟隨 `fda.gov.tw` 的連結**（見上方理由），於列表階段即排除。

**來源名**依實際發布機關分為三個，而非全部標成「衛福部真相說明」——`source_name` 是使用者在回覆裡看到的機構名，標錯就是引用錯機關：

- `衛福部真相說明`
- `國健署真相與闢謠`
- `疾管署闢謠專區`

**修改**：
- `scraper_api.py`：`衛福部闢謠網站` → `國健署新聞`
- `main_pipeline.py`：`EXPECTED_SOURCES` 更新，並註冊新爬蟲
- `test_system.py`：新增對應測試
- `README.md`：資料來源表更新
- `migrations/`：既有文件的 `source_name` 改名腳本

## Impact

**下游契約**：`source_name` 是 CARE Backend 讀取的欄位之一。改名會讓既有文件與新文件的來源名不一致，因此需要 migration 一次改完。`chunk_content` 與 `embedding` 不受影響，**既有向量不失效、不需要重建**。

**執行時間**：新增約 810 篇文章的首次全量抓取。以每頁 0.7 秒節流估算，列表 52 頁約 36 秒，明細 810 篇約 9.5 分鐘。之後每日增量僅需列表翻頁（新文章出現在前幾頁）。

**額度**：810 篇 × 平均 chunk 數會消耗 Gemini embedding 額度。`main_pipeline` 既有的額度耗盡處理（區分每日額度與短暫超速）已能涵蓋，但首次執行很可能需要分數日完成。

**測試計畫**：
- `test_system.py` 追加 `MohwScraperTests`——列表解析、網域分派、fda 連結排除、日期正規化、404 確認重試
- 既有的動態一致性驗證比照 `scraper_fda` 的做法，對真實站台驗證選擇器仍有效

## 不在本次範圍

- 國健署 `newsapi.ashx` 既有內容的正確性（本次只改它的來源名）
- 疾管署站台的其他專區（本次只跟隨真相說明頁上出現的 24 筆）
