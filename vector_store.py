"""RAG 向量的 PostgreSQL（pgvector）寫入端。

為什麼向量不寫 Mongo
--------------------
2026-09-19 Atlas 免費層 512 MB 被撐爆：3072 維向量一筆 chunk 42 KB，內文只佔
1.4 KB，96% 的空間在向量。之後 CARE 的向量檢索改讀 care-vm 上的 pgvector
（CARE `app/services/rag/pgvector_retriever.py`），內文與 BM25 仍留在 Atlas。
兩邊用同一個 id 對應：PG 的 `id` 就是 Mongo 切片 `_id` 的字串形式。

ETL 原本跑在 GitHub Actions、連不到叢集內的 PG，只好照舊把向量寫進 Atlas，再由
CARE-infra 的 `care-vector-sync` CronJob 每 6 小時搬過去。那座橋漏了兩件事：

1. **重切／改版時刪掉的 Mongo 切片，PG 那邊沒人刪。** 2026-09-22 實測 PG 有
   16,989 筆、Mongo 只有 13,987 筆，3,002 筆向量找不到內文。檢索是 PG 先取前 k
   筆再去 Mongo 撈內文，孤兒佔掉名額、撈不到內文，結果就少幾筆——而且孤兒多半是
   同一篇文章的舊版切片，語意相近、特別容易排在前面。
2. **判定只在搬家那一刻複製一次。** 查核比對是在 PG 以 `WHERE verdict = ANY(...)`
   篩選的，之後 `claim_tagger` 或補日期時寫進 Mongo 的判定，PG 永遠看不到。

所以 ETL 搬進叢集後直接寫這裡，並在每次執行結束時做一次 `reconcile`，把兩邊
對齊——單筆寫入失敗留下的不一致也會在下一次被收掉。
"""
from __future__ import annotations

# 一批的筆數。一筆 3072 維向量以文字表示約 60 KB（每個數字 ~20 字元），
# 200 筆約 12 MB 的參數量，與 CARE 那支同步腳本的批次大小一致。
BATCH_SIZE = 200

# reconcile 一次最多允許刪掉 PG 多少比例的列。
#
# 為什麼要有這道閘：孤兒的判定是「PG 有、Mongo 沒有」，若 Mongo 那邊查錯了
# （連到錯的 database、collection 名打錯、查詢被截斷），會把整張表判成孤兒全刪，
# 而向量刪了只能重算——每天 1,000 次的 embedding 額度要重算 17,000 筆得兩個多星期。
#
# 為什麼是 0.5：正常情況下一次執行只會多出當天改版／重切的舊切片，遠低於此；
# 目前已知最大的一次是首次清理既有的 3,002 筆孤兒（佔 18%）。0.5 放得下那次，
# 又擋得住「Mongo 查回空的或少一半」這類整批誤判。
MAX_ORPHAN_DELETE_RATIO = 0.5


def _vector_literal(vector) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


class PgVectorStore:
    """`health_articles_chunks` 表的寫入與對帳。

    表結構由 2026-09-19 的搬遷建立（CARE-data 沒有建表權限，也不該有）：
    `id text PK, embedding vector(3072), embedding_half halfvec(3072), verdict text`，
    HNSW 索引建在 `embedding_half`。這裡只寫這四欄。
    """

    def __init__(self, dsn: str, table: str = "health_articles_chunks"):
        import psycopg

        self._conn = psycopg.connect(dsn, autocommit=False)
        self._table = table

    def close(self):
        self._conn.close()

    def upsert(self, rows):
        """rows：[(id, vector, verdict), ...]。同一交易內寫完，失敗整批回滾。"""
        rows = [(str(i), _vector_literal(v), verdict) for i, v, verdict in rows]
        if not rows:
            return
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {self._table} (id, embedding, embedding_half, verdict) "
                    "VALUES (%s, %s::vector, %s::vector::halfvec, %s) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  embedding = EXCLUDED.embedding, "
                    "  embedding_half = EXCLUDED.embedding_half, "
                    "  verdict = EXCLUDED.verdict",
                    [(i, lit, lit, verdict) for i, lit, verdict in rows],
                )

    def delete_ids(self, ids):
        ids = [str(i) for i in ids]
        if not ids:
            return
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._table} WHERE id = ANY(%s)", (ids,))

    def all_verdicts(self) -> dict:
        """{id: verdict}，verdict 可能是 None。只取這兩欄，不碰向量。"""
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT id, verdict FROM {self._table}")
            rows = cur.fetchall()
        self._conn.commit()
        return dict(rows)

    def set_verdicts(self, pairs):
        """pairs：[(id, verdict), ...]。"""
        pairs = [(verdict, str(i)) for i, verdict in pairs]
        if not pairs:
            return
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.executemany(
                    f"UPDATE {self._table} SET verdict = %s WHERE id = %s", pairs)


def reconcile(collection, store, *, max_delete_ratio=MAX_ORPHAN_DELETE_RATIO):
    """讓 PG 與 Mongo 對齊：刪孤兒向量、同步判定。回傳統計 dict。

    Mongo 是準：內文、判定都以 Mongo 為主，PG 只是它的向量索引。所以方向永遠是
    「照 Mongo 改 PG」，從不反過來。

    Mongo 有、PG 沒有的切片（沒有向量）只回報不處理：補救要重新向量化，那是
    `upload_to_mongodb` 的工作，而且照現在的寫入順序不該發生。
    """
    mongo = {
        str(d["_id"]): d.get("verdict") or None
        for d in collection.find({}, {"_id": 1, "verdict": 1})
    }
    pg = store.all_verdicts()

    orphans = [i for i in pg if i not in mongo]
    stats = {
        "mongo": len(mongo),
        "pg": len(pg),
        "orphans": len(orphans),
        "orphans_deleted": 0,
        "missing_vectors": sum(1 for i in mongo if i not in pg),
        "verdicts_fixed": 0,
        "refused": False,
    }

    if orphans:
        if not mongo or len(orphans) > len(pg) * max_delete_ratio:
            # 寧可留著孤兒（檢索少幾筆），也不要因為 Mongo 查錯而清掉整張表。
            stats["refused"] = True
        else:
            for start in range(0, len(orphans), BATCH_SIZE):
                store.delete_ids(orphans[start:start + BATCH_SIZE])
            stats["orphans_deleted"] = len(orphans)

    drift = [(i, mongo[i]) for i in pg if i in mongo and pg[i] != mongo[i]]
    for start in range(0, len(drift), BATCH_SIZE):
        store.set_verdicts(drift[start:start + BATCH_SIZE])
    stats["verdicts_fixed"] = len(drift)
    return stats
