# 實作與驗證計畫

- [x] 修正 CDC 授權否定句與明確禁止重製漏攔：先紅後綠回歸測試，授權正文完整指紋比對。
- [x] 新增受限來源設定與 HTTP 存取：官方入口、robots、授權快照、手動檢查 redirect、有限重試／節流／請求預算。
- [x] 新增 HPA／ECDC／MHLW 解析與目錄流程、PMDA 停用狀態；純 HTML fixture 驗證正文、日期、語言／地區、第三方排除。
- [x] 主流程來源開關、URL 識別、日期／雜湊與 metadata 保存；舊來源維持原行為，以假 DB／embedding 驗證重跑和更新。
- [x] 新增 preview CLI 及來源限定的分批匯入／讀回驗證；preview 不匯入 DB 或 embedding。
- [x] 執行 python test_system.py 與新增測試，官方小量 smoke test；文件完整列出入口、範圍、授權與操作。
- [ ] 經測試與審查後在 care-vm 執行來源限定批次；讀回正文、metadata、chunk 數、PG id／維度，測試中文跨語言檢索。
- [ ] 提交分支／PR 與各來源實際入庫紀錄；不自動合併、部署或刪除既有資料。
