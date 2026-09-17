import unittest
from api.play import normalize_uzbek_latin

class TestUzbekNormalization(unittest.TestCase):
    def test_apostrophe_variants(self):
        # All variants should collapse to standard single quote '
        variants = ["O'qituvchi", "O’qituvchi", "Oʻqituvchi", "Oʼqituvchi", "O`qituvchi"]
        for v in variants:
            self.assertEqual(normalize_uzbek_latin(v), "o'qituvchi")

    def test_capitalization_and_spacing(self):
        self.assertEqual(normalize_uzbek_latin("  TOSHKENT  "), "toshkent")
        self.assertEqual(normalize_uzbek_latin("Alisher   Navoiy"), "alisher navoiy")

    def test_internal_hyphens(self):
        self.assertEqual(normalize_uzbek_latin("Katta-qo'rg'on"), "katta-qo'rg'on")

    def test_surrounding_punctuation(self):
        self.assertEqual(normalize_uzbek_latin('"Toshkent!"'), "toshkent")
        self.assertEqual(normalize_uzbek_latin("...samarqand?"), "samarqand")
        self.assertEqual(normalize_uzbek_latin("(Buxoro)"), "buxoro")

if __name__ == '__main__':
    unittest.main()