import re
from typing import Optional, Set

def normalize_uzbek_latin(text: str) -> str:
    """
    Normalizes specifically for Uzbek Latin ZakoWhat content.
    Collapses apostrophe variants (’, ʻ, ʼ, `) into standard single quote (').
    Strips punctuation, handles hyphens, and compresses whitespace.
    """
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[’ʻʼ`]", "'", text)
    text = text.strip('.,!?"()[]{}:;* ')
    text = re.sub(r'\s+', ' ', text)
    return text


def extract_uzbek_boundary_letters(text: str, position: str = "start") -> Set[str]:
    """
    Extracts valid letter representations at the start or end of an Uzbek Latin word.
    Handles Uzbek-specific letter units:
    - Vowels with modifiers: O', G'
    - Digraphs: Sh, Ch
    - Standard alphabet characters
    
    Returns a set of acceptable matching representations (e.g. {"sh", "s"} for digraph boundary).
    """
    norm = normalize_uzbek_latin(text)
    if not norm:
        return set()

    results: Set[str] = set()

    if position == "start":
        # 1. Check for two-character modified letters: o', g'
        if len(norm) >= 2 and norm[:2] in ("o'", "g'"):
            results.add(norm[:2])
            return results

        # 2. Check for digraphs: sh, ch
        if len(norm) >= 2 and norm[:2] in ("sh", "ch"):
            results.add(norm[:2])
            # Also allow fallback to base single character to avoid penalizing players
            # if the quiz author chained on the single character
            results.add(norm[0])
            return results

        # 3. Standard single character
        results.add(norm[0])
        return results

    elif position == "end":
        # 1. Check for ending with o', g'
        if len(norm) >= 2 and norm[-2:] in ("o'", "g'"):
            results.add(norm[-2:])
            return results

        # 2. Check for digraphs: sh, ch
        if len(norm) >= 2 and norm[-2:] in ("sh", "ch"):
            results.add(norm[-2:])
            results.add(norm[-2])
            return results

        # 3. If word ends with an apostrophe preceded by another character
        # Strip trailing apostrophe if not part of o'/g'
        stripped = norm.rstrip("'")
        if stripped:
            if len(stripped) >= 2 and stripped[-2:] in ("o'", "g'"):
                results.add(stripped[-2:])
                return results
            if len(stripped) >= 2 and stripped[-2:] in ("sh", "ch"):
                results.add(stripped[-2:])
                results.add(stripped[-2])
                return results
            results.add(stripped[-1])
            return results

        # Fallback to last character
        results.add(norm[-1])
        return results

    raise ValueError(f"Unknown position: {position}. Expected 'start' or 'end'.")


def verify_zanjir_chain(prev_primary_answer: str, current_answer: str) -> bool:
    """
    Verifies that current_answer starts with the letter that prev_primary_answer ends with,
    adhering to Uzbek Latin orthography.
    """
    prev_end_letters = extract_uzbek_boundary_letters(prev_primary_answer, position="end")
    curr_start_letters = extract_uzbek_boundary_letters(current_answer, position="start")

    if not prev_end_letters or not curr_start_letters:
        return False

    # Check for any overlapping valid letter representation
    return bool(prev_end_letters.intersection(curr_start_letters))


TRUE_SYNONYMS = {"rost", "ha", "to'g'ri", "togri", "true", "1"}
FALSE_SYNONYMS = {"yolg'on", "yolgon", "yo'q", "yoq", "noto'g'ri", "notogri", "false", "0"}


def is_true_false_match(submitted_clean: str, accepted_clean: str) -> bool:
    """
    Matches True/False submissions allowing standard Uzbek equivalents
    ('ha'/'rost'/'to'g'ri'/'true' vs 'yo'q'/'yolg'on'/'noto'g'ri'/'false').
    """
    if submitted_clean == accepted_clean:
        return True
    if accepted_clean in TRUE_SYNONYMS and submitted_clean in TRUE_SYNONYMS:
        return True
    if accepted_clean in FALSE_SYNONYMS and submitted_clean in FALSE_SYNONYMS:
        return True
    return False


def get_zanjir_hint(prev_primary_answer: str) -> Optional[str]:
    """
    Returns the uppercase letter representation for the Zanjir starting prompt:
    'Javobingiz [X] harfi bilan boshlanishi kerak'.
    """
    if not prev_primary_answer:
        return None
    letters = extract_uzbek_boundary_letters(prev_primary_answer, position="end")
    if not letters:
        return None
    # Prefer multi-character letter (e.g. O', G', Sh, Ch) if available
    sorted_letters = sorted(letters, key=lambda x: len(x), reverse=True)
    return sorted_letters[0].upper()
