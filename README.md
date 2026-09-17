# ZakoWhat — Milliy Intellektual Viktorina Platformasi

ZakoWhat — O'zbekiston intellektual viktorina madaniyatiga (Zakovat, Svoya Igra, Erudit) moslashtirilgan real vaqtli, jamoaviy va yakkalik (Solo Attempt) viktorina platformasi.

---

## 1. Texnologik Stek va Arxitektura

- **Backend**: Python 3.12 / 3.14, FastAPI, SQLAlchemy 2.0+, Alembic, Pydantic v2.
- **Database**: PostgreSQL (Neon Serverless) `DATABASE_URL` orqali.
- **Testing**: Pytest, HTTPX (izolatsiyalangan in-memory SQLite sinov bazasi).
- **Frontend**: Zamonaviy, toza SPA (Vanilla JS + Tailwind CSS), mobil va desktopga to'liq moslashgan.
- **Question Bank**: Markaziy mustaqil Savollar Banki (`Question` & `AcceptedAnswer`).
- **Placement**: Ko'p-ko'pga (M:N) munosabatli `RoundQuestion` assotsiatsiyasi orqali savollarni paketlarga joylashtirish.
- **Immutability**: E'lon qilingan versiyalarni muzlatilgan `published_manifest` (JSONB) orqali o'zgarmas saqlash.

---

## 2. Ma'lumotlar Modeli (Question Bank + RoundQuestion)

```
Quiz (Paket konteyneri)
  └── QuizVersion (game_mode, status, published_manifest)
        └── Round (sequence, round_type, config)
              └── RoundQuestion (sequence, points_override, config_override)
                    └── Question (Universal savollar banki)
                          └── AcceptedAnswer (To'g'ri javob variantlari)

SoloAttempt (O'yin sessiyasi)
  └── AnswerRecord (Yuborilgan javoblar auditi, round_question_id, wager)
```

### Muhim Tamoyillar:
1. **Mustaqil Savollar Banki (`Question`)**: Savollar biror konga yoki raundga qat'iy bog'lanmagan (`round_id = NULL`). Bitta savol bir nechta konga va bir nechta raundga takrorlanmasdan qo'shilishi mumkin.
2. **`RoundQuestion` Joylashuvi**: Savolning raunddagi tartibi (`sequence`), ball o'zgaruvchisi (`points_override`) va raund sozlamalari saqlanadi. `UNIQUE(round_id, sequence)` va `UNIQUE(round_id, question_id)` qoidalari qo'llaniladi.
3. **Versiya O'zgarmasligi (`published_manifest`)**: Viktorina e'lon qilinganda (`publish`), uning barcha savollari, javoblari va qoidalari yaxlit JSONB ko'rinishida muzlatiladi. Savollar bankidagi keyingi tahrirlar e'lon qilingan o'yinlarga ta'sir qilmaydi.

---

## 3. O'yin Rejimlari va Mexanikalari

Platforma quyidagi rasmiy o'yin rejimlarini qo'llab-quvvatlaydi:

### A. Zamonaviy Ko'p Raundli Viktorina (`modern_multiround`)
1. **Gulmisiz, rayhonmisiz? (`gulmisiz` / `mcq`)**:
   - 4 ta variantli (A, B, C, D) test savollari.
   - O'yin paytida to'g'ri javob sir tutiladi. Server avtoritativ tekshiradi.
2. **Zanjir (`zanjir`)**:
   - Harflar zanjiri qoidasi: javob avvalgi savol asosiy javobining oxirgi harfi bilan boshlanishi shart.
   - O'yinchiga avtomatik harf ko'rsatmasi berilmaydi.
3. **Mantiqqasqon (`mantiqasqon`)**:
   - Yashirin mantiqiy bog'liqlik (`hidden_rule`).
   - Raund yakunlangandan so'ng ochiladi (`/reveal`).
4. **Aldama meni (`true_false` / `aldama_meni`)**:
   - Rost yoki Yolg'on ("Ha" / "Yo'q") tugmalari.
   - Ko'p tilli sinonimlarni tushunadi (`rost`/`yolg'on`, `ha`/`yo'q`, `true`/`false`).
5. **Rasmiyatchilik (`rasmiyatchilik`)**:
   - Rasm/media bilan beriladigan savollar. Erkin matnli javob.
6. **Vabank (`vabank`)**:
   - Oddiy javob: to'g'ri `+1`, noto'g'ri `-1`.
   - Vabank (tavakkal): to'g'ri `+2`, noto'g'ri `-2`.
   - Bo'sh qoldirish (pass): `0` ball (jarimasiz).

### B. Klassik Zakovat (`classic_zakovat`)
- 24 ta savol, 2 ta tur (12 + 12 savol).
- 1-tur yakunida oraliq hisobot va ochilish (`/reveal`).
- 2-tur yakunida umumiy 24 savollik hisobot.

### C. Svoяk (`svoyak`)
- Mavzular bo'yicha tabaqalangan ballar (10, 20, 30, 40, 50).
- To'g'ri javob `+ball`, xato javob avtomatik ravishda `-ball`, bo'sh javob `0`.

---

## 4. Lokal Ishga Tushirish

### Muhitni faollashtirish va bog'liqliklar:
```powershell
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Muhit o'zgaruvchisi (`.env`):
```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
```

### Serverni ishga tushirish:
```powershell
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```
Brauzerda: `http://127.0.0.1:8000` ochiladi.

---

## 5. Testlarni Ishga Tushirish

Sinovlar in-memory SQLite bazasida 100% xavfsiz va tezkor bajariladi:

```powershell
.\venv\Scripts\pytest.exe -v
```

Natija: **49 ta test muvaffaqiyatli o'tadi**.

---

## 6. Savollar Bankiga Import Pipeline

Import to'g'ridan-to'g'ri umumiy Savollar Bankiga (`questions` va `accepted_answers`) yo'naltiriladi. Hech qanday soxta konga yoki bufer raundga ehtiyoj yo'q:

```powershell
# Faqat tekshirish
.\venv\Scripts\python.exe importer.py --validate-only canonical_1.json

# Xavfsiz test (Rollback)
.\venv\Scripts\python.exe importer.py --dry-run canonical_1.json --limit 50

# Jonli import
.\venv\Scripts\python.exe importer.py canonical_1.json --limit 50
```

---

## 7. API Kontrakt

| Usul | Yo'nalish | Tavsif |
| :--- | :--- | :--- |
| `GET` | `/health` | Tizim va ma'lumotlar bazasi holati |
| `GET` | `/api/quizzes` | E'lon qilingan paketlar ro'yxati va ularning `game_mode`i |
| `GET` | `/api/quizzes/{id}` | Viktorina tafsilotlari (javoblarsiz xavfsiz) |
| `POST`| `/api/quizzes/{id}/publish/{v}` | Versiyani e'lon qilish va `published_manifest`ni muzlatish |
| `POST`| `/api/play/start/{quiz_id}` | Yangi o'yin sessiyasini boshlash |
| `GET` | `/api/play/{session_token}` | Joriy holat va savol (MCQ variantlari bilan) |
| `POST`| `/api/play/{session_token}/answer` | Javob yuborish (Vabank va MCQ qo'llab-quvvatlanadi) |
| `GET` | `/api/play/{session_token}/reveal` | Raund yakunida to'g'ri javoblarni ochish |
| `POST`| `/api/play/{session_token}/continue` | Keyingi raundga o'tish |
| `GET` | `/api/play/{session_token}/results` | Yakuniy natijalar va turlar hisoboti |
