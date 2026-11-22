# ZAKOWHAT — IMPLEMENTATION, DECISION & RELEASE-READINESS REGISTER

**Platform:** ZakoWhat — Milliy Intellektual Viktorina Platformasi

**Repository:** `LstAkva/PROJECT-Z`

**Reconciled:** 27 October 2026

**Document status:** Decision-ready; current runtime verification is blocked in this workspace.
**Purpose:** Preserve the product decisions and hardening history that implementation agents need, while distinguishing them from current, independently verified facts and unverified operational claims.

---

## 1. How to use this register

This file replaces a point-in-time hardening report with a maintained decision register. It is not evidence that production is deployed, migrated, or healthy.

| Label | Meaning |
| --- | --- |
| **Current static evidence** | Confirmed by inspecting the present repository and working tree on 27 October 2026. It has not necessarily been executed in this reconciliation. |
| **Historical evidence** | A result recorded in the former 17 September 2026 report. It remains useful history, not a current assertion. |
| **Unverified** | No current evidence was available. Do not infer the state. |
| **Owner decision lock** | A product constraint expressed in the current `tests/test_owner_decision_lock.py` working-tree test. Preserve it unless the product owner explicitly changes it. |
| **Proposal** | Future work only. It does not authorize implementation, content publication, database changes, or deployment. |

### Scope of this reconciliation

Only this document was edited. No application code, quiz content, configuration, migration, database, deployment, release, or external service was changed.

---

## 2. Executive decision summary

ZakoWhat’s public product focus is the homepage, the Arena catalogue, published quiz packs, **Classic Zakovat**, and final results/review. The public Arena is deliberately limited to Classic Zakovat rather than exposing every technically retained game mode.

The most important preserved constraints are:

1. Classic Zakovat is a two-tour, 24-question experience: **2 × 12**.
2. Timed play uses a deterministic **90–180 second** per-question range; no-timer play returns a zero per-question limit.
3. Answer matching remains conservative and deterministic; it is not semantic or AI-based grading.
4. Anonymous visitors receive exactly one free play that is resistant to cookie clearing and spoofing; authenticated players are not blocked by that anonymous quota.
5. Published gameplay must use an immutable manifest. Editing the question bank after publication must not alter published questions, answers, explanations, rules, or scoring.
6. Zanjir rules are server-enforced without player-facing answer or letter hints before a legitimate reveal.
7. Production operations remain opt-in and guarded. No evidence in this workspace authorizes a migration, import, seeding run, or publication.

The former report presented several historical checks as if they described the present state. This register corrects that problem and makes the remaining work testable.

---

## 3. Evidence reviewed

### 3.1 Existing document and repository context

| Evidence | What it supports | Limits |
| --- | --- | --- |
| This file’s previous 17 September 2026 version | Historical hardening results, content-ingestion statistics, and earlier recommendations. | Its test count, database counts, smoke test, and deployment state are not current. |
| `main.py` | Routes for the homepage (`/`), Arena/play surfaces (`/arena`, `/quiz/{quiz_id}`, `/play/{quiz_id}`), staging routes, and owner/builder/bank views. | Route existence is not a live smoke test. |
| `templates/homepage_grand_sanctum.html`, `templates/index.html`, `static/css/zakowhat-design-system.css` | Current UI implementation and public Arena copy/filter evidence. | No browser or visual regression run was performed in this reconciliation. |
| `api/play.py`, `api/quizzes.py`, `api/drafts.py`, `services/gameplay.py` | Current gameplay, publication, answer-matching, and mode-handling implementation evidence. | Static inspection cannot establish live behavior. |
| `tests/` and `test_logic.py` | The repository currently contains 21 test files and 235 test functions, including regression and owner-decision locks. | The suite could not be executed here; see §8. |
| `data/packs/pack_02_canonical.json` and `tests/test_owner_decision_lock.py` | Pack 02 canonical-content and accepted-answer constraints. | The lock test is currently uncommitted, so its intent is clear but its CI/commit status is unverified. |
| `database.py`, `.env.example`, `scripts/verify_production_preflight_readonly.py`, `scripts/run_production_migration_secure.py`, `scripts/seed_and_publish_pack02_prod.py` | Environment/database safeguards and the existence of guarded operational scripts. | Script existence is not proof of production readiness or execution. |

### 3.2 Independent audit status

No standalone Claude audit or other independent audit artifact was found in the workspace. Therefore no audit finding has been adopted as fact. When such an audit is supplied, review it against the decision locks in §5 and add only findings that can be tied to current repository evidence.

---

## 4. Current product boundary

### 4.1 Public product — current intended scope

| Area | Current static evidence | Product requirement |
| --- | --- | --- |
| Homepage | `main.py` renders `templates/homepage_grand_sanctum.html` at `/`. | Explain the product and lead players to the Arena without inventing additional game modes or lore. |
| Arena catalogue | `templates/index.html` exposes only `all` and `classic_zakovat` filters. | Show published, player-safe pack metadata; draft/staging packs must stay out of public discovery. |
| Classic Zakovat play | `services/gameplay.py` declares `SUPPORTED_GAME_MODES = ["classic_zakovat"]`; publication and staging tests cover 2 × 12 behaviour. | Deliver the two-tour, 24-question flow with an intermediate first-tour break, final reveal/results, and review. |
| Results and review | `api/play.py` contains results/review handling; staging gameplay tests cover the full loop. | Show outcome only after the required gameplay state; do not expose answers early. |
| Published packs | `api/quizzes.py`/`api/play.py` use published versions and manifests. | Published material is immutable from the player’s perspective. |

### 4.2 Technical capability is not public scope

`api/drafts.py`, legacy tests, and the README still reference `modern_multiround` and `svoyak`. Their presence is **not** approval to expose them in the Arena. The current owner lock and public-mode declaration restrict the player-facing scope to `classic_zakovat`; `FUTURE_MINI_GAMES = ["wheel"]` is an extension placeholder, not an implemented feature.

**Owner decision required before any scope expansion:** whether to revive a broader public mode catalogue, and if so which mode, rules, content standard, Arena language, migration needs, and release milestone apply. Do not infer that decision from old code paths.

### 4.3 Visual identity — approved direction

The player-facing UI must use a black-and-white foundation, burgundy for meaningful interaction/state accents, and gold sparingly for brand details. It must avoid generic dashboard patterns, excessive decorative treatments, nested cards, gratuitous gradients, and exaggerated luxury-editorial styling.

This is a design constraint, not permission to redesign the app. Any UI change must preserve legibility, responsive gameplay, the single clear answer action, and the existing product hierarchy.

---

## 5. Preserved decision locks and implementation requirements

### 5.1 Gameplay, timing, and results

| Decision | Requirement | Why it matters | Objective acceptance criteria |
| --- | --- | --- | --- |
| Classic structure | A public Classic Zakovat pack has exactly two `zakovat_classic` rounds of 12 questions each. | The format is the core public experience and must not drift through authoring changes. | Publication rejects one 24-question round, three rounds, or any round count other than 12; a valid 2 × 12 pack publishes. |
| First-tour transition | After question 12, play enters an intermediate break; the player must explicitly continue to tour 2. | Preserves the intended tournament rhythm and prevents accidental answers during the break. | Submissions during the break are rejected; continue opens question 13; results/review show both tours. |
| Timer policy | With timer: base 90 seconds plus `round(word_count / 2.5)`, constrained to 90–180 seconds. Without timer: `0`. | Reading time is accounted for while preventing unbounded per-question timers. | Empty text is 90; 25 words is 100; 200 words is 170; 225+ words is 180; disabled timer is 0. |
| Session expiry | More than 300 seconds of inactivity expires a non-completed attempt and returns HTTP 410 for state, answer, continue, reveal, and results access. | Prevents stale/resumable sessions from creating ambiguous results. | An attempt with `last_active_at` older than 300 seconds is marked `expired` and every listed endpoint returns 410. |
| Results/reveal | Answers and any hidden rules are revealed only in the permitted post-round/final state. | A quiz loses integrity if answers leak into discovery or active play. | Discovery, start, current-state, and answer responses exclude primary/accepted answers and hidden rules; reveal/review returns them only when allowed. |

### 5.2 Answer evaluation and Zanjir

| Decision | Requirement | Objective acceptance criteria |
| --- | --- | --- |
| Normalization | Use the existing Uzbek Latin normalization and accepted-answer list. | Apostrophe variants, casing, surrounding punctuation, and whitespace behave consistently; the question’s accepted answers remain authoritative. |
| Conservative typo tolerance | A one-edit Damerau–Levenshtein tolerance applies only under the current deterministic guardrails: matching word count, same initial character per changed word, long enough target, exactly one edit, and exact matching for short roots. | A valid long-word single typo can pass; short answers, changed initials, different word counts, semantic substitutions, or multiple edits fail. No semantic/LLM matching is introduced. |
| Zanjir | The server checks the submitted/matched answer against the previous primary answer using Uzbek-Latin boundary handling. The player receives no automatic starting-letter, previous-answer, expected-letter, or hint payload before reveal. | A valid standard, modified-vowel, and digraph chain passes; an invalid link scores incorrect; active-play payloads contain none of `zanjir_start_letter`, `previous_answer`, `expected_letter`, or a hint field. |

**Confirmed maintenance discrepancy:** `services/gameplay.py` still defines `get_zanjir_hint`, although `api/play.py` does not import/call it and response regression tests assert no hint payload. The former report’s claim that the helper was “completely removed” is therefore inaccurate. Treat this as an internal cleanup item, not evidence that player-facing hints currently leak. Remove the unused helper only after the test environment is restored and the anti-leak tests pass.

### 5.3 Content immutability and Pack 02

| Decision | Requirement | Objective acceptance criteria |
| --- | --- | --- |
| Published manifest | Published versions must resolve gameplay from `published_manifest`; a published version without a valid manifest must fail rather than fall back to mutable database rows. | A post-publication edit/delete of a source `Question`, answer, or placement cannot change a new player’s published experience. |
| Publication validity | Incomplete drafts may save but must not publish; repeat publication must preserve the original manifest and return a conflict rather than create a new version silently. | Prevents partial packs and accidental content mutation. |
| Pack 02 canonical source | `data/packs/pack_02_canonical.json` remains 24 questions. Q22 accepts only **“Uchib tushmoq”** and **“Uchib tushdi”**; it must not accept **“Yuragi uchmoq.”** Spoken preambles in Q09, Q10, Q13, Q15, Q17, and Q23 are retained. | Protects reviewed editorial content and its intended phrasing. |

Do not edit published questions, accepted answers, Pack 02 canonical content, round order, or scoring as a side effect of UI or code work. Editorial change requires a deliberate owner-approved content/version workflow.

### 5.4 Identity, access, and preview isolation

| Decision | Requirement | Objective acceptance criteria |
| --- | --- | --- |
| Anonymous play | One free anonymous attempt is bound to the server’s anonymous identity and server-side IP hash; clearing/spoofing a cookie or expiring an attempt does not reset it. Authenticated play is not blocked by this anonymous quota. | First anonymous play succeeds; subsequent attempts from the same tracked visitor/IP are blocked with 403 even after cookie changes; an authenticated player can start. |
| Owner/admin access | Owner routes fail closed when owner email configuration is absent. Staging play requires an authorized authenticated editor/admin. | Unauthenticated or unconfigured access is denied; public users cannot access staging or consume anonymous quota through preview. |
| Staging isolation | Staged draft packs are excluded from public Arena discovery, detail, play, and leaderboards. Preview attempts carry staging/preview state and do not become public results. | Public endpoints return no staged content; authorized staging gameplay works without exposing answers before reveal. |

---

## 6. Production and release safeguards

### 6.1 Verified safeguards in source

- `database.py` requires explicit `ENVIRONMENT` and `DB_TARGET`, rejects missing/invalid values, and rejects development/production target mismatches.
- `api/auth.py` requires an explicit `SECRET_KEY` in production and uses secure cookies in production.
- `scripts/verify_production_preflight_readonly.py` is explicitly non-mutating and checks production targeting before inspection.
- `scripts/run_production_migration_secure.py` separates `--dry-run` from `--execute` and contains development-host guards.
- `scripts/seed_and_publish_pack02_prod.py` exists as a guarded Pack 02 seeding/publishing path.

### 6.2 Operational state — explicitly unverified

The current production database target, deployed revision, migration level, health, published-pack inventory, credentials, backup/restore readiness, and actual execution of any operational script are **unknown** from this workspace review. The former report’s Neon schema counts and `SELECT 1` result are retained as September history only.

### 6.3 Required approval and execution sequence

No implementation agent may run any production-changing command from this document. A future approved release must proceed in this order:

1. Product owner names the target environment and approves the exact release/migration/content scope.
2. An authorized operator performs the read-only preflight against the intended target and records the sanitized result.
3. The repaired local test suite and required deployment checks pass against the release revision.
4. The operator performs the explicitly approved migration or seed/publish action, never both by assumption.
5. The operator verifies public discovery, a safe play-through, reveal/results, and the absence of draft/staging exposure.
6. The owner accepts the release outcome and records the deployed revision and target in a dated release record.

---

## 7. Confirmed problems, not assumptions

| Priority | Problem and evidence | Impact / risk | Recommended change | Ownership / dependency |
| --- | --- | --- | --- | --- |
| P0 | This workspace cannot execute tests: no system Python is available and `venv\\Scripts\\pytest.exe` references a missing `venv\\Scripts\\python.exe`. | Current runtime claims cannot be revalidated; a release gate is missing. | Restore or recreate the approved local Python environment, install the locked/project dependencies, then run the full suite before trusting current behaviour. | Safe engineering work; no product-rule change. Do not alter production configuration. |
| P1 | The old report says all 37 tests passed; the current tree statically contains 235 test functions. README still says 49 tests and presents broader modes as current product modes. | Future agents could use stale test counts and expand public scope accidentally. | Keep this register as the decision reference; separately reconcile README and any user-facing documentation after tests run. | Documentation change is independent; public-scope change requires owner approval. |
| P1 | `get_zanjir_hint` remains defined in `services/gameplay.py`, contradicting the old report’s “removed” statement. | Dormant code may be reused and reintroduce a prohibited hint path. | After test recovery, remove it or explicitly justify/cover its use; retain anti-leak tests. | Safe technical cleanup, dependent on passing tests. |
| P1 | No `.github/workflows` directory was found. | There is no repository-local evidence of CI enforcing publication, anti-leak, or regression checks. | Propose CI only after the test environment is reproducible; gate the full suite and targeted content/anti-leak tests. | Requires repository/CI authorization; does not require gameplay policy changes. |
| P2 | The current working tree contains uncommitted source, style, and test changes, including the owner-decision lock. | The documented decisions may not be protected in the branch that is ultimately released. | Review and commit changes through the normal workflow after tests pass; do not discard user work. | Requires code-review/release ownership. |
| P2 | There is no standalone independent audit artifact to reconcile. | Potential external findings remain unknown. | Add a dated audit-integration section only when the actual audit is supplied. | Needs audit artifact; no code work. |

---

## 8. Verification plan and release gate

### 8.1 Reconciliation performed on 27 October 2026

- Read the entire previous implementation report and inspected the current repository structure, route templates, core API/service files, operational scripts, tests, current git state, and decision-lock test.
- Counted 21 test files and 235 test functions by static search.
- Confirmed no standalone audit artifact and no repository-local CI workflow directory.
- Attempted the current virtual-environment test entry point. It did not start because the virtual environment references a missing Python executable; therefore **no test pass/fail result is claimed for this reconciliation**.

### 8.2 Mandatory verification before a release or completion claim

| Verification | Evidence to record | Pass condition |
| --- | --- | --- |
| Environment restoration | Python version, dependency install method, and sanitized command output. | Test runner starts from the intended isolated environment. |
| Full regression suite | Exact command, revision, test count, duration, pass/fail result. | Entire suite passes with no skipped security/content tests unless explicitly approved. |
| Decision-lock coverage | `tests/test_owner_decision_lock.py` plus relevant gameplay, publishing, security, staging, and production-parity tests. | Timer, answer, Pack 02, session, anonymous quota, mode filter, and visual-copy locks pass. |
| Player-path smoke | Home → Arena → published Classic pack → 24-question flow → break → reveal → results/review. | No early answer/hidden-rule leak; correct tour transition and results totals. |
| Access/isolation smoke | Anonymous quota, authenticated access, owner/admin denial, staged-pack isolation. | Public users cannot enumerate/play drafts; authorized editor preview does not create public leaderboard state. |
| Visual review | Desktop and mobile captures of home, Arena, question, results/review. | Black/white foundation; burgundy is the primary interaction accent; gold stays limited to brand detail; no nested-card/gradient/dashboard drift; readable controls and content. |
| Production preflight, if approved | Sanitized read-only preflight record with target/revision/approver. | Target guards pass and no mutation occurs during preflight. |

---

## 9. Prioritized roadmap

| Priority | Outcome | Scope and rationale | Dependencies | Owner approval |
| --- | --- | --- | --- | --- |
| P0 — release gate | Reproducible verification | Repair the local test environment and record a full-suite result. This is prerequisite evidence, not a feature. | Approved Python/dependency setup; preserve the user’s working tree. | No product decision; release still requires owner approval. |
| P1 — control integrity | Align active documentation and dormant anti-leak code | Reconcile README with public Classic-only scope and current test facts; remove or justify the unused Zanjir hint helper after regression tests pass. | P0 test recovery. | No gameplay-rule change. |
| P1 — safe automation | CI proposal | Add a reviewed workflow to execute the suite and key content/anti-leak checks on changes. | Reproducible P0 environment and repository integration access. | Required for external CI configuration. |
| P2 — editorial workflow review | Decide whether existing owner/bank/builder surfaces satisfy review needs | Assess the actual content review path before proposing a new moderation UI. The old “build a moderation UI” recommendation is too vague and may duplicate existing surfaces. | Owner/editor workflow evidence; P0 verification. | Required before changing editorial process. |
| P2 — release evidence | Create a per-release record | For an approved release, capture preflight, migration/seed authorization, revision, smoke results, and acceptance. | P0/P1 completion and operator access. | Required for every production-changing step. |
| P3 — future concepts | Wheel or multiplayer/tournament concepts | Keep `wheel` as a future placeholder. Multiplayer/WebSocket tournaments are not part of the current public core and must not be implemented from this document. | Product brief, rules, moderation, scalability, privacy, and release plan. | Explicit owner approval required. |

---

## 10. Explicit non-goals and prohibited changes

- Do not make `modern_multiround`, `svoyak`, blitz, wheel, multiplayer, WebSockets, or tournament rooms public without a new owner-approved product scope.
- Do not change Classic Zakovat’s 2 × 12 structure, timer formula/range, session expiry, answer-matching policy, anonymous quota, publication semantics, or Pack 02 canonical questions/answers without explicit owner approval and updated decision locks.
- Do not expose answers, accepted-answer lists, Zanjir chain clues, `hidden_rule`, or draft/staging packs during public discovery or active play.
- Do not deploy, migrate, seed, import, publish, push, merge, or modify production configuration from this document.
- Do not treat historical counts, old local smoke tests, or the presence of scripts as proof of current production state.
- Do not replace the product’s restrained black/white/burgundy visual language with generic dashboard cards, decorative gradients, or a new luxury-editorial concept.

---

## 11. Historical evidence retained from the former report

The following is retained for project history but is **not** a current claim:

### 11.1 17 September 2026 hardening snapshot

- The former report recorded **37 passing automated tests** in an isolated in-memory SQLite configuration, taking 2.85 seconds.
- It recorded a quality-gate evaluation of `canonical_1.json`: 4,318 records total; 3,356 ready to import (77.72%); 790 needing review (18.30%); and 172 rejected (3.98%). Recorded rejection/review categories are preserved in the former report history: answer length, Cyrillic question/answer text, answer contamination, terminal punctuation, options/brackets, and potential answer leakage.
- It recorded read-only development/Neon inspection at that time: tables for quizzes, versions, rounds, questions, accepted answers, attempts, records, and Alembic; counts of 2 quizzes, 2 versions, 5 rounds, and 69 questions.
- It recorded a local `/health` and gameplay smoke test, including an earlier published demo quiz and no observed Zanjir payload hint.
- It explicitly said mass ingestion, production Alembic execution, and live Telegram integration had not been performed.

### 11.2 What was consolidated or retired

- The long, verbatim 37-test transcript was replaced with its historical result plus a current verification plan. It duplicated information while obscuring that the suite has since grown.
- The old “next steps” were reclassified: editorial moderation is now an evidence-gathering decision; batch ingestion/migrations are operator-approved production actions; multiplayer is a future non-goal; CI is a specific proposal dependent on a reproducible test environment.
- The inaccurate absolute statement that all Zanjir hint code was removed was replaced with the narrower, evidence-based player-payload requirement and the remaining helper discrepancy.

---

## 12. Owner decisions and evidence still needed

1. **Public mode boundary:** Confirm that the public RC remains Classic Zakovat only, despite technically retained legacy/authoring paths. A decision to expose another mode must define its complete rule and content contract.
2. **Production release authorization:** Name the target environment, pack/version, operator, approved operation (preflight, migration, seed, or publish), and rollback/acceptance authority. Current production state is unknown.
3. **Editorial workflow:** Decide whether the existing owner/bank/builder/staging flow is adequate before commissioning additional moderation UI.
4. **Test-environment owner:** Identify the supported Python/dependency path that should make the suite reproducible in this workspace/CI.
5. **Independent audit:** Supply the audit artifact, if one exists, so findings can be verified and incorporated without overriding established product decisions.

---

## 13. Change record for this document

**27 October 2026 — document reconciliation**

- Reframed the existing hardening report as a current decision and release-readiness register.
- Preserved historical hardening/quality-gate evidence while removing its false implication of current verification.
- Added explicit product decision locks, testable acceptance criteria, dependency-aware roadmap, release gates, non-goals, and owner decisions.
- Recorded current contradictions and missing evidence, including the blocked test runner, stale README scope/test claims, dormant Zanjir hint helper, uncommitted working-tree decisions, missing CI evidence, and absent independent audit.
- Made no application or production-system change.
