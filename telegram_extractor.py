import json
import re
import hashlib

def clean_text(text):
    if not text:
        return ""
    if isinstance(text, list):
        parts = []
        for item in text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        text = "".join(parts)
    return text.strip()

def parse_answer_block(ans_raw):
    ans_raw = ans_raw.strip()
    explanation = None
    accepted_answers = []
    
    izoh_match = re.split(r'\n\s*Izoh:\s*', ans_raw, flags=re.IGNORECASE)
    main_text = izoh_match[0].strip()
    if len(izoh_match) > 1:
        explanation = "Izoh: " + izoh_match[1].strip()
        
    qabul_parts = re.split(r'\bQabul:\s*', main_text, flags=re.IGNORECASE)
    
    primary_answer = qabul_parts[0].strip()
    if len(qabul_parts) > 1:
        accepted_answers = [p.strip() for p in qabul_parts[1:] if p.strip()]
        
    if accepted_answers and primary_answer.endswith('('):
        last_acc = accepted_answers[-1]
        if last_acc.endswith(').'):
            primary_answer = primary_answer[:-1].strip()
            accepted_answers[-1] = last_acc[:-2].strip()
        elif last_acc.endswith(')'):
            primary_answer = primary_answer[:-1].strip()
            accepted_answers[-1] = last_acc[:-1].strip()
            
    return primary_answer, accepted_answers, explanation

def generate_stable_id(msg_id, points, text, occurrence=0):
    if occurrence == 0:
        raw_str = f"1.json_{msg_id}_{points}_{text[:50]}"
    else:
        raw_str = f"1.json_{msg_id}_{points}_{text[:50]}_{occurrence}"
    return hashlib.md5(raw_str.encode('utf-8')).hexdigest()

def generate_canonical_1():
    with open("1.json", "r", encoding="utf-8") as f:
        data = json.load(f)
        
    # 1. Read root-level channel/source metadata
    channel_id_val = str(data.get("id"))
    channel_name_val = data.get("name")
    
    print("1. Root-level channel/source metadata from 1.json:")
    print(f"   - id: {data.get('id')}")
    print(f"   - name: {channel_name_val}")
    print(f"   - type: {data.get('type')}")
    
    # 2. Exact value used as channel_id
    print("\n2. Exact value that will be used as channel_id:")
    print(f"   - channel_id: {channel_id_val!r}")
    
    messages = data.get("messages", [])
    canonical_records = []
    
    seen_exact_records = set()
    id_occurrences = {}
    seen_ids = set()
    
    stats = {
        "total_canonical": 0,
        "exact_duplicates_skipped": 0,
        "with_qabul": 0,
        "with_explanation": 0,
        "missing_primary": 0,
        "missing_channel_id": 0,
        "missing_question_message_id": 0,
        "duplicate_ids_remaining": 0
    }
    
    sample_msg_201_source = None
    
    for msg in messages:
        msg_id = msg.get("id")
        text = clean_text(msg.get("text", ""))
        if not text:
            continue
            
        blocks = re.split(r'\n(?=(?:10|20|30|40|50)[\.\)]\s)', text)
        
        for block in blocks:
            block_stripped = block.strip()
            if not block_stripped:
                continue
                
            m_q = re.match(r'^(?:(10|20|30|40|50)[\.\)]\s*)(.+)', block_stripped, re.DOTALL)
            if not m_q:
                continue
                
            points = int(m_q.group(1))
            q_and_a = m_q.group(2)
            
            parts = re.split(r'\n\s*(?:J:|Javob:)\s*', q_and_a, flags=re.IGNORECASE)
            if len(parts) < 2:
                parts = re.split(r'\b(?:J:|Javob:)\s*', q_and_a, flags=re.IGNORECASE)
            
            if len(parts) < 2:
                continue
                
            question_text = parts[0].strip()
            answer_raw = parts[1].strip()
            
            primary_ans, qabul_ans, explanation = parse_answer_block(answer_raw)
            
            dedup_key = (msg_id, points, question_text, primary_ans, tuple(qabul_ans), explanation)
            if dedup_key in seen_exact_records:
                stats["exact_duplicates_skipped"] += 1
                continue
            seen_exact_records.add(dedup_key)
            
            base_raw_str = f"1.json_{msg_id}_{points}_{question_text[:50]}"
            occurrence = id_occurrences.get(base_raw_str, 0)
            id_occurrences[base_raw_str] = occurrence + 1
            
            record_id = generate_stable_id(msg_id, points, question_text, occurrence)
            
            if record_id in seen_ids:
                stats["duplicate_ids_remaining"] += 1
            seen_ids.add(record_id)
            
            accepted_list = [{"text": primary_ans, "type": "primary"}]
            for qa in qabul_ans:
                accepted_list.append({"text": qa, "type": "zachot"})
                
            source_obj = {
                "source_name": "Svoya Igra baza",
                "pack": None,
                "question_number": None,
                "source_page": None,
                "channel_id": channel_id_val,
                "question_message_id": msg_id
            }
            
            if msg_id == 201 and sample_msg_201_source is None:
                sample_msg_201_source = source_obj
                
            record = {
                "id": record_id,
                "text": question_text,
                "primary_answer": primary_ans,
                "accepted_answers": accepted_list,
                "explanation": explanation,
                "round_type": "jeopardy",
                "media": [],
                "compound": None,
                "tags": [],
                "points": points,
                "category": None,
                "source": source_obj,
                "editorial": {
                    "status": "needs_review",
                    "notes": None
                }
            }
            
            if not primary_ans:
                stats["missing_primary"] += 1
            if not channel_id_val:
                stats["missing_channel_id"] += 1
            if not msg_id:
                stats["missing_question_message_id"] += 1
                
            stats["total_canonical"] += 1
            canonical_records.append(record)
            
    with open("canonical_1.json", "w", encoding="utf-8") as f:
        json.dump(canonical_records, f, ensure_ascii=False, indent=2)
        
    # 3. Print one example resulting source object for message 201
    print("\n3. Example resulting source object for message 201:")
    print(json.dumps(sample_msg_201_source, ensure_ascii=False, indent=4))
        
    return stats

if __name__ == "__main__":
    print("Running Telegram Extractor with updated Provenance contract...\n")
    stats = generate_canonical_1()
    
    print("\n--- GENERATION STATISTICS ---")
    print(f"Total canonical records: {stats['total_canonical']}")
    print(f"Exact duplicates skipped: {stats['exact_duplicates_skipped']}")
    print(f"Duplicate IDs remaining: {stats['duplicate_ids_remaining']}")
    print(f"Missing primary_answer: {stats['missing_primary']}")
    print(f"Missing channel_id: {stats['missing_channel_id']}")
    print(f"Missing question_message_id: {stats['missing_question_message_id']}")