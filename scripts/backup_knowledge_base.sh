#!/usr/bin/env bash
# 備份知識庫 collection。
#
# 為什麼需要這個檔案：`health_articles_chunks` 的四個上游來源裡，有兩個已經
# **無法從來源重建**——
#
#   1. 國健署 `newsapi.ashx` 硬性只回最近 1000 筆，且不接受任何分頁參數
#      （2026-09-09 實測 top/rows/count/pageSize/page/pn 六個常見參數，
#      全部回 1000 筆、最舊都停在 2021-08-16）。DB 裡目前有 1,013 筆，
#      多出來的 13 筆已經滑出窗口，只存在這個資料庫裡。窗口每天前進，
#      差距只會擴大。
#   2. 食藥署闢謠專區實質停更（2024 年 23 篇 → 2025 年 3 篇 → 2026 年 1 篇），
#      站上文章雖然還在，但那個專區已經沒人維護，不能假設它會一直掛著。
#
# 也就是說：這個 collection 一旦沒了，**不是重跑一次 ETL 就能救回來的**，
# 而且救不回來的那部分不會有任何錯誤訊息——ETL 會照常「成功」，只是內容變少。
# 這與 embedding 額度無關，是來源端的結構性限制。
#
# 只備份知識庫，**不含 users / medications / family_trees 等含個資的
# collection**：那些要不要落到開發機上是另一個決定，不該由一支備份腳本默默決定。
set -euo pipefail

DB="${MONGODB_DB:-CARE_database}"
OUT_DIR="${BACKUP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
DEST="${OUT_DIR}/kb_${STAMP}"

# 只備份這幾個 collection。knowledge base 之外的都不碰。
COLLECTIONS=(
  health_articles_chunks   # ETL 產出的知識庫，來源已部分不可重建
  drug_news                # Tier 1 索引：可重建，但要重燒搜尋與 LLM 額度
)

if [[ -z "${MONGO_URI:-}" ]]; then
  ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"
  [[ -f "$ENV_FILE" ]] || { echo "找不到 MONGO_URI，也沒有 $ENV_FILE" >&2; exit 1; }
  # 只取這一個變數，不把整個 .env source 進來
  MONGO_URI="$(grep -E '^MONGO_URI=' "$ENV_FILE" | head -1 | cut -d= -f2- | sed 's/^["'\'']//; s/["'\'']$//')"
fi
[[ -n "$MONGO_URI" ]] || { echo ".env 裡沒有 MONGO_URI" >&2; exit 1; }

mkdir -p "$DEST"

# 一個 collection 一份 archive。mongodump 的 --collection 一次只吃一個，而
# --nsInclude 是 mongorestore 才有的選項（mongodump 100.12.2 會直接報
# unknown option）。與其為了單一檔案改用「整庫 dump + --excludeCollection
# 排掉含個資的那些」，不如逐個列舉——白名單漏列只是少備份一個 collection，
# 黑名單漏列是把使用者個資寫到開發機上。
echo "備份 ${DB} 的 ${#COLLECTIONS[@]} 個 collection → ${DEST}/"
for c in "${COLLECTIONS[@]}"; do
  mongodump --uri="$MONGO_URI" --db="$DB" --collection="$c" \
    --gzip --archive="${DEST}/${c}.archive.gz"
done

echo
echo "完成："
du -h "${DEST}"/*.archive.gz

# 還原單一 collection：
#   mongorestore --uri="$MONGO_URI" --gzip \
#     --archive=backups/kb_YYYYmmdd_HHMMSS/health_articles_chunks.archive.gz
# 還原到別的資料庫名（建議先這樣驗，不要直接蓋正式庫）：
#   mongorestore --uri="$MONGO_URI" --gzip --archive=... \
#     --nsFrom='CARE_database.*' --nsTo='CARE_restore_test.*'
#
# 注意：Atlas Vector Search 的索引定義**不在 mongodump 的範圍內**，
# 還原後要在 Atlas console 重建 vector_index / care_text_index。
# embedding 欄位本身有備份到，不需要重新向量化。
