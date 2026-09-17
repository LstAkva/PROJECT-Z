import json
import logging
import os
from database import SessionLocal
from models import Quiz, QuizVersion, Round, Question, AcceptedAnswer

class QuestionValidator:
    def __init__(self):
        self.stats = {
            "total_processed": 0,
            "eligible": 0,
            "rejected": 0,
            "needs_review": 0
        }
        self.rejected_records = []

    def _reject(self, filename, index, record, reason):
        self.stats["rejected"] += 1
        raw_text = record.get('text', '') or ''
        text_preview = str(raw_text)[:50].replace('\n', ' ') + '...'
        
        self.rejected_records.append({
            "filename": filename,
            "index": index,
            "text_preview": text_preview,
            "reason": reason
        })

    def validate_and_normalize(self, record, filename, index):
        self.stats["total_processed"] += 1

        # Извлекаем вложенный словарь source согласно canonical schema
        source_data = record.get('source', {})
        if not isinstance(source_data, dict):
            source_data = {}

        # Rule 1: Provenance Check (Strict)
        channel_id = source_data.get('channel_id')
        msg_id = source_data.get('question_message_id')
        source_file = source_data.get('source_file')
        q_num = source_data.get('question_number')

        has_tg_prov = bool(channel_id and msg_id)
        has_file_prov = bool(source_file and q_num)
        
        if not (has_tg_prov or has_file_prov):
            self._reject(filename, index, record, "missing_provenance")
            return None

        # Rule 2: Primary Answer Check
        primary_answer = record.get('primary_answer')
        if not primary_answer or not str(primary_answer).strip():
            self._reject(filename, index, record, "missing_primary_answer")
            return None

        # Обработка Editorial и флагов медиа
        editorial_data = record.get('editorial', {})
        if not isinstance(editorial_data, dict):
            editorial_data = {}
            
        needs_review = False
        if editorial_data.get('status') == 'needs_review':
            needs_review = True
        if 'missing_media' in editorial_data.get('flags', []):
            needs_review = True

        # Rule 3 & 4: Safe Extraction & Type Coercion для media
        media_field = record.get('media')
        if isinstance(media_field, list):
            media_field = ", ".join(str(m) for m in media_field)
            needs_review = True
        elif record.get('missing_media'):
            needs_review = True

        if needs_review:
            self.stats["needs_review"] += 1

        self.stats["eligible"] += 1

        # Возвращаем нормализованную запись
        return {
            "text": record.get('text', ''),
            "primary_answer": str(primary_answer).strip(),
            "source": {
                "channel_name": source_data.get('channel_name'),
                "channel_id": channel_id,
                "question_message_id": msg_id,
                "source_file": source_file,
                "question_number": q_num
            },
            "accepted_answers": record.get('accepted_answers', []),
            "explanation": record.get('explanation'),
            "editorial": editorial_data,
            "compound": record.get('compound', False),
            "points": record.get('points'),
            "category": record.get('category'),
            "media": media_field,
            "needs_review": needs_review,
            "round_type": record.get('round_type')
        }

def get_or_create_import_round(db):
    """Создает технический квиз и раунд для импортированных вопросов, если их нет."""
    quiz = db.query(Quiz).filter_by(title="Telegram Imports Database").first()
    if not quiz:
        quiz = Quiz(title="Telegram Imports Database", description="База вопросов из ТГ")
        db.add(quiz)
        db.flush()

    version = db.query(QuizVersion).filter_by(quiz_id=quiz.id).first()
    if not version:
        version = QuizVersion(quiz_id=quiz.id, version_number=1, status="draft")
        db.add(version)
        db.flush()

    import_round = db.query(Round).filter_by(quiz_version_id=version.id, round_type="import_buffer").first()
    if not import_round:
        import_round = Round(quiz_version_id=version.id, sequence=1, round_type="import_buffer")
        db.add(import_round)
        db.flush()

    return import_round.id

def import_questions(json_filepath):
    db = SessionLocal()
    validator = QuestionValidator()
    filename = os.path.basename(json_filepath)
    
    stats = {
        "total": 0, "added": 0, "skipped_duplicate": 0, "skipped_rejected": 0,
        "needs_review": 0, "ready": 0, "draft": 0,
        "compound": 0, "jeopardy": 0, "media_dependent": 0
    }
    
    try:
        with open(json_filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        stats["total"] = len(data)
        round_id = get_or_create_import_round(db)
        max_seq = db.query(Question).filter_by(round_id=round_id).count()

        for index, raw_item in enumerate(data):
            # 1. Валидация и нормализация записи
            item = validator.validate_and_normalize(raw_item, filename, index)
            
            # Отбраковываем записи без provenance или primary_answer (ДО БД)
            if not item:
                stats["skipped_rejected"] += 1
                continue

            source_data = item["source"]
            channel_id = source_data.get("channel_id")
            message_id = source_data.get("question_message_id")
            source_file = source_data.get("source_file")
            question_number = source_data.get("question_number")
            
            # 2. Усиленная дедупликация (по нормализованным данным)
            existing_q = None
            if channel_id and message_id:
                existing_q = db.query(Question).filter(
                    Question.source_meta['channel_id'].astext == str(channel_id),
                    Question.source_meta['question_message_id'].astext == str(message_id)
                ).first()
            elif source_file and question_number:
                existing_q = db.query(Question).filter(
                    Question.source_meta['source_file'].astext == str(source_file),
                    Question.source_meta['question_number'].astext == str(question_number)
                ).first()

            if existing_q:
                stats["skipped_duplicate"] += 1
                continue

            # 3. Подсчет статистики и определение финального статуса
            canonical_status = item["editorial"].get("status")
            flags = item["editorial"].get("flags", [])
            
            # Если валидатор поставил флаг, принудительно отправляем на ревью
            if item["needs_review"]:
                final_status = "needs_review"
            elif canonical_status in ["ready", "needs_review"]:
                final_status = canonical_status
            else:
                final_status = "draft"
            
            stats[final_status] += 1
            
            compound_data = item.get("compound")
            if compound_data and isinstance(compound_data, dict):
                stats["compound"] += 1
                c_group_id = compound_data.get("group_id")
                c_type = compound_data.get("type")
            else:
                c_group_id = None
                c_type = None
                
            if item.get("round_type") == "jeopardy": stats["jeopardy"] += 1
            if "media_dependent" in flags or "missing_media" in flags: stats["media_dependent"] += 1

            max_seq += 1
            
            # 4. Создание вопроса
            new_question = Question(
                round_id=round_id,
                sequence=max_seq,
                text=item["text"],
                explanation=item.get("explanation"),
                status=final_status,
                source_meta=source_data,
                points=item.get("points"),
                category=item.get("category"),
                compound_group_id=c_group_id,
                compound_type=c_type
            )
            
            db.add(new_question)
            db.flush()

            # 5. Primary и Accepted answers
            primary_ans = AcceptedAnswer(
                question_id=new_question.id,
                answer_text=item["primary_answer"],
                is_primary=True
            )
            db.add(primary_ans)

            for alt_text in item.get("accepted_answers", []):
                alt_ans = AcceptedAnswer(
                    question_id=new_question.id,
                    answer_text=alt_text,
                    is_primary=False
                )
                db.add(alt_ans)
            
            stats["added"] += 1

        # 6. Транзакционная фиксация
        db.commit()
        
        # 7. Вывод совмещенного отчета
        print(f"\n=== VALIDATION REPORT [{filename}] ===")
        print(f"Total Processed: {validator.stats['total_processed']}")
        print(f"Eligible for DB: {validator.stats['eligible']}")
        print(f"Rejected       : {validator.stats['rejected']}")
        
        if validator.rejected_records:
            print("\n--- REJECTED RECORDS DETAILS ---")
            for req in validator.rejected_records:
                print(f"[Index: {req['index']}] REASON: {req['reason']}")
                print(f"Preview: {req['text_preview']}\n")

        print("\n=== DB IMPORT SUMMARY ===")
        print(f"Total records      : {stats['total']}")
        print(f"Added              : {stats['added']}")
        print(f"Skipped (duplicate): {stats['skipped_duplicate']}")
        print(f"Skipped (rejected) : {stats['skipped_rejected']}")
        print("--- Status Details ---")
        print(f"Needs review       : {stats['needs_review']}")
        print(f"Ready              : {stats['ready']}")
        print(f"Draft (fallback)   : {stats['draft']}")
        print("--- Content Types ---")
        print(f"Compound records   : {stats['compound']}")
        print(f"Jeopardy records   : {stats['jeopardy']}")
        print(f"Media-dependent    : {stats['media_dependent']}")
        print("======================\n")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка импорта (откат транзакции): {e}\n")
    finally:
        db.close()

if __name__ == "__main__":
    import_questions("result_2.json")