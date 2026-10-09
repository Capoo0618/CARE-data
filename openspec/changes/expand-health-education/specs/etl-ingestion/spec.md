## ADDED Requirements

### Requirement: 受限官方一般衛教來源

系統 SHALL 沿用文章 dict、既有切片、Mongo／pgvector 契約接入國健署主題衛教、ECDC 自有疾病／預防文字及厚生勞動省指定欄目。來源 SHALL 與新聞／闢謠分開，保留原語言與適用地區。來源入口、正文 selector 及翻頁 SHALL 依官方實測文件記錄；SHALL NOT 無限遍歷全站或收錄行政、附件、影音、OCR、外站、個資與權利未確認第三方素材。

#### Scenario: PMDA 禁止自動巡迴下載

- **WHEN** 使用者選取 PMDA，但機構網站條款禁止自動巡迴下載
- **THEN** 系統回報 blocked、零 HTTP、零 embedding／入庫，不以更換工具或身分繞過

### Requirement: 授權、robots 與失敗訊號

系統 SHALL 在每輪請求前核對 robots；每次 redirect SHALL 再核對網域／路徑／robots。授權正文與已審核完整指紋不一致 SHALL 停抓；MHLW 排除別紙 SHALL 同樣核對。權利限制 SHALL 有重用上下文，不得把醫师許可或藥物使用禁忌當成文章重用限制。

#### Scenario: 授權否定允許條款

- **WHEN** 條款仍有「重製、改作」關鍵字，但增加不得／禁止
- **THEN** 完整正文指紋不符，停止該來源，不取得文章

#### Scenario: 全篇排除與合法 offset 末端

- **WHEN** 本輪實際嘗試候選，全部因權利／正文範圍排除
- **THEN** 零產出不視為正常無待辦，CLI／來源健康檢查非零
- **WHEN** 已核對目錄但起始 offset 超過所有候選，未嘗試文章
- **THEN** 可回報正常無待辦

### Requirement: 中繼資料、識別與正式批次

一般衛教每個 chunk SHALL 保存授權、署名、語言、地區、取得時間及正文 hash，查核三欄 SHALL 為 None。新來源以正規化 URL 識別，同名不同 URL 不誤删；有可信更新日期優先用日期，缺日期比較穩定正文 hash，不影響既有來源規則。

#### Scenario: 預覽與正式入庫邊界

- **WHEN** CLI 未指定 --write
- **THEN** 只讀公開文章，不載入 DB／embedding 依賴，單來源最多五篇
- **WHEN** 使用已授權 --write 正式批次
- **THEN** 重驗授權／robots、快取期限及必要 metadata，限制 embedding 呼叫預算，全篇成功才寫入
- **AND** 只讀查驗該批 Mongo／PG ID、正文、metadata、向量維度，不执行媒體、tagger、全庫對帳刪除
