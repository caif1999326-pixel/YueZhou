import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from lxml import html
import fanqie_source as fq
import reliable_engine as engine


def document(payload):
    return html.fromstring('<html><script>window.__INITIAL_STATE__=' + json.dumps(payload) + ';</script></html>')


class FanqieTests(unittest.TestCase):
    def test_unknown_font_and_character_rejected(self):
        char = chr(next(iter(fq.TRANSLATION)))
        with self.assertRaisesRegex(ValueError, '编码已变化'):
            fq.decode_content('<p>' + char + '</p>', 'other.woff2')
        with self.assertRaisesRegex(ValueError, '未知字体'):
            fq.decode_content('<p>\ue000</p>', fq.FONT['font_file'])
        self.assertEqual(fq.decode_content('<p>' + char + '</p>', fq.FONT['font_file']), fq.TRANSLATION[ord(char)])

    def test_catalog_missing_duplicate_and_order_rejected(self):
        page = dict(bookId='123', bookName='测试', chapterTotal=2, chapterListWithVolume=[[
            dict(itemId='1', title='一', realChapterOrder=1), dict(itemId='2', title='二', realChapterOrder=2)]])
        fetch = Mock()
        def read():
            fetch.get.return_value = document(dict(page=page)), 'https://fanqienovel.com/page/123'
            return fq.catalog(fetch, 'https://fanqienovel.com/page/123')
        self.assertEqual(len(read()[0]), 2)
        page['chapterTotal'] = 3
        with self.assertRaisesRegex(ValueError, '总章数'): read()
        page['chapterTotal'] = 2
        page['chapterListWithVolume'][0][1]['realChapterOrder'] = 3
        with self.assertRaisesRegex(ValueError, '不连续'): read()
        page['chapterListWithVolume'][0][1]['itemId'] = '1'
        with self.assertRaisesRegex(ValueError, '重复'): read()

    def test_locked_wrong_and_truncated_chapters_rejected_cache_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(url='https://fanqienovel.com/page/123', rules=str(Path(__file__).with_name('rules.json')),
                output=tmp, workers=2, interval=0, retries=0, timeout=10, min_chars=100, refresh=False)
            downloader = engine.create_downloader(args)
            chapter = dict(url='https://fanqienovel.com/reader/1', title='正文', item_id='1', book_id='123', order=1)
            data = dict(itemId='1', bookId='123', title='正文', realChapterOrder=1, chapterWordNumber=200,
                        content='<p>' + '完整正文内容' * 40 + '</p>', needPay=1)
            def read():
                downloader.fetch.get = Mock(return_value=(document(dict(reader=dict(chapterData=data))), chapter['url']))
                return downloader.chapter(chapter)
            with self.assertRaisesRegex(ValueError, '付费'): read()
            data['needPay'] = 0
            data['bookId'] = '999'
            with self.assertRaisesRegex(ValueError, '不匹配'): read()
            data['bookId'] = '123'
            data['chapterWordNumber'] = 400
            with self.assertRaisesRegex(ValueError, '长度'): read()
            data['chapterWordNumber'] = 240
            self.assertFalse(read()[1])
            self.assertTrue(read()[1])
            downloader.fetch.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
