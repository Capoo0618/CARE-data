"""合併 certifi 根憑證與手動釘選的中繼憑證，供 requests 的 verify= 使用。

為什麼需要這個檔案：衛福部（www.hpa.gov.tw）的伺服器只送出 leaf 憑證、
沒有附上中繼憑證 TWCA Secure SSL Certification Authority。瀏覽器與
macOS 的 curl 會依 leaf 憑證的 AIA 欄位自動補抓，但 Python 的 ssl 模組不會，
因此 requests 會拋 SSLError。

以往的做法是 verify=False——那等於對所有來源關閉憑證驗證，中間人攻擊
完全無法偵測。這裡改為「信任原本那批根憑證，外加 TWCA 這一張中繼憑證」，
驗證仍然有效。

釘選的憑證是公開資料（任何人連上衛福部都能取得同一張），存於
certs/twca_secure_ssl_ca.pem，有效期至 2030-10-16。到期後衛福部會連不上
並拋出明確的 SSLError——這是刻意的：大聲失敗遠優於 verify=False 那種
默默什麼都不檢查。屆時重新從 leaf 憑證的 AIA 網址下載新的一張即可。

另一個失效情境：若未來的 certifi 移除了 TWCA Global Root CA，
單靠這裡釘選的中繼憑證救不回來——Python 預設不設 X509_V_FLAG_PARTIAL_CHAIN，
憑證鏈必須終止於自簽根憑證。這同樣會讓連線失敗、被 Task 6 的來源檢查抓到。
"""

import atexit
import os
import tempfile

import certifi

_PINNED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
_bundle_path = None


def get_ca_bundle() -> str:
    """回傳合併後 CA bundle 的檔案路徑（同一程序內只建立一次）。"""
    global _bundle_path
    if _bundle_path and os.path.exists(_bundle_path):
        return _bundle_path

    # certifi 的內容原封不動保留（含開頭可能有的空白行），確保它仍是 bundle 的
    # 逐字子字串；只對我們自己附加的釘選憑證做 strip，避免多餘空白造成 PEM 解析問題。
    with open(certifi.where(), encoding="utf-8") as fh:
        certifi_content = fh.read()

    pinned_parts = []
    for name in sorted(os.listdir(_PINNED_DIR)):
        if name.endswith(".pem"):
            with open(os.path.join(_PINNED_DIR, name), encoding="utf-8") as fh:
                pinned_parts.append(fh.read().strip() + "\n")

    fd, path = tempfile.mkstemp(prefix="care_data_ca_", suffix=".pem")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(certifi_content)
        if not certifi_content.endswith("\n"):
            fh.write("\n")
        fh.write("\n".join(pinned_parts))

    atexit.register(lambda: os.path.exists(path) and os.unlink(path))
    _bundle_path = path
    return path
