# Gameplay and frontend guide

A working guide to the player-facing frontend: how a game runs, where the code lives, how the visual
system ("The Round") is applied, and how to test changes safely. It complements `README.md` (stack, data
model, import pipeline) and `DESIGN.md` (design principles); it does not repeat them.

No credentials, connection strings or personal data belong in this file.

## 1. Where things live

| Area | File |
|---|---|
| Arena, Pack Details, Game Settings, gameplay, results, review, leaderboard, profile | `templates/index.html` (markup + one inline controller script) |
| Homepage | `templates/homepage_grand_sanctum.html` |
| Daily Question | `templates/daily.html` |
| Shared header / footer / sign-in modal / brand mark | `templates/components/` |
| Design tokens and every component style | `static/css/zakowhat-design-system.css` |
| Custom dialogs (`zwAlert`, `zwConfirm`, `zwPrompt`) | `static/js/zakowhat-dialog.js` |
| Theme toggle, navigation, password toggle | `static/js/zakowhat-navbar.js` |
| Gameplay API | `api/play.py` |
| Pack list / details / leaderboard API | `api/quizzes.py` |

`/arena`, `/quiz/{id}`, `/play/{id}`, `/profile` and `/staging` all serve `index.html`; the controller
shows the right screen from the URL.

## 2. Gameplay lifecycle

Screens are `<section>` elements switched by `showScreen(id)`:

`screen-selection` (Arena) → `screen-gameplay` → `screen-intermediate` (break, Classic only) →
`screen-gameplay` → `screen-results`. `screen-reveal` is the round review used by older round types.

Sheets (overlays) share one shell, `.pack-detail`: Pack Details (`modal-quiz-detail`), Game Settings
(`modal-zakovat-settings`), Leaderboard (`modal-leaderboard`), Answer Review (`modal-review`) and the
restart confirmation (`modal-gameplay-restart`). Escape closes the topmost one; focus moves into a sheet
when it opens and returns to the control that opened it.

| Step | Controller function | Endpoint |
|---|---|---|
| Open details | `openQuizDetail` | `GET /api/quizzes/{id}` |
| Choose mode | `openZakovatSettingsModal` → `confirmStartGameWithSettings` | — |
| Start | `executeStartGame` | `POST /api/play/start/{id}` |
| Show a question | `renderQuestionState` → `startTimer` | (payload from start / answer / continue) |
| Submit / time out / pass | `submitAnswer` (`handleTimeOut`, `handleSvoyakPass`) | `POST /api/play/{token}/answer` |
| Reveal and continue | `showAnswerReveal` → `handleRevealContinue` → `continueToNextQuestion` | `POST /api/play/{token}/continue` |
| Break | `showIntermediateBreak` → `handleIntermediateContinue` | `POST /api/play/{token}/continue` |
| Svoya Igra board | `renderSvoyakBoard` → `selectSvoyakQuestion` | `POST /api/play/{token}/select` |
| Results | `handleFinalizeAndShowResults` → `loadFinalResults` | `GET /api/play/{token}/results` |
| Review | `openReviewModal` | `GET /api/play/{token}/review` |
| Restart | `confirmRestartGameplay` → `executeRestartGameplay` | `POST /api/play/{token}/restart` |
| Resume after reload | `restoreActiveSession` | `GET /api/play/{token}` |

Rules of thumb:

- **The server is authoritative.** Timer mode, remaining time, ranked/practice status and scores always
  come from the API response; the client only displays them. Never compute a score or a verdict client-side.
- **One submission at a time.** `state.isSubmitting` guards `submitAnswer` and `selectSvoyakQuestion`;
  buttons are disabled while a request is pending. Keep that guard when adding actions.
- **Timers.** `state.timerInterval` (question) and `state.breakInterval` (break) must be cleared whenever
  the player leaves the screen (`goToQuizSelection`, `popstate`, restart).
- **Errors.** Show them with `zwAlert('Xatolik: ' + describeError(err))`. Read API error bodies with
  `apiDetail(body)` (handles both string and validation-list `detail`), never `err.detail` directly.
- **Element ids are a contract.** The controller finds elements by id and several tests pin them. Ids must
  be unique in the page; `tests/test_gameplay_redesign.py` fails on a missing or renamed target.

## 3. Classic Zakovat versus Svoya Igra

| | Classic Zakovat | Svoya Igra |
|---|---|---|
| Structure | 2 rounds × 12 questions | 5 themes × 5 questions (10–50 points) |
| Entry | first question | board (`free_choice`) or fixed order (`classic`) |
| Scoring | count of correct answers | points: +value correct, −value wrong, 0 for pass or timeout |
| Empty submit | allowed (counts as unanswered) | refused; passing is an explicit button |
| Break | after round 1 (countdown, auto-continue) | none |
| Leaderboard metric | correct answers, then active time | score, then active time |
| Accent | burgundy rule and progress | brass rule, brass point values |

The play surface is `#game-table`. The controller sets `data-mode` (`classic_zakovat` / `svoyak`) and
`data-view` (`question` / `board`) on it; the stylesheet does the rest. Do not branch on format in CSS
any other way.

## 3a. Daily Question

- One question per Asia/Tashkent date (`daily_questions`), scheduled lazily on the first visit of the day from
  `data/daily/daily_questions_bank.json`, never repeated (key: `message_id`). Selection lives in
  `services/daily_question.py` and must not be changed casually.
- One submission row per player per day (`daily_submissions`), created only by `/api/daily/submit` after a
  non-empty, evaluated attempt. Three attempts; a repeated answer costs nothing.
- **Streaks** (`services/daily_engagement.py`) are derived from those rows — no extra columns. A participation
  day is the `schedule_date` of a question the player has a submission for (participation, not correctness).
  Calendar days with no scheduled question are neutral. The current streak stays alive until a scheduled day
  passes unanswered. Exposed by `/api/daily/stats` and in `/api/daily/today` (`streak`).
- **Sharing**: `/api/daily/today` returns `share_text` once the player has finished. It is built server-side
  from the date, outcome, attempt count and streak only — never the question, answer, name or ids.
  `PUBLIC_SITE_URL` (optional env var, default `https://zakowhat.uz`) sets the link.
- **Bank monitoring**: `/api/owner/daily-bank` (owner only, read-only) counts what the scheduler can still
  serve with its own pool, eligibility rule and uniqueness key. Levels: normal > 30, warning ≤ 30,
  critical ≤ 10, empty 0. Shown on the owner dashboard.
- Answers are revealed only after the player finishes (or, in `/api/daily/history`, once the day is over).

## 3b. Player progress and resuming games

`services/player_progress.py` (read-only) derives everything from `solo_attempts` and `answer_records`:

- **Profile statistics** (`stats.by_format` in `/api/auth/profile`): Classic = games, correct, answered,
  accuracy; Svoya Igra = games, total points, best points. Competitive games only (practice counted apart,
  previews excluded). The two scoring systems are never added together.
- **Arena progress** (`/api/me/pack-progress`, the caller's own packs): `in_progress` = an active attempt
  within the 30-minute inactivity limit, with distinct questions answered in *that* attempt / questions in its
  version; `completed` = best result in that pack's own scoring; `unfinished` = started, no longer resumable.
  Never-played packs carry no label. Restarts are separate attempts and never add up.
- **Resuming** (`/api/me/active-game`): returns the caller's latest resumable attempt (anonymous visitors get
  `{"game": null}`). The page then restores it through the existing `GET /api/play/{token}`; it never starts a
  game. Timers are server-side (`question_opened_at`): a reload or another device never grants time, and a
  question whose time already ran out times out immediately. Ranked answers past the limit (+3 s grace) score 0.
  Sessions expire after 30 minutes without activity (`SESSION_INACTIVITY_LIMIT_SECONDS`).

## 3c. Indexing

- Indexed: `/`, `/arena`, `/daily`, `/privacy`, `/terms` — each has a canonical URL, Open Graph and
  Twitter tags and the share image `static/img/og-zakowhat.png` (1200×630, built from the official mark).
- `noindex`: every other path served by `index.html` (`/quiz/{id}`, `/play/{id}`, `/profile`, `/u/…`,
  `/staging`) and the owner tools. `robots.txt` also keeps crawlers out of `/api/`, owner tools, games and
  profiles — a crawling hint only; access control stays on the server.
- `/sitemap.xml` and all absolute URLs use `public_site_url()` (`PUBLIC_SITE_URL`, default `https://zakowhat.uz`).

## 4. The Round: tokens and components

- Brand tokens: `--ink`, `--charcoal`, `--burgundy`, `--brass`, `--ivory`. Semantic tokens built on them:
  `--bg-app`, `--bg-surface`, `--bg-surface-elevated`, `--text-main`, `--text-muted`, `--text-dim`,
  `--border-subtle`, `--border-medium`, `--link-color`, `--text-brass`, `--mode-rule-svoyak`,
  `--state-correct`, `--state-incorrect`.
- Dark values are the `:root` defaults; `[data-theme="light"]` overrides them. Light is the default theme.
- **New component CSS uses semantic tokens only** — no hex colours, no `rgba()`, no Tailwind colour
  utilities in gameplay markup or in HTML built by the controller. A test enforces this for section 16.
- Headings and question text: `var(--font-serif)` (Playfair Display). Controls, labels, metadata:
  `var(--font-sans)`.
- Visibility is toggled with the `hidden` class (`display: none !important` in the design system).
- The older "table" classes (`.table-outer-rim`, `.question-stage-card`, `.clock-display`, …) belong to the
  homepage demo only.

## 5. Testing

```bash
# targeted (fast)
venv/Scripts/python.exe -m pytest tests/test_gameplay_redesign.py tests/test_svoyak_gameplay.py -q

# full suite (about ten minutes) — run it on its own, with no browser checks or dev server load alongside
venv/Scripts/python.exe -m pytest -q
```

Tests run against an in-memory SQLite database (`tests/conftest.py` refuses anything else).

### Real playthroughs without touching real data

```bash
venv/Scripts/python.exe scripts/qa_server.py     # http://127.0.0.1:8011
```

This runs the unmodified app against a throwaway SQLite file in `scratch/` (git-ignored), seeded with one
Classic Zakovat pack (round 1 has a 20-second limit so timeouts can be observed) and one Svoya Igra pack.
Register a player through the normal sign-up form; nothing is bypassed. Signing up as
`qa-owner@zakowhat.test` gives owner access in this throwaway database only. Delete
`scratch/qa_playthrough.db` to reset. The server refuses to start on anything but SQLite.

Manual acceptance checklist: start → correct answer → wrong answer → let the timer run out → reload the
page mid-game → finish round 1 → break → finish → results → review → leaderboard → restart → exit; then
Svoya Igra: board → correct / wrong / pass → verify the score and the played cells → finish → results.

## 6. Working with the databases

- Local development uses the **development** database configured in `.env`; production is only reachable
  from the hosting environment. `database.py` validates `ENVIRONMENT` / `DB_TARGET` and refuses a
  mismatched pair.
- Never point a local process at production, and never paste a connection string into chat, an issue or a
  commit.
- Content moves from development to production only through `scripts/transfer_packs.py`: it is
  idempotent, dry-run by default, limited to named batches, and needs an explicit flag for the production
  branch. Take a Neon backup first. Development-only test packs are excluded by the script and its tests.
- Schema changes go through Alembic migrations; production migrations run in the pre-deploy step, not by hand.
- Deployments are manual and need the owner's explicit approval.
