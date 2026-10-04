import copy
import unittest

import reliable_engine as engine


def entries(numbers):
    return [dict(title='第%s章 测试%s' % (n, i), url='https://example.org/c%s' % i)
            for i, n in enumerate(numbers)]


def ranges(chapters):
    return [(g['start'], g['end'], g['count']) for g in engine.catalog_gaps(chapters)]


class CatalogOrderTests(unittest.TestCase):
    def test_out_of_order_existing_chapters_are_not_missing(self):
        chapters = entries([1, 3, 2, 4])
        original = copy.deepcopy(chapters)
        self.assertEqual(ranges(chapters), [])
        self.assertTrue(any('顺序或章号异常' in item for item in engine.catalog_warnings(chapters)))
        self.assertEqual(chapters, original)

    def test_true_gap_is_preserved_and_overlapping_ranges_are_deduplicated(self):
        self.assertEqual(ranges(entries([127, 129, 126, 130])), [(128, 128, 1)])
        self.assertEqual(ranges(entries([1, 6, 2, 9])), [(3, 5, 3), (7, 8, 2)])

    def test_repeated_numbers_are_warnings_not_deleted_chapters(self):
        chapters = entries([1, 2, 2, 3])
        self.assertEqual(ranges(chapters), [])
        self.assertTrue(any('重复章号 2' in item for item in engine.catalog_warnings(chapters)))
        self.assertEqual(len(chapters), 4)

    def test_volume_reset_does_not_hide_an_earlier_volume_gap(self):
        self.assertEqual(ranges(entries([1, 3, 1, 2])), [(2, 2, 1)])
        self.assertEqual(ranges(entries([1, 3, 1, 3])), [(2, 2, 1), (2, 2, 1)])
        chapters = entries([1, 3]) + [dict(title='第二卷 新开始')] + entries([1, 2])
        self.assertEqual(ranges(chapters), [(2, 2, 1)])
        self.assertFalse(any('重复章号' in item for item in engine.catalog_warnings(entries([1, 2, 1, 2]))))

    def test_isolated_typo_not_rediscovered_by_a_later_large_jump(self):
        chapters = entries([1, 2, 4, 9, 6, 3, 10])
        # 9 between 4 and 6 represents ambiguous chapter 5. A later 3->10
        # jump must not invent a missing 5 after this existing typo treatment.
        self.assertEqual(ranges(chapters), [(7, 8, 2)])

    def test_real_manifest_number_pattern_has_eight_suspect_labels_not_724(self):
        numbers = [n for n in range(1, 1241) if n not in (128, 283, 297, 312)]
        for original, replacement in {537: 357, 569: 568, 624: 924, 625: 925,
                                      626: 926, 658: 578, 839: 829}.items():
            numbers[numbers.index(original)] = replacement
        numbers.remove(162)
        numbers.insert(numbers.index(536) + 2, 162)
        numbers.insert(numbers.index(539) + 1, 495)
        numbers.insert(numbers.index(51) + 1, 51)
        chapters = entries(numbers)
        self.assertEqual(len(chapters), 1238)
        self.assertEqual(ranges(chapters), [(128, 128, 1), (283, 283, 1), (297, 297, 1),
                                           (312, 312, 1), (537, 537, 1), (624, 626, 3)])
        self.assertEqual(sum(g['count'] for g in engine.catalog_gaps(chapters)), 8)

    def test_huge_label_jump_uses_compact_range(self):
        largest = 10 ** 12
        self.assertEqual(ranges(entries([1, largest])), [(2, largest - 1, largest - 2)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
