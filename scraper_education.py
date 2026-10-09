"""一般衛教的受限接入；只讀預覽不載入資料庫或 embedding。

各來源的 DOM／入口於 2026-10-09 實測。PMDA 網站條款禁止自動巡迴下載，
故即使使用者選取該來源仍回報 blocked，不發 HTTP 請求。
"""
import argparse
import hashlib
import json
import re
import sys
import time
from collections import deque
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote_plus

from bs4 import BeautifulSoup
from cdc_access import CDCAccess, AccessDenied, RequestLimit, TRACKING_KEYS, SECRET_KEYS
from scraper_cdc import CrawlResult, content_hash, _body_text, _date_for
from utils import make_soup

MHLW_ROOT = '/stf/seisakunitsuite/bunya/'
MHLW_ANNEX_URL = 'https://www.mhlw.go.jp/chosakuken/exhibit.html'
MHLW_ANNEX_SHA256 = '74169d9116de0a97fe0ccb9bee31bffbd278551af4e8455460bf05d12b22c5fb'
# 這些明細由實際疾病導覽頁確認；不放寬成任意多層全站路徑。
ECDC_EXTRA_PAGES = (
    '/en/infectious-disease-topics/ebola-disease/disease-information/factsheet-about-ebola-disease',
    '/en/ebola-and-marburg-fevers/facts/questions-answers',
    '/en/seasonal-influenza/prevention-and-control/personal-protective-measures',
    '/en/seasonal-influenza/prevention-and-control/antivirals',
)
MHLW_PAGES = (
    MHLW_ROOT + 'kenkou_iryou/shokuhin/syokuchu/01_00008.html',
    MHLW_ROOT + '0000177609.html',
    MHLW_ROOT + 'kenkou_iryou/kenkou/kekkaku-kansenshou/infulenza/QA2026.html',
    MHLW_ROOT + 'kenkou_iryou/kenkou/undou/index.html',
)
SOURCES = {
    'hpa': dict(source='國健署主題衛教', host='www.hpa.gov.tw', language='zh-TW', jurisdiction='臺灣',
                license='政府資料開放授權條款－第1版', license_url='https://data.gov.tw/license',
                policy_url='https://www.hpa.gov.tw/Pages/Detail.aspx?nodeid=92&pid=5141',
                policy_selector='.ContentWrap .htmlBlock .tako-retirement', flag='HPA_THEME_ENABLED',
                roots=['/Pages/List.aspx?nodeid=' + n for n in ('46', '36', '37', '39')],
                sample_url='https://www.hpa.gov.tw/Pages/Detail.aspx?nodeid=543&pid=8365',
                creator='衛生福利部國民健康署'),
    'ecdc': dict(source='ECDC 疾病衛教', host='www.ecdc.europa.eu', language='en', jurisdiction='歐盟／歐洲經濟區',
                 license='CC BY 4.0', license_url='https://creativecommons.org/licenses/by/4.0/',
                 policy_url='https://www.ecdc.europa.eu/en/ecdc-intellectual-property-notices',
                 policy_selector='article.full .wysiwyg-content', flag='ECDC_EDUCATION_ENABLED',
                 roots=['/en/all-topics'], sample_url='https://www.ecdc.europa.eu/en/q-fever/facts',
                 creator='European Centre for Disease Prevention and Control (ECDC)'),
    'mhlw': dict(source='厚生勞動省健康與疾病預防', host='www.mhlw.go.jp', language='ja', jurisdiction='日本',
                 license='公共データ利用規約（第1.0版）(PDL1.0)',
                 license_url='https://www.digital.go.jp/resources/open_data/public_data_license_v1.0',
                 policy_url='https://www.mhlw.go.jp/chosakuken/index.html',
                 policy_selector='main .l-contentMain', flag='MHLW_EDUCATION_ENABLED',
                 roots=list(MHLW_PAGES), sample_url='https://www.mhlw.go.jp' + MHLW_PAGES[0],
                 creator='厚生労働省'),
    'pmda': dict(source='PMDA 用藥安全衛教', host='www.pmda.go.jp', language='ja', jurisdiction='日本',
                 license=None, license_url='https://www.pmda.go.jp/0048.html',
                 flag='PMDA_EDUCATION_ENABLED', roots=[], blocked=True,
                 reason='網站條款禁止自動巡迴下載；待確認機構允許的取得方式，不擷取藥廠文件'),
}

# 經人工審閱的完整條款正文（僅移除排版空白）；異動後停抓，不能靠關鍵字猜授權。
POLICY_SHA256 = {
    'hpa': 'a462da1357ab24b8405b4478f37b1c95d8c692bbaf4e9c26f2d57602b032550f',
    'ecdc': 'd7934f366418f23829423b8895ace99da62acab0e12f027269c4f00d27add919',
    'mhlw': 'f72770c6356c5eee6e6acf0c68b7aca18d52882bb499d16f56bf4ecdc8b45416',
}
EDUCATION_SOURCES = frozenset(c['source'] for c in SOURCES.values())
ADMIN = re.compile(r'招標|採購|甄選|徵才|經費|核定|公告|補助|法規|研討會|師資|作業手冊|隱私權|計畫|計劃|好站連結|相關出版品|影音|檔案下載')
RIGHTS = re.compile(r'(?:禁止|不得)(?:轉載|重製|改作)|(?:本文|本篇|本素材)(?:版權所有)?[，,:；。\s]*(?:禁止|不得)使用|未經(?:本署|本機關|著作權人|權利人).{0,10}同意|'
                   r'本文.{0,30}(?:轉載自|版權歸|著作權歸)|(?:作者|撰文|文／)\s*[：:]|'
                   r'all rights reserved|do not (?:reproduce|copy)|copyright\s*(?:©|\(c\))\s*(?!ECDC)|'
                   r'無断(?:転載|複製)|(?:転載|複製|改変)禁止|第三者.{0,20}著作権', re.I)


def normalize_education_url(source, url):
    from scraper_cdc import SOURCE_NAME
    from cdc_access import normalize_url
    if source == SOURCE_NAME:
        return normalize_url(url, '/Category/Page/')
    key = next((k for k, cfg in SOURCES.items() if cfg['source'] == source), None)
    return normalize_source_url(key, url) if key else None


def normalize_source_url(key, raw):
    cfg = SOURCES[key]
    if cfg.get('blocked'):
        return None
    try:
        p = urlsplit(urljoin('https://' + cfg['host'] + '/', raw or ''))
        if (p.scheme not in {'http', 'https'} or p.hostname != cfg['host'] or p.username
                or p.password or p.port is not None):
            return None
        path = p.path
        policy_path = urlsplit(cfg['policy_url']).path
        allowed = path in {'/robots.txt', policy_path}
        if key == 'hpa':
            allowed = allowed or path in {'/Pages/List.aspx', '/Pages/TopicList.aspx', '/Pages/Detail.aspx'}
        elif key == 'ecdc':
            allowed = allowed or path in ECDC_EXTRA_PAGES or bool(re.fullmatch(r'/en/[a-z0-9-]+(?:/(?:facts|factsheet|prevention-and-control))?', path))
        elif key == 'mhlw':
            allowed = allowed or path in MHLW_PAGES or path == '/chosakuken/exhibit.html'
        if not allowed:
            return None
        query = []
        for part in p.query.split('&') if p.query else []:
            name = unquote_plus(part.split('=', 1)[0]).lower()
            if name in SECRET_KEYS:
                return None
            if name not in TRACKING_KEYS:
                query.append(part)
        if key == 'hpa' and path != '/robots.txt':
            names = {unquote_plus(x.split('=', 1)[0]).lower() for x in query}
            if 'nodeid' not in names or (path.endswith('Detail.aspx') and 'pid' not in names):
                return None
        return urlunsplit(('https', cfg['host'], path, '&'.join(query), ''))
    except ValueError:
        return None


def _soup(html):
    return BeautifulSoup(html, 'html.parser') if isinstance(html, (str, bytes)) else html


def policy_digest(key, html):
    nodes = _soup(html).select(SOURCES[key]['policy_selector'])
    if not nodes:
        raise AccessDenied('找不到授權正文，停止來源')
    text = ''.join(''.join(n.get_text().split()) for n in nodes)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def check_policy(key, html):
    if policy_digest(key, html) != POLICY_SHA256.get(key):
        raise AccessDenied('授權正文與審核版本不同，停止來源並待人工重新確認')


def check_mhlw_annex(html):
    body = _soup(html).select_one('#contentsInner')
    text = ''.join(body.get_text().split()) if body else ''
    if hashlib.sha256(text.encode()).hexdigest() != MHLW_ANNEX_SHA256:
        raise AccessDenied('MHLW 授權排除別紙異動，停止來源並待人工重新確認')


def parse_article(key, html, url):
    cfg = SOURCES[key]; soup = _soup(html)
    canonical = normalize_source_url(key, url)
    if not canonical:
        return None
    if key == 'hpa':
        heading = soup.select_one('.ContentWrap h3.pageTitle')
        nodes = soup.select('.ContentWrap .htmlBlock')
        dates = ' '.join(n.get_text(' ', strip=True) for n in soup.select('.ContentWrap .pageInfo'))
        published = _date_for(dates, '發布日期')
        updated = _date_for(dates, '更新日期')
    elif key == 'ecdc':
        heading = soup.select_one('article.full h1')
        nodes = soup.select('article.full .wysiwyg-content')
        published = updated = None
    elif key == 'mhlw':
        heading = soup.select_one('.l-contentMain h1')
        nodes = soup.select('.l-contentMain')
        published = updated = None
        # 只讀明示的最終改訂／版本日，絕不抽任意內文日期當發布日。
        match = re.search(r'(?:最終改訂[：:]?\s*|令和)(?:令和)?\s*(\d+)年(\d+)月(\d+)日(?:版)?',
                          nodes[0].get_text(' ', strip=True)[:350] if nodes else '')
        if match:
            y, m, d = map(int, match.groups())
            try:
                updated = date(2018 + y, m, d).isoformat()
            except ValueError:
                pass
    else:
        return None
    if heading is None or not nodes:
        if key == 'ecdc' and heading and soup.select_one('article.full .paragraph--type--pt-static-list,article.full .paragraph--type--pt-system-component'):
            raise AccessDenied('此疾病頁只有導覽卡片，沒有獨立衛教正文')
        return None
    title = heading.get_text(' ', strip=True)
    if key == 'mhlw':
        title = title.removeprefix('健康・医療').strip()
    if not title or ADMIN.search(title):
        raise AccessDenied('行政／非衛教標題，排除')
    body = BeautifulSoup(''.join(str(n) for n in nodes), 'html.parser')
    for n in body.select('script,style,noscript,nav,footer,figure,img,video,audio,iframe,.m-navLocal,.m-txtAdobeReader,.m-boxReader,.paperBtn,h1'):
        n.decompose()
    # 只擷取文字正文，不以附件名稱、外站引用或媒體 alt text 補正文。
    for a in body.select('a[href]'):
        href = a['href']
        if (a.get_text(strip=True) in {'上一則', '下一則', 'ページの先頭へ戻る'}
                or re.search(r'\.(?:pdf|docx?|xlsx?|pptx?|mp4|jpe?g|png)(?:\?|$)', href, re.I)):
            a.decompose()
    text = _body_text(body)
    if key == 'ecdc' and len(text) < 200 and text.startswith('This page contains'):
        raise AccessDenied('只有欄目導覽摘要，没有疾病／預防正文')
    if RIGHTS.search(text):
        raise AccessDenied('正文有第三方／另行權利限制提示，待人工確認')
    if len(text) < 80:
        raise AccessDenied('只有附件／媒體或不足的正文，不以 OCR/PDF 補救')
    return dict(title=title, content=text, source=cfg['source'], url=canonical,
                published_at=published, updated_at=updated, content_type='health_education',
                language=cfg['language'], jurisdiction=cfg['jurisdiction'], license=cfg['license'],
                license_url=cfg['license_url'], retrieved_at=datetime.now(timezone.utc).isoformat(),
                attribution=f"資料來源：{cfg['creator']}；原文：{canonical}。CARE 擷取、清洗與切片；適用地區：{cfg['jurisdiction']}。",
                content_hash=content_hash(title, text), claim=None, verdict=None, verdict_slug=None)


def discover_links(key, html, url, depth):
    """只從指定正文分類／翻頁區探索，絕不走全站選單、推薦、搜尋或新聞列表。"""
    soup = _soup(html)
    if key == 'hpa':
        anchors = soup.select('.ContentWrap .tl-item a[href],.ContentWrap .listBox a[href],.ContentWrap .page a[href]')
    elif key == 'ecdc' and depth == 0:
        anchors = soup.select('article.full .disease-list--default a[href]')
    elif key == 'ecdc' and depth < 2:
        anchors = soup.select('article.full .ecdc-side-nav a.nav-link[href]')
    elif key == 'ecdc' and depth == 2:
        anchors = soup.select('article.full .paragraph--type--pt-system-component a[href]')
    else:
        return []
    rows = []
    for a in anchors:
        label = a.get_text(' ', strip=True)
        if ADMIN.search(label):
            continue
        target = normalize_source_url(key, urljoin(url, a['href']))
        if not target or target == url:
            continue
        if key == 'ecdc' and depth > 0 and not (urlsplit(target).path in ECDC_EXTRA_PAGES or
                re.search(r'/(?:facts|factsheet|prevention-and-control)$', urlsplit(target).path)):
            continue
        is_article = '/Detail.aspx' in target if key == 'hpa' else depth > 0
        if key == 'hpa' and not is_article and depth >= 4:
            continue
        rows.append((target, is_article))
    return list(dict.fromkeys(rows))


def crawl_education(key, *, max_articles=200, max_requests=500, start_at=0, get=None, sleep=time.sleep):
    if key not in SOURCES or not 1 <= max_articles <= 200 or start_at < 0:
        raise ValueError('來源無效；max_articles 必須 1..200，start_at 必須非負')
    cfg = SOURCES[key]
    stats = dict(source=cfg['source'], success=0, excluded=0, failed=0, requests=0,
                 limited=False, exhausted=False, blocked=bool(cfg.get('blocked')), next_offset=start_at, exclusions=[])
    if cfg.get('blocked'):
        stats['exclusions'].append(dict(reason=cfg['reason']))
        stats['excluded'] = 1
        return CrawlResult([], stats)
    normalizer = lambda u: normalize_source_url(key, u)
    access = CDCAccess(get=get, sleep=sleep, sleep_seconds=0.5, max_requests=max_requests,
                       normalizer=normalizer, robots_url='https://' + cfg['host'] + '/robots.txt')
    queue = deque((normalizer(u), 0, key == 'mhlw') for u in cfg['roots'])
    seen = set(); accepted = set(); articles = []; article_offset = 0; attempted_details = 0
    try:
        access.load_robots()
        check_policy(key, make_soup(access.get(cfg['policy_url'])))
        if key == 'mhlw':
            check_mhlw_annex(make_soup(access.get(MHLW_ANNEX_URL)))
        while queue and len(articles) < max_articles:
            url, depth, is_article = queue.popleft()
            soup = None; final_url = url
            if url in seen:
                continue
            seen.add(url)
            try:
                if is_article and article_offset < start_at:
                    article_offset += 1
                    continue
                if is_article:
                    attempted_details += 1
                response = access.get(url)
                soup = make_soup(response)
                final_url = normalizer(response.url or url)
                if not is_article:
                    # HPA 主題 List 頁有自有 RLintro/htmlBlock 文字，不能只取連出去的 Detail。
                    # 空分類／純導航不當文章；同樣納入候選 offset 與每來源文章預算。
                    if key == 'hpa':
                        intro = soup.select_one('.ContentWrap .RLintro .htmlBlock')
                        if intro and len(intro.get_text(strip=True)) >= 80:
                            if article_offset >= start_at:
                                attempted_details += 1
                                article = parse_article(key, soup, final_url)
                                if article and article['url'] not in accepted:
                                    accepted.add(article['url']); articles.append(article)
                            article_offset += 1
                    links = discover_links(key, soup, final_url, depth)
                    for target, detail in (reversed(links) if key == 'ecdc' and depth > 0 else links):
                        if key == 'ecdc' and detail:
                            queue.appendleft((target, depth + 1, detail))
                        else:
                            queue.append((target, depth + 1, detail))
                    continue
                article_offset += 1
                article = parse_article(key, soup, final_url)
                if article is None:
                    raise ValueError('缺少標題或正文，可能是版面變更')
                if article['url'] not in accepted:
                    accepted.add(article['url'])
                    articles.append(article)
            except RequestLimit:
                stats['limited'] = True
                break
            except AccessDenied as exc:
                stats['excluded'] += 1
                stats['exclusions'].append(dict(url=url, reason=str(exc)))
                if key == 'ecdc' and is_article and depth == 2 and soup is not None:
                    for target, detail in reversed(discover_links(key, soup, final_url, depth)):
                        queue.appendleft((target, depth + 1, detail))
            except Exception as exc:
                stats['failed'] += 1
                print(f"[{cfg['source']}] 抓取／解析失敗 {url} ({type(exc).__name__})", file=sys.stderr)
        stats['limited'] = stats['limited'] or bool(queue)
        stats['exhausted'] = (not queue and stats['failed'] == 0 and start_at > 0
                              and attempted_details == 0 and article_offset <= start_at)
        if not articles and article_offset == 0:
            raise ValueError('官方索引沒有有效文章入口')
    except Exception as exc:
        stats['failed'] += 1
        print(f"[{cfg['source']}] 前置檢查／索引失敗 ({type(exc).__name__})", file=sys.stderr)
    stats.update(success=len(articles), requests=access.requests, next_offset=article_offset)
    return CrawlResult(articles, stats)


def main(argv=None, *, get=None, sleep=time.sleep, stdout=None):
    parser = argparse.ArgumentParser(description='官方一般衛教只讀預覽，不向量化／入庫')
    parser.add_argument('--source', choices=SOURCES, required=True)
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 5:
        parser.error('preview --limit 必須 1..5')
    result = crawl_education(args.source, max_articles=args.limit, start_at=args.offset, get=get, sleep=sleep)
    json.dump(dict(articles=[dict(a, content_length=len(a['content'])) for a in result.articles], stats=result.stats),
              stdout or sys.stdout, ensure_ascii=False, indent=2)
    return 0 if result.articles or (result.stats['exhausted'] and not result.stats['failed']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
