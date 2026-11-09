import os
import sys
import json
import re
import hashlib
import argparse
from typing import Optional, List, Dict, Any, Tuple

from services.svoyak_extractor import (
    clean_text,
    parse_theme,
    parse_answer_and_explanation,
    extract_svoyak_groups_from_message,
    extract_svoyak_dataset,
    generate_stable_id as generate_stable_id_v2,
)


def parse_answer_block(ans_raw: str) -> Tuple[str, List[str], Optional[str]]:
    """
    Legacy wrapper for answer block parsing.
    Maintains backward compatibility with earlier pipeline stages.
    """
    q_dummy = f"Savol\nJavob: {ans_raw}"
    _, prim, acc_list, expl, _ = parse_answer_and_explanation(q_dummy)
    alts = [a["text"] for a in acc_list if a.get("type") != "primary"]
    return prim or "", alts, expl


def generate_stable_id(msg_id: int, points: int, text: str, occurrence: int = 0) -> str:
    """
    Legacy MD5 ID generator maintained for backwards compatibility.
    """
    if occurrence == 0:
        raw_str = f"1.json_{msg_id}_{points}_{text[:50]}"
    else:
        raw_str = f"1.json_{msg_id}_{points}_{text[:50]}_{occurrence}"
    return hashlib.md5(raw_str.encode("utf-8")).hexdigest()


def generate_canonical_1(output_dir: str = "data/svoyak_extracted") -> Dict[str, Any]:
    """
    Authoritative Svoya Igra extractor for 1.json.
    Preserves:
    - Raw original message text.
    - Channel ID and message ID.
    - Theme/category title with prefixes and matrix clues.
    - 5-question theme groups with position tracking.
    - Point tiers (10, 20, 30, 40, 50).
    - Primary and accepted alternative answers with explanations.
    Outputs to dedicated directory without overwriting canonical_1.json.
    """
    print(f"Executing improved Svoya Igra extraction on '1.json' -> '{output_dir}'...")
    stats = extract_svoyak_dataset("1.json", output_dir=output_dir)
    return stats


def main():
    parser = argparse.ArgumentParser(description="ZakoWhat Svoya Igra Telegram Extractor")
    parser.add_argument("--input", "-i", default="1.json", help="Path to input Telegram JSON file (e.g. 1.json, 2.json)")
    parser.add_argument("--out-dir", "-o", default="data/svoyak_extracted", help="Output directory for extracted files")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: Input file '{args.input}' not found.")
        sys.exit(1)

    print(f"Processing '{args.input}'...")
    stats = extract_svoyak_dataset(args.input, output_dir=args.out_dir)
    print("\n--- EXTRACTION SUMMARY ---")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
