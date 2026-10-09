"""官方衛教來源限定批次。預設只讀；--write 才連 DB 並向量化。

不呼叫完整 job()，不執行媒體推播、政府查核標記、全庫對帳或全庫刪除。
輸入可用同工具 --output 產生的當日公開文章 JSON；正式寫入前重新核對政策。
"""
import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from scraper_cdc import crawl_cdc, SOURCE_NAME as CDC_SOURCE, LICENSE_NAME, LICENSE_URL, LICENSE_BODY_SHA256
from scraper_education import SOURCES, crawl_education, check_policy, normalize_education_url
from cdc_access import CDCAccess
from utils import make_soup


class RedactingStream:
    def __init__(self, stream, secrets):
        self.stream = stream
        self.secrets = [s for s in secrets if s]
    def write(self, text):
        for secret in self.secrets:
            text = text.replace(secret, '[REDACTED]')
        return self.stream.write(text)
    def flush(self):
        return self.stream.flush()


def validate_articles(articles):
    configs = {c['source']: c for c in SOURCES.values() if not c.get('blocked')}
    configs[CDC_SOURCE] = dict(license=LICENSE_NAME, license_url=LICENSE_URL, language='zh-TW', jurisdiction='臺灣')
    for article in articles:
        cfg = configs.get(article.get('source'))
        if cfg is None or not normalize_education_url(article['source'], article.get('url')):
            raise ValueError('輸入含未知、停用來源或不允許的 URL')
        for key in ('license', 'license_url', 'language', 'jurisdiction'):
            if article.get(key) != cfg[key]:
                raise ValueError('輸入授權／語言／地區與來源規格不符：' + key)
        if (article.get('content_type') != 'health_education' or not article.get('title')
                or not article.get('content') or not article.get('attribution') or not article.get('retrieved_at')):
            raise ValueError('輸入缺少正文或必要 metadata')


def ingest_batch(articles, collection, store, *, embed_fn, max_embedding_calls=800):
    """沿用原 ETL 的全篇寫入契約；呼叫預算耗盡時不保存部分文章。"""
    from main_pipeline import upload_to_mongodb, DailyQuotaExhausted
    if not 1 <= max_embedding_calls <= 5000:
        raise ValueError('embedding 呼叫預算必須 1..5000')
    validate_articles(articles)
    calls = 0
    def embed(text):
        nonlocal calls
        if calls >= max_embedding_calls:
            raise DailyQuotaExhausted('本批 embedding 呼叫預算用盡')
        calls += 1
        vector = embed_fn(text)
        if len(vector) != 3072 or not all(math.isfinite(v) for v in vector):
            raise ValueError('embedding 維度或數值無效')
        return vector
    new, failed = upload_to_mongodb(articles, collection, vector_store=store, embed_fn=embed)
    return dict(new_or_rewritten_articles=new, write_failed=failed, embedding_calls=calls,
                embedding_call_budget=max_embedding_calls)


def verify_batch(articles, collection, store):
    """唯讀查驗這批 URL 的正文、metadata 及 Mongo／PG ID；不對帳刪除。"""
    from main_pipeline import chunk_text, ARTICLE_METADATA_FIELDS
    urls = [a['url'] for a in articles]
    docs = list(collection.find({'url': {'$in': urls}}))
    ids = [str(d['_id']) for d in docs]
    with store._conn.cursor() as cur:
        cur.execute('SELECT id, vector_dims(embedding), embedding_half IS NOT NULL, verdict '
                    'FROM health_articles_chunks WHERE id = ANY(%s)', (ids,))
        rows = cur.fetchall()
    store._conn.commit()
    vectors = {r[0]: r for r in rows}
    grouped = {}
    for d in docs:
        grouped.setdefault(d['url'], []).append(d)
    problems = []; remaining = []; covered = []; counts = {}
    for a in articles:
        chunks = sorted(grouped.get(a['url'], []), key=lambda d: d.get('chunk_index', 0))
        if not chunks:
            remaining.append(a['url']); continue
        if chunks[0]['source_name'] != a['source']:
            covered.append(dict(url=a['url'], source=chunks[0]['source_name']))
            continue
        expected = chunk_text(a['content'])
        if (len(chunks) != len(expected) or [d['chunk_content'] for d in chunks] != expected
                or [d['chunk_index'] for d in chunks] != list(range(1, len(expected) + 1))
                or any(d.get('total_chunks') != len(expected) or d.get('original_title') != a['title']
                       or any(d.get(k) != a.get(k) for k in ARTICLE_METADATA_FIELDS)
                       or any(d.get(k) is not None for k in ('claim', 'verdict', 'verdict_slug'))
                       or 'embedding' in d for d in chunks)):
            problems.append(dict(url=a['url'], issue='正文／切片／metadata 不符'))
        if any(str(d['_id']) not in vectors or vectors[str(d['_id'])][1:] != (3072, True, None) for d in chunks):
            problems.append(dict(url=a['url'], issue='PG 向量缺失／維度／判定不符'))
        item = counts.setdefault(a['source'], dict(articles=0, chunks=0))
        item['articles'] += 1; item['chunks'] += len(chunks)
    return dict(sources=counts, remaining_urls=remaining, covered_by_existing_sources=covered,
                integrity_problems=problems, verified_pg_vectors=len(rows))


def check_cached_policies(articles):
    for source in {a['source'] for a in articles}:
        if source == CDC_SOURCE:
            access = CDCAccess(); access.load_robots()
            soup = make_soup(access.get(LICENSE_URL)); body = soup.select_one('.m-t-30.m-b-30')
            digest = hashlib.sha256(''.join(body.get_text().split()).encode()).hexdigest() if body else None
            if digest != LICENSE_BODY_SHA256:
                raise ValueError('CDC 授權已變更，停止入庫')
        else:
            from scraper_education import normalize_source_url, check_mhlw_annex, MHLW_ANNEX_URL
            key = next(k for k, c in SOURCES.items() if c['source'] == source)
            cfg = SOURCES[key]
            access = CDCAccess(normalizer=lambda u, key=key: normalize_source_url(key, u),
                               robots_url='https://' + cfg['host'] + '/robots.txt')
            access.load_robots(); check_policy(key, make_soup(access.get(cfg['policy_url'])))
            if key == 'mhlw':
                check_mhlw_annex(make_soup(access.get(MHLW_ANNEX_URL)))
        for a in articles:
            if a['source'] == source and not access.robots.can_fetch('CARE-data/0.1', a['url']):
                raise ValueError('最新 robots 禁止快取文章路徑，停止入庫')


def main(argv=None):
    p = argparse.ArgumentParser(description='指定官方衛教批次；預設只讀，不执行全庫 ETL')
    p.add_argument('--source', action='append', choices=('cdc', *SOURCES), help='可重複指定')
    p.add_argument('--limit', type=int, default=3, help='每來源上限；預覽 1..5，寫入 1..200')
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--input', type=Path, help='讀同工具產生的公開 JSON，避免重新爬取')
    p.add_argument('--output', type=Path, help='保存不含秘密的公開文章／驗證報告')
    p.add_argument('--write', action='store_true', help='已授權的正式 embedding 與來源限定 DB 寫入')
    p.add_argument('--max-embedding-calls', type=int, default=800)
    args = p.parse_args(argv)
    if not 1 <= args.limit <= (200 if args.write else 5):
        p.error('--limit 預覽必須 1..5，寫入必須 1..200')
    if args.input:
        payload = json.loads(args.input.read_text(encoding='utf-8'))
        articles = payload['articles']
        validate_articles(articles)
        if args.source:
            names = {CDC_SOURCE if k == 'cdc' else SOURCES[k]['source'] for k in args.source}
            articles = [a for a in articles if a['source'] in names]
        # 每個來源獨立限制，以維持小量／分批入口的一致性。
        selected = []; seen = {}
        for a in articles:
            n = seen.get(a['source'], 0); seen[a['source']] = n + 1
            if args.offset <= n < args.offset + args.limit:
                selected.append(a)
        articles = selected
        stats = payload.get('crawl_stats', [])
    else:
        articles = []; stats = []
        for key in args.source or ('cdc', 'hpa', 'ecdc', 'mhlw', 'pmda'):
            result = (crawl_cdc if key == 'cdc' else lambda **kw: crawl_education(key, **kw))(
                max_articles=args.limit, start_at=args.offset)
            articles.extend(result.articles)
            stats.append(dict(result.stats, source=CDC_SOURCE if key == 'cdc' else SOURCES[key]['source']))
    payload = dict(articles=articles, crawl_stats=stats, retrieved_at=datetime.now(timezone.utc).isoformat())
    if args.write:
        if not articles:
            raise ValueError('沒有有效文章，不連 DB／不向量化')
        validate_articles(articles)
        for a in articles:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(a['retrieved_at'])
            if age.total_seconds() < 0 or age.total_seconds() > 86400:
                raise ValueError('只允許 24 小時內的公開文章快取，請重新抓取')
        check_cached_policies(articles)
        from main_pipeline import _default_collection, _default_vector_store, get_embedding
        secrets = [os.getenv(k) for k in ('MONGO_URI', 'MONGODB_URI', 'GEMINI_API_KEY', 'PGVECTOR_SYNC_DSN')]
        sys.stdout = RedactingStream(sys.stdout, secrets)
        sys.stderr = RedactingStream(sys.stderr, secrets)
        collection = _default_collection(); store = _default_vector_store()
        try:
            report = ingest_batch(articles, collection, store, embed_fn=get_embedding,
                                  max_embedding_calls=args.max_embedding_calls)
            report.update(verify_batch(articles, collection, store))
            payload['ingestion'] = report
        finally:
            store.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in payload.items() if k != 'articles'}, ensure_ascii=False))
    print(json.dumps(dict(articles=[{k: v for k, v in a.items() if k != 'content'} | {'content_length': len(a['content'])}
                                   for a in articles]), ensure_ascii=False))
    report = payload.get('ingestion', {})
    return int(bool(report.get('write_failed') or report.get('remaining_urls') or report.get('integrity_problems')
                    or any(s.get('failed') for s in stats) or not articles))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # DB/API 例外可能包含連線字串，CLI 只印類型，詳細來源失敗由爬蟲另行安全記錄。
        print('衛教批次失敗：' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
