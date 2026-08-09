# certs/

這裡放的是**公開的中繼憑證**，不是機密。任何一次 TLS 連線都會傳送這類憑證，
放進 repo 不涉及任何私鑰或憑證資料。

## 為什麼需要這個目錄

衛福部闢謠網站（`hpa.gov.tw`）的伺服器在 TLS 交握時只送出自己的伺服器憑證，
沒有附上簽發它的中繼憑證。瀏覽器與 curl 會依憑證裡的 AIA
（Authority Information Access）欄位自動去下載缺少的那一段，
但 Python 的 `ssl` 模組**不做 AIA 補抓**，憑證鏈就斷在中繼這層、驗證失敗。

`ca_bundle.py` 會在執行時把這裡的每一個 `.pem` 與 certifi 的根憑證庫合併成
一份完整的 CA bundle。驗證仍然完整進行，只是補上了伺服器沒送的那一段。

詳細取捨見 `openspec/changes/etl-write-integrity/design.md` 的 D4。

## 目前釘選的憑證

| | |
| --- | --- |
| 檔案 | `twca_secure_ssl_ca.pem` |
| Subject | `C=TW, O=TAIWAN-CA, CN=TWCA Secure SSL Certification Authority` |
| Issuer | `C=TW, O=TAIWAN-CA, OU=Root CA, CN=TWCA Global Root CA`（certifi 有收錄） |
| 有效期至 | **2030-10-16** |
| SHA-256 | `1A:2C:75:FD:09:6E:04:99:E9:FF:6A:C7:4E:52:6F:61:EA:AE:3E:DF:C8:C2:EA:44:36:FE:E0:C2:4D:8B:7D:0E` |

驗證手上的檔案是否就是這一張：

```bash
openssl x509 -in certs/twca_secure_ssl_ca.pem -noout -subject -issuer -enddate -fingerprint -sha256
```

## 到期或需要更換時

到期後衛福部會連不上，該來源本次一篇文章都抓不到，於是
`find_missing_sources` 會指名它、該次執行以非零狀態碼結束——**失敗是響亮的**。
`test_11_ca_bundle_contains_pinned_intermediate` 也會跟著失敗。

更換步驟：

1. 從伺服器實際的憑證鏈取得新的中繼憑證：

   ```bash
   openssl s_client -connect www.hpa.gov.tw:443 -showcerts </dev/null 2>/dev/null
   ```

   若伺服器仍然沒送出中繼憑證（本專案存在的原因），改由伺服器憑證的 AIA
   欄位取得下載網址：

   ```bash
   openssl s_client -connect www.hpa.gov.tw:443 </dev/null 2>/dev/null \
     | openssl x509 -noout -text | grep -A2 "Authority Information Access"
   ```

   也可以直接到 TAIWAN-CA 的官方憑證下載頁取得。

2. 確認新憑證的 Issuer 仍是 certifi 收錄的根憑證（目前是 `TWCA Global Root CA`）。
   若不是，單靠釘選中繼憑證救不回來——Python 預設不設
   `X509_V_FLAG_PARTIAL_CHAIN`，憑證鏈必須終止於自簽根憑證。

3. 覆蓋 `twca_secure_ssl_ca.pem`，更新本檔案上表的有效期與 SHA-256，
   然後跑 `python test_system.py`。
