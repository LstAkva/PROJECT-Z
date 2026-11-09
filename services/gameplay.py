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
    text = re.sub(r"[‘’ʻʼ`]", "'", text)
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


# ==============================================================================
# CONSERVATIVE DETERMINISTIC TYPO TOLERANCE (HYBRID ANSWER EVALUATION)
# ==============================================================================

def damerau_levenshtein_distance(s1: str, s2: str) -> int:
    """
    Computes standard Damerau-Levenshtein distance allowing insertions, deletions,
    substitutions, and transpositions of adjacent characters.
    Deterministic O(len(s1) * len(s2)) implementation.
    """
    d = {}
    len1, len2 = len(s1), len(s2)
    for i in range(-1, len1 + 1):
        d[(i, -1)] = i + 1
    for j in range(-1, len2 + 1):
        d[(-1, j)] = j + 1

    for i in range(len1):
        for j in range(len2):
            cost = 0 if s1[i] == s2[j] else 1
            d[(i, j)] = min(
                d[(i - 1, j)] + 1,       # deletion
                d[(i, j - 1)] + 1,       # insertion
                d[(i - 1, j - 1)] + cost  # substitution
            )
            if i > 0 and j > 0 and s1[i] == s2[j - 1] and s1[i - 1] == s2[j]:
                d[(i, j)] = min(d[(i, j)], d[(i - 2, j - 2)] + 1)  # transposition

    return d[(len1 - 1, len2 - 1)]


def is_conservative_typo_match(submitted: str, target: str) -> bool:
    """
    Evaluates whether a normalized player submission is a conservative, deterministic
    minor typographical slip of the accepted target answer.

    STRICT SAFETY RULES:
    1. Word Count Equivalence: len(submitted.split()) == len(target.split()).
       A typo never adds or removes whole words or spaces.
    2. Minimum Word Length Threshold:
       - Single-word answers require len(target) >= 6.
       - Short words (length <= 5, e.g. 'xum', 'neft', 'jenga', 'bisot', 'atlas')
         STRICTLY REQUIRE 100% exact matching; edit distance is never tolerated on
         short roots to prevent accidental semantic drift.
    3. Root Initial Anchor:
       - The first character of each word must match identically. In trivia/Zakovat,
         entities and concepts are anchored by their initial letter (e.g. 'Meksika'
         vs 'Teksika' is a different entity, not a typo).
    4. Strict Damerau-Levenshtein Edit Distance <= 1:
       - Single word: distance == 1 (single transposition, substitution, insertion, or deletion).
       - Multi-word: exactly one word in the phrase has distance == 1 (with len >= 5 and
         matching initial char), while all other words in the phrase must match exactly (distance == 0).
    """
    # 0. Strict Numeric & Date Safety:
    # Numbers, dates, years, quantities, and answers containing digits must match exactly.
    # Typo / edit distance tolerance is strictly forbidden on numeric data to prevent false acceptance (e.g. 1991 vs 1992).
    if any(ch.isdigit() for ch in submitted) or any(ch.isdigit() for ch in target):
        return False

    sub_words = submitted.strip().split()
    tgt_words = target.strip().split()

    if len(sub_words) != len(tgt_words) or not sub_words:
        return False

    if len(sub_words) == 1:
        w_sub, w_tgt = sub_words[0], tgt_words[0]
        if len(w_tgt) < 6:
            return False
        if w_sub[0] != w_tgt[0]:
            return False
        return damerau_levenshtein_distance(w_sub, w_tgt) == 1

    # Multi-word phrase
    typo_word_count = 0
    for w_sub, w_tgt in zip(sub_words, tgt_words):
        if w_sub == w_tgt:
            continue
        if len(w_tgt) < 5 or w_sub[0] != w_tgt[0]:
            return False
        if damerau_levenshtein_distance(w_sub, w_tgt) == 1:
            typo_word_count += 1
        else:
            return False

    return typo_word_count == 1


# ==============================================================================
# PRODUCT GAME MODES & FUTURE EXTENSION POINTS
# ==============================================================================

# Active supported public modes
SUPPORTED_GAME_MODES = ["classic_zakovat", "svoyak"]

# Clean minimal future extension point: ZakoWhat lightweight wheel mini-game
# (Reserved for future milestone, not implemented in RC1)
FUTURE_MINI_GAMES = ["wheel"]
