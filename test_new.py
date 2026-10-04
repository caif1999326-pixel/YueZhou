import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import reliable_engine as engine
from search_engine import search_source, catalog_summary, supported, search_all
from test_reliable_engine import Fixture


class NewTests(unittest.TestCase):
    def test_disabled_sources_are_never_requested(self):
        rules = [dict(url='https://example.org', disabled=True, search={'url':'https://example.org/search'}), dict(url='https://example.net', search={'url':'https://example.net/search','disabled':True})]
        self.assertFalse(any(supported(r) for r in rules))
        with patch('search_engine.search_source') as request:
            search_all(rules,'西游记',lambda *a,**k:None)
            request.assert_not_called()

    def test_login_page_is_not_zero_results(self):
        rule=dict(url='https://example.org',search=dict(url='https://example.org/search',result='.row',bookName='a'))
        with patch('search_engine.requests.Session') as session:
            response=session.return_value.__enter__.return_value.request.return_value
            response.content='<meta charset="utf-8"><title>需要登录 - 搜搜书</title>'.encode()
            response.url='https://example.org/search'
            with self.assertRaisesRegex(ValueError,'要求登录'):
                search_source(rule,'西游记')

    def test_get_form_fields_use_query_parameters(self):
        rule=dict(url='https://example.org',search=dict(url='https://example.org/search',method='get',data='{keyword: %s}',result='.row',bookName='a'))
        with patch('search_engine.requests.Session') as session:
            request=session.return_value.__enter__.return_value.request
            request.return_value.content=b'<meta charset="utf-8"><title>Search</title>'
            request.return_value.url='https://example.org/search'
            search_source(rule,'西游记')
            self.assertEqual(request.call_args.kwargs['params'],{'keyword':'西游记'})
            self.assertIsNone(request.call_args.kwargs['data'])

    def test_catalog_count_uses_real_entries(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(count=7) as fixture:
            args = fixture.args(tmp)
            rule = json.loads(Path(args.rules).read_text('utf-8'))[0]
            summary = catalog_summary(rule, fixture.url)
            self.assertEqual(summary, dict(count=7, latest='第7章 标题7'))
            self.assertEqual(fixture.requests, {'/book': 1})
            self.assertFalse(Path(args.output).exists())

    def test_catalog_gap_is_not_shown_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(mode='gap') as fixture:
            args = fixture.args(tmp)
            rule = json.loads(Path(args.rules).read_text('utf-8'))[0]
            result = catalog_summary(rule, fixture.url)
            self.assertEqual(result['count'], 3)
            self.assertIn('缺少章号 2', result['warnings'][0])

    def test_chinese_numbers(self):
        for title, expected in [('第十一回', 11), ('第一百零二章', 102), ('第两千五百章', 2500), ('第１２０章', 120), ('序言', None)]:
            self.assertEqual(engine.number(title), expected)

    def test_title_truncation_requires_opt_in(self):
        short = '第二十四回 万寿山大仙留故友 五庄观行者'
        full = short + '窃人参'
        self.assertFalse(engine.title_matches(short, full))
        self.assertTrue(engine.title_matches(short, full, True))
        self.assertFalse(engine.title_matches(short, full.replace('二十四', '二十五'), True))
        self.assertFalse(engine.title_matches('第一章', '第一章 不同标题', True))

    def test_search_parse_and_query(self):
        rule = dict(url='https://example.org', name='测试源', search=dict(url='https://example.org/search?q=%s', method='get', result='.row', bookName='h3 a', author='.author'))
        page = '<meta charset="utf-8"><div class="row"><h3><a href="/book/1">西游记</a></h3><p class="author">吴承恩</p></div>'
        with patch('search_engine.requests.Session') as session:
            response = session.return_value.__enter__.return_value.request.return_value
            response.content = page.encode()
            response.url = 'https://example.org/search'
            result = search_source(rule, '西游记')
            self.assertEqual(result[0]['title'], '西游记')
            self.assertEqual(result[0]['url'], 'https://example.org/book/1')
            self.assertEqual(result[0]['author'], '吴承恩')


if __name__ == '__main__':
    unittest.main(verbosity=2)
