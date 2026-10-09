"""來源限定入庫工具：離線 DB／embedding 假件。"""
import unittest
from unittest.mock import Mock
from test_system import FakeCollection, FakeVectorStore
from test_education import FIXTURES


class TestEducationIngest(unittest.TestCase):
    def article(self):
        from scraper_education import parse_article, SOURCES
        return parse_article('ecdc', (FIXTURES / 'ecdc-detail.html').read_text(), SOURCES['ecdc']['sample_url'])

    def test_write_repeats_without_embedding_and_reports_metadata(self):
        from ingest_education import ingest_batch
        c = FakeCollection([]); s = FakeVectorStore(); a = self.article()
        embed = Mock(return_value=[0.01] * 3072)
        report = ingest_batch([a], c, s, embed_fn=embed, max_embedding_calls=10)
        self.assertEqual(report['new_or_rewritten_articles'], 1)
        self.assertEqual(report['embedding_calls'], 1)
        embed.reset_mock()
        report = ingest_batch([a], c, s, embed_fn=embed, max_embedding_calls=10)
        self.assertEqual(report['new_or_rewritten_articles'], 0)
        embed.assert_not_called()
        self.assertTrue(all(d['attribution'] == a['attribution'] for d in c.docs))

    def test_invalid_source_or_license_rejected_before_embedding(self):
        from ingest_education import ingest_batch
        for a in [dict(self.article(), license='wrong'), dict(self.article(), source='PMDA 用藥安全衛教')]:
            c = FakeCollection([]); embed = Mock()
            with self.assertRaises(ValueError):
                ingest_batch([a], c, FakeVectorStore(), embed_fn=embed)
            embed.assert_not_called()
            self.assertEqual(c.docs, [])

    def test_embedding_budget_stops_without_partial_article(self):
        from ingest_education import ingest_batch
        a = dict(self.article(), content='衛教測試。' * 300)
        c = FakeCollection([]); embed = Mock(return_value=[0.01] * 3072)
        report = ingest_batch([a], c, FakeVectorStore(), embed_fn=embed, max_embedding_calls=1)
        self.assertEqual(report['embedding_calls'], 1)
        self.assertEqual(c.docs, [])
        self.assertTrue(report['write_failed'])


if __name__ == '__main__':
    unittest.main()
