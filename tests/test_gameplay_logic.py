from services.gameplay import (
    normalize_uzbek_latin,
    extract_uzbek_boundary_letters,
    verify_zanjir_chain,
)


def test_apostrophe_normalization_variants():
    variants = ["O'qituvchi", "O’qituvchi", "Oʻqituvchi", "Oʼqituvchi", "O`qituvchi"]
    for v in variants:
        assert normalize_uzbek_latin(v) == "o'qituvchi"


def test_spacing_and_casing():
    assert normalize_uzbek_latin("  TOSHKENT  ") == "toshkent"
    assert normalize_uzbek_latin("Alisher   Navoiy") == "alisher navoiy"


def test_internal_hyphens_preserved():
    assert normalize_uzbek_latin("Katta-qo'rg'on") == "katta-qo'rg'on"


def test_surrounding_punctuation_stripped():
    assert normalize_uzbek_latin('"Toshkent!"') == "toshkent"
    assert normalize_uzbek_latin("...samarqand?") == "samarqand"
    assert normalize_uzbek_latin("(Buxoro)") == "buxoro"


def test_extract_uzbek_start_letters():
    assert "o'" in extract_uzbek_boundary_letters("O'qituvchi", position="start")
    assert "g'" in extract_uzbek_boundary_letters("G'alaba", position="start")
    assert "sh" in extract_uzbek_boundary_letters("Shahar", position="start")
    assert "ch" in extract_uzbek_boundary_letters("Chinor", position="start")
    assert "t" in extract_uzbek_boundary_letters("Toshkent", position="start")


def test_extract_uzbek_end_letters():
    assert "o'" in extract_uzbek_boundary_letters("Avto'", position="end")
    assert "sh" in extract_uzbek_boundary_letters("Quyosh", position="end")
    assert "y" in extract_uzbek_boundary_letters("Navoiy", position="end")
    assert "t" in extract_uzbek_boundary_letters("Toshkent", position="end")


def test_zanjir_standard_consonant_chain():
    # Navoiy ends in 'y', Yulduz starts with 'y'
    assert verify_zanjir_chain("Alisher Navoiy", "Yulduz") is True
    # Yulduz ends in 'z', Zanjir starts with 'z'
    assert verify_zanjir_chain("Yulduz", "Zanjir") is True


def test_zanjir_uzbek_modified_vowel_chain():
    # Avto' ends in "o'", O'qituvchi starts with "o'"
    assert verify_zanjir_chain("Avto'", "O'qituvchi") is True
    # Should not match standard non-modified 'o' with "o'"
    assert verify_zanjir_chain("Avto", "O'qituvchi") is False


def test_zanjir_digraph_chain():
    # Quyosh ends in 'sh', Shahar starts with 'sh'
    assert verify_zanjir_chain("Quyosh", "Shahar") is True
    # If author chained on 's': Quyosh (sh) -> Samarqand (s)
    assert verify_zanjir_chain("Quyosh", "Samarqand") is True


def test_zanjir_invalid_chain_rejected():
    assert verify_zanjir_chain("Toshkent", "Buxoro") is False
    assert verify_zanjir_chain("", "Samarqand") is False
    assert verify_zanjir_chain("Navoiy", "") is False
