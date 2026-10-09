"""新增衛教來源的離線契約驗證，不使用正式 DB／embedding。"""
import io
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from test_system import FakeCollection, FakeVectorStore, fake_embed_ok

FIXTURES = Path(__file__).parent / 'tests/fixtures/education'


class TestEducationSources(unittest.TestCase):
    @staticmethod
    def response(html, url, content_type='text/html; charset=utf-8'):
        import requests
        r = requests.Response(); r.status_code = 200; r.url = url
        r._content = html.encode(); r.encoding = 'utf-8'
        r.headers['content-type'] = content_type
        return r

    def test_parsers_preserve_body_and_metadata(self):
        from scraper_education import parse_article, SOURCES
        for key in ('hpa', 'ecdc', 'mhlw'):
            with self.subTest(source=key):
                cfg = SOURCES[key]
                article = parse_article(key, (FIXTURES / (key + '-detail.html')).read_text(), cfg['sample_url'])
                self.assertTrue(article['content'])
                self.assertEqual(article['source'], cfg['source'])
                self.assertEqual(article['language'], cfg['language'])
                self.assertEqual(article['jurisdiction'], cfg['jurisdiction'])
                self.assertEqual(article['license'], cfg['license'])
                self.assertIn('CARE', article['attribution'])
                self.assertIn('retrieved_at', article)
                self.assertNotIn('頁尾測試', article['content'])

    def test_missing_heading_and_body_fail(self):
        from scraper_education import parse_article, SOURCES
        for key in ('hpa', 'ecdc', 'mhlw'):
            self.assertIsNone(parse_article(key, '<footer>頁尾測試</footer>', SOURCES[key]['sample_url']))

    def test_ecdc_directory_teaser_is_not_a_disease_article(self):
        from scraper_education import parse_article, SOURCES
        from cdc_access import AccessDenied
        html = '<article class="full"><h1>Facts about antimicrobial consumption</h1><div class="wysiwyg-content">This page contains facts, infographics and videos on antimicrobial consumption in Europe</div></article>'
        with self.assertRaises(AccessDenied):
            parse_article('ecdc', html, SOURCES['ecdc']['sample_url'])

    def test_hpa_theme_page_own_intro_is_collected(self):
        from scraper_education import crawl_education, SOURCES
        cfg = SOURCES['hpa']
        detail = (FIXTURES / 'hpa-detail.html').read_text()
        intro = detail.replace('class="htmlBlock"', 'class="RLintro"><div class="htmlBlock"').replace('</div></div><footer>', '</div></div></div><footer>')
        def get(url):
            if url.endswith('robots.txt'):
                return self.response('User-agent: *\nDisallow: /File', url, 'text/plain')
            if url == cfg['policy_url']:
                return self.response((FIXTURES / 'hpa-policy.html').read_text(), url)
            return self.response(intro, url)
        result = crawl_education('hpa', max_articles=1, get=get, sleep=lambda _: None)
        self.assertEqual(len(result.articles), 1)
        self.assertIn('/Pages/List.aspx?', result.articles[0]['url'])

    def test_article_navigation_is_not_body(self):
        from scraper_education import parse_article, SOURCES
        html = (FIXTURES / 'hpa-detail.html').read_text().replace('</div></div><footer>',
            '<a href="/Pages/Detail.aspx?nodeid=543&pid=999">下一則</a></div></div><footer>')
        article = parse_article('hpa', html, SOURCES['hpa']['sample_url'])
        self.assertNotIn('下一則', article['content'])

    def test_pmda_never_requests_even_if_selected(self):
        from scraper_education import crawl_education
        get = Mock(side_effect=AssertionError('禁止任何請求'))
        result = crawl_education('pmda', get=get)
        self.assertEqual(result.articles, [])
        self.assertTrue(result.stats['blocked'])
        get.assert_not_called()

    def test_offsite_pdf_and_secrets_rejected_unknown_query_preserved(self):
        from scraper_education import normalize_source_url
        for key in ('hpa', 'ecdc', 'mhlw'):
            for raw in ('https://evil.example/x', '/files/a.pdf', '/admin/', '?api_key=secret'):
                self.assertIsNone(normalize_source_url(key, raw))
        url = normalize_source_url('hpa', '/Pages/Detail.aspx?nodeid=543&pid=8365&edition=2&utm_source=x')
        self.assertIn('edition=2', url)
        self.assertNotIn('utm_source', url)

    def test_general_education_urls_do_not_collide_on_title_and_hash_is_stable(self):
        from scraper_education import parse_article, SOURCES
        from main_pipeline import upload_to_mongodb
        cfg = SOURCES['ecdc']
        a = parse_article('ecdc', (FIXTURES / 'ecdc-detail.html').read_text(), cfg['sample_url'])
        b = dict(a, url='https://www.ecdc.europa.eu/en/dengue/facts')
        coll = FakeCollection([]); store = FakeVectorStore()
        self.assertEqual(upload_to_mongodb([a, b], coll, vector_store=store, embed_fn=fake_embed_ok), (2, False))
        embed = Mock(side_effect=AssertionError('重跑不能再向量化'))
        self.assertEqual(upload_to_mongodb([dict(a, retrieved_at='new')], coll, vector_store=store, embed_fn=embed), (0, False))
        for d in coll.docs:
            self.assertEqual(d['language'], 'en')
            self.assertEqual(d['license'], 'CC BY 4.0')
            self.assertIsNone(d['claim'])
        changed = dict(a, content=a['content'] + '\nUpdated preventive advice.')
        self.assertEqual(upload_to_mongodb([changed], coll, vector_store=store, embed_fn=fake_embed_ok), (1, False))

    def test_source_flags_default_off(self):
        from main_pipeline import enabled_sources, build_fetchers
        from scraper_education import SOURCES
        for key in ('hpa', 'ecdc', 'mhlw', 'pmda'):
            self.assertNotIn(SOURCES[key]['source'], enabled_sources({}))
        self.assertIn(SOURCES['ecdc']['source'], enabled_sources({'ECDC_EDUCATION_ENABLED': 'true'}))
        self.assertEqual(len(build_fetchers({})), 5)

    def test_rights_restriction_prevents_article(self):
        from scraper_education import parse_article, SOURCES
        from cdc_access import AccessDenied
        html = (FIXTURES / 'ecdc-detail.html').read_text().replace('Preventive measures', 'All rights reserved. Do not reproduce. Preventive measures')
        with self.assertRaises(AccessDenied):
            parse_article('ecdc', html, SOURCES['ecdc']['sample_url'])

    def test_medical_use_restrictions_are_body_not_copyright(self):
        from scraper_education import parse_article, SOURCES
        html = (FIXTURES / 'hpa-detail.html').read_text().replace('均衡飲食</h4>', '均衡飲食</h4><p>孕婦禁止使用某些藥品，須詢問醫師。</p>')
        a = parse_article('hpa', html, SOURCES['hpa']['sample_url'])
        self.assertIn('孕婦禁止使用', a['content'])
        from test_system import TestCDCDiseaseScraper
        from scraper_cdc import parse_disease_detail
        html = TestCDCDiseaseScraper.fixture('detail.html').replace('測試疾病介紹內容。', '孕婦禁止使用metronidazole。')
        self.assertIn('孕婦禁止使用', parse_disease_detail(html, '測試', 'https://www.cdc.gov.tw/Category/Page/test')['content'])
        html = (FIXTURES / 'hpa-detail.html').read_text().replace('均衡飲食</h4>', '均衡飲食</h4><p>未經醫師同意，請勿自行停藥。</p>')
        self.assertIn('未經醫師同意', parse_article('hpa', html, SOURCES['hpa']['sample_url'])['content'])
        html = TestCDCDiseaseScraper.fixture('detail.html').replace('測試疾病介紹內容。', '未經醫師評估不得使用藥品。')
        self.assertIn('未經醫師評估', parse_disease_detail(html, '測試', 'https://www.cdc.gov.tw/Category/Page/test')['content'])
        html = TestCDCDiseaseScraper.fixture('detail.html').replace('測試疾病介紹內容。', '本文建議，須經醫師同意方可使用。')
        self.assertIn('須經醫師同意', parse_disease_detail(html, '測試', 'https://www.cdc.gov.tw/Category/Page/test')['content'])

    def test_policy_fingerprint_detects_negation(self):
        from scraper_education import check_policy
        html = (FIXTURES / 'ecdc-policy.html').read_text()
        check_policy('ecdc', html)
        with self.assertRaises(ValueError):
            check_policy('ecdc', html.replace('They may be reproduced', 'They may NOT be reproduced'))

    def test_mhlw_annex_changes_stop_collection(self):
        from scraper_education import check_mhlw_annex
        from cdc_access import AccessDenied
        html = (FIXTURES / 'mhlw-annex.html').read_text()
        check_mhlw_annex(html)
        with self.assertRaises(AccessDenied):
            check_mhlw_annex(html.replace('別の利用ルール', '利用禁止'))

    def test_catalog_scopes_and_hpa_actual_pagination(self):
        from scraper_education import discover_links
        html = '<div class="ContentWrap"><div class="listBox"><a href="/Pages/Detail.aspx?nodeid=543&pid=8365">飲食</a></div><div class="page"><a href="?idx=1&nodeid=543">2</a></div></div><footer><a href="/Pages/Detail.aspx?nodeid=129&pid=999">新聞</a></footer>'
        rows = discover_links('hpa', html, 'https://www.hpa.gov.tw/Pages/TopicList.aspx?nodeid=543', 1)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0][1])
        self.assertIn('idx=1', rows[1][0])
        self.assertFalse(rows[1][1])
        ecdc = '<article class="full"><div class="ecdc-side-nav"><a class="nav-link" href="/en/q-fever/facts">Disease information</a><a class="nav-link" href="/en/q-fever/surveillance">Surveillance</a></div></article>'
        self.assertEqual(discover_links('ecdc', ecdc, 'https://www.ecdc.europa.eu/en/q-fever', 1),
                         [('https://www.ecdc.europa.eu/en/q-fever/facts', True)])
        nested = '<article class="full"><div class="paragraph--type--pt-system-component"><a href="/en/seasonal-influenza/prevention-and-control/personal-protective-measures">Protect yourself</a><a href="/en/publications-data/a-report">Report</a></div></article>'
        self.assertEqual(discover_links('ecdc', nested, 'https://www.ecdc.europa.eu/en/seasonal-influenza/prevention-and-control', 2),
                         [('https://www.ecdc.europa.eu/en/seasonal-influenza/prevention-and-control/personal-protective-measures', True)])

    def test_crawl_robots_policy_limit_and_duplicate_links(self):
        from scraper_education import crawl_education, SOURCES
        cfg = SOURCES['ecdc']; calls = []
        pages = {
            'https://www.ecdc.europa.eu/robots.txt': 'User-agent: *\nDisallow: /admin/',
            cfg['policy_url']: (FIXTURES / 'ecdc-policy.html').read_text(),
            'https://www.ecdc.europa.eu/en/all-topics': '<article class="full"><div class="disease-list--default"><a href="/en/q-fever">Q fever</a><a href="/en/q-fever">duplicate</a><a href="https://evil.example/a">outside</a></div></article>',
            'https://www.ecdc.europa.eu/en/q-fever': '<article class="full"><div class="ecdc-side-nav"><a class="nav-link" href="/en/q-fever/facts">Disease information</a></div></article>',
            cfg['sample_url']: (FIXTURES / 'ecdc-detail.html').read_text(),
        }
        def get(url):
            calls.append(url)
            return self.response(pages[url], url, 'text/plain' if url.endswith('robots.txt') else 'text/html')
        result = crawl_education('ecdc', max_articles=1, get=get, sleep=lambda _: None)
        self.assertEqual(len(result.articles), 1)
        self.assertEqual(result.stats['failed'], 0)
        self.assertEqual(calls.count(cfg['sample_url']), 1)
        self.assertEqual(result.stats['requests'], 5)
        pages['https://www.ecdc.europa.eu/robots.txt'] += '\nDisallow: /en/q-fever/facts'
        calls.clear()
        result = crawl_education('ecdc', get=get, sleep=lambda _: None)
        self.assertEqual(result.articles, [])
        self.assertEqual(result.stats['excluded'], 1)
        self.assertNotIn(cfg['sample_url'], calls)
        self.assertFalse(result.stats['exhausted'], '全篇被排除不能假裝已完成正常無待辦')
        from scraper_education import main
        self.assertEqual(main(['--source', 'ecdc', '--limit', '1'], get=get,
                              sleep=lambda _: None, stdout=io.StringIO()), 1)
        from unittest.mock import patch
        from main_pipeline import job
        with patch('main_pipeline.EXPECTED_SOURCES', frozenset()), patch('main_pipeline.API_KEY', None):
            self.assertEqual(job(fetchers=[lambda: result], collection_factory=lambda: FakeCollection([]),
                                 vector_store_factory=FakeVectorStore, embed_fn=fake_embed_ok,
                                 env={'ECDC_EDUCATION_ENABLED': 'true'}), 1)
        pages['https://www.ecdc.europa.eu/robots.txt'] = 'User-agent: *\nDisallow: /admin/'
        end = crawl_education('ecdc', start_at=99, get=get, sleep=lambda _: None)
        self.assertEqual(end.articles, [])
        self.assertTrue(end.stats['exhausted'])

    def test_preview_cannot_load_db_or_paid_pipeline(self):
        import subprocess, sys
        code = '''import sys, importlib.abc
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in {'main_pipeline','pymongo','psycopg','claim_tagger'}:
   raise AssertionError('preview loaded a write dependency: '+fullname)
sys.meta_path.insert(0,Block())
import scraper_education
raise SystemExit(scraper_education.main(['--source','pmda']))
'''
        p = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 1)
        self.assertTrue(json.loads(p.stdout)['stats']['blocked'])
        self.assertNotIn('AssertionError', p.stderr)

    def test_general_education_is_not_tagger_source(self):
        from claim_tagger import SOURCES as TAGGER_SOURCES
        from scraper_education import EDUCATION_SOURCES
        self.assertFalse(EDUCATION_SOURCES & set(TAGGER_SOURCES))


if __name__ == '__main__':
    unittest.main()
