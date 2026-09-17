import json
import sys
from pathlib import Path

# Импорты из твоего проекта
from database import SessionLocal
from models import Question
from importer import QuestionValidator

def normalize_text_for_preview(text_field):
    """
    Нормализует текст для превью (например, из Telegram-структур),
    НЕ изменяя исходный JSON-объект.
    """
    if isinstance(text_field, list):
        parts = []
        for entity in text_field:
            if isinstance(entity, str):
                parts.append(entity)
            elif isinstance(entity, dict) and "text" in entity:
                parts.append(entity["text"])
        return "".join(parts)
    elif isinstance(text_field, str):
        return text_field
    return str(text_field)

def get_empty_stats():
    return {
        "total": 0,
        "valid": 0,
        "rejected": 0,
        "duplicate": 0,
        "would_insert": 0,
        "needs_review": 0,
        "ready": 0,
        "draft": 0,
        "compound": 0,
        "jeopardy": 0,
        "media_dependent": 0,
        "accepted_answers_empty": 0,
        "accepted_answers_multiple": 0,
        "accepted_answers_wrong_type": 0,
        "wrong_type_examples": []
    }

def print_stats(name, stats):
    print(f"\n{'='*60}")
    print(f"REPORT FOR: {name}")
    print(f"{'='*60}")
    print(f"Total Records:      {stats['total']}")
    print(f"Valid:              {stats['valid']}")
    print(f"Rejected:           {stats['rejected']}")
    print(f"Duplicate (DB):     {stats['duplicate']}")
    print(f"WOULD INSERT:       {stats['would_insert']}")
    print("-" * 30)
    print("Status Breakdown (from editorial.status):")
    print(f"  Ready:            {stats['ready']}")
    print(f"  Needs Review:     {stats['needs_review']}")
    print(f"  Draft:            {stats['draft']}")
    print("-" * 30)
    print("Flags & Types Breakdown:")
    print(f"  Compound:         {stats['compound']}")
    print(f"  Jeopardy:         {stats['jeopardy']}")
    print(f"  Media Dependent:  {stats['media_dependent']}")
    print("-" * 30)
    print("Accepted Answers Analysis:")
    print(f"  Empty []:         {stats['accepted_answers_empty']}")
    print(f"  1+ alternatives:  {stats['accepted_answers_multiple']}")
    print(f"  Wrong Type:       {stats['accepted_answers_wrong_type']}")
    
    if stats.get("wrong_type_examples"):
        print("\n  Wrong Type Examples (up to 3):")
        for ex in stats["wrong_type_examples"]:
            print(f"    -> {ex}")

def run_dry_run(file_paths):
    db = SessionLocal()
    total_stats = get_empty_stats()
    validator = QuestionValidator()  # Инициализация реального валидатора один раз
    
    try:
        for file_path in file_paths:
            path = Path(file_path)
            file_stats = get_empty_stats()
            
            if not path.exists():
                print(f"\nFile not found: {file_path}. Skipping.")
                continue
                
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            records = data.get("messages", data) if isinstance(data, dict) else data
            
            if not isinstance(records, list):
                print(f"\n[{path.name}] Invalid JSON structure: Expected a list of records.")
                continue

            rejected_records = []
            
            for index, record in enumerate(records):
                file_stats["total"] += 1
                total_stats["total"] += 1
                
                text_preview = normalize_text_for_preview(record.get("text", ""))[:60].replace('\n', ' ')

                # 1. Анализ Accepted Answers
                aa = record.get("accepted_answers")
                if aa is None or (isinstance(aa, list) and len(aa) == 0):
                    file_stats["accepted_answers_empty"] += 1
                    total_stats["accepted_answers_empty"] += 1
                elif isinstance(aa, list):
                    if len(aa) > 1:
                        file_stats["accepted_answers_multiple"] += 1
                        total_stats["accepted_answers_multiple"] += 1
                    
                    if not all(isinstance(ans, str) for ans in aa):
                        file_stats["accepted_answers_wrong_type"] += 1
                        total_stats["accepted_answers_wrong_type"] += 1
                        if len(file_stats["wrong_type_examples"]) < 3:
                            file_stats["wrong_type_examples"].append(str(aa)[:100])
                        if len(total_stats["wrong_type_examples"]) < 3:
                            total_stats["wrong_type_examples"].append(str(aa)[:100])
                else:
                    file_stats["accepted_answers_wrong_type"] += 1
                    total_stats["accepted_answers_wrong_type"] += 1
                    if len(file_stats["wrong_type_examples"]) < 3:
                        file_stats["wrong_type_examples"].append(str(aa)[:100])
                    if len(total_stats["wrong_type_examples"]) < 3:
                        total_stats["wrong_type_examples"].append(str(aa)[:100])

                # 2. Фактическая валидация
                normalized = validator.validate_and_normalize(record, path.name, index)
                
                if normalized is None:
                    file_stats["rejected"] += 1
                    total_stats["rejected"] += 1
                    rejected_records.append({
                        "filename": path.name,
                        "index": index,
                        "reason": "Rejected by QuestionValidator.validate_and_normalize (returned None)",
                        "text_preview": text_preview
                    })
                    continue

                # 3. Дедупликация (только SELECT через JSONB)
                source = record.get("source", {})
                channel_id = source.get("channel_id")
                q_msg_id = source.get("question_message_id")
                src_file = source.get("source_file")
                q_num = source.get("question_number")

                dup = None
                if channel_id and q_msg_id:
                    dup = db.query(Question).filter(
                        Question.source_meta["channel_id"].astext == str(channel_id),
                        Question.source_meta["question_message_id"].astext == str(q_msg_id)
                    ).first()
                elif src_file and q_num:
                    dup = db.query(Question).filter(
                        Question.source_meta["source_file"].astext == str(src_file),
                        Question.source_meta["question_number"].astext == str(q_num)
                    ).first()
                else:
                    file_stats["rejected"] += 1
                    total_stats["rejected"] += 1
                    rejected_records.append({
                        "filename": path.name,
                        "index": index,
                        "reason": "Missing canonical source constraints for deduplication",
                        "text_preview": text_preview
                    })
                    continue

                if dup:
                    file_stats["duplicate"] += 1
                    total_stats["duplicate"] += 1
                    continue

                # Если дошли сюда — запись готова к вставке
                file_stats["valid"] += 1
                total_stats["valid"] += 1
                file_stats["would_insert"] += 1
                total_stats["would_insert"] += 1

                # 4. Сбор статистики по статусам
                editorial = record.get("editorial", {})
                status = editorial.get("status")
                
                if status == "ready":
                    file_stats["ready"] += 1
                    total_stats["ready"] += 1
                elif status == "needs_review":
                    file_stats["needs_review"] += 1
                    total_stats["needs_review"] += 1
                else:
                    file_stats["draft"] += 1
                    total_stats["draft"] += 1

                # 5. Compound проверка
                if isinstance(record.get("compound"), dict):
                    file_stats["compound"] += 1
                    total_stats["compound"] += 1
                    
                # 6. Jeopardy проверка
                if record.get("round_type") == "jeopardy":
                    file_stats["jeopardy"] += 1
                    total_stats["jeopardy"] += 1

                # 7. Media проверка
                flags = editorial.get("flags", [])
                has_media = bool(record.get("media"))
                has_missing_media = bool(record.get("missing_media"))
                has_media_flag = isinstance(flags, list) and "media_dependent" in flags
                
                if has_media or has_missing_media or has_media_flag:
                    file_stats["media_dependent"] += 1
                    total_stats["media_dependent"] += 1

            # Вывод отчета по текущему файлу
            print_stats(path.name, file_stats)
            if rejected_records:
                print("\n  REJECTED RECORDS PREVIEW (Top 5):")
                for rej in rejected_records[:5]:
                    print(f"    [{rej['filename']}] Index {rej['index']} | {rej['reason']} | Text: {rej['text_preview']}...")

        # Вывод общего отчета
        print_stats("OVERALL TOTAL", total_stats)
        
    finally:
        # Гарантированное закрытие сессии. Строго read-only.
        db.close() 

if __name__ == "__main__":
    target_files = [
        "1.json", 
        "2.json", 
        "3.json", 
        "4.json", 
        "5.json", 
        "6.json",
        "7.json"
    ]
    print("Starting STRICT READ-ONLY mass import dry-run...")
    run_dry_run(target_files)
    print("\nDry-run completed successfully.")