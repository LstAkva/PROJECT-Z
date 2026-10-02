import pytest
from fastapi.testclient import TestClient
from main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_production_dependency_google_auth_requests_importable():
    """
    Regression test: Ensures requests package is installed and usable
    by google.auth.transport.requests for Google Sign-In.
    """
    from google.auth.transport import requests as google_requests
    req = google_requests.Request()
    assert req is not None


def test_production_dependency_psycopg_importable():
    """
    Regression test: Ensures psycopg v3 driver is installed and importable.
    """
    import psycopg
    assert psycopg.__version__ is not None


def test_homepage_renders_grand_sanctum_and_never_old_mvp(client):
    """
    Regression test: Verifies GET / serves Grand Sanctum, not the legacy MVP.
    """
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    
    # Must contain Grand Sanctum signatures
    assert "Daqiqa Ichra Haqiqat Jilvasi" in html
    assert "Mustaqil Intellektual Maydon" in html
    assert "ZakoWhat" in html
    assert "zakowhat-design-system.css" in html
    
    # Must NEVER contain the old MVP foundation text
    assert "Zakovat Platform MVP" not in html
    assert "Фундамент заложен" not in html

    # Must NOT render the temporarily removed "Girih-i Muammo: Mantiq Geometriyasi" section
    assert "Girih-i Muammo" not in html
    assert "Mantiq Geometriyasi" not in html
    assert 'id="lore"' not in html


def test_homepage_and_arena_have_no_cache_headers(client):
    """
    Regression test: Verifies that HTML pages send Cache-Control headers
    preventing edge proxies and browsers from serving stale cached templates.
    """
    res_home = client.get("/")
    assert "no-cache" in res_home.headers.get("Cache-Control", "")
    assert "no-store" in res_home.headers.get("Cache-Control", "")

    res_arena = client.get("/arena")
    assert "no-cache" in res_arena.headers.get("Cache-Control", "")
    assert "no-store" in res_arena.headers.get("Cache-Control", "")


def test_arena_route_renders_catalog_container(client):
    """
    Verifies GET /arena serves the Arena catalog SPA container.
    """
    response = client.get("/arena")
    assert response.status_code == 200
    html = response.text
    assert "ZAKOWHAT ARENA" in html
    assert "Rasmiy Viktorina To‘plamlari" in html
    assert "Zakovat Platform MVP" not in html


def test_static_assets_load_cleanly(client):
    """
    Verifies that all core static assets required by Grand Sanctum and Arena load with 200 OK.
    """
    css_res = client.get("/static/css/zakowhat-design-system.css")
    assert css_res.status_code == 200
    assert len(css_res.content) > 1000

    insignia_res = client.get("/static/svg/zakowhat-insignia.svg")
    assert insignia_res.status_code == 200
    assert len(insignia_res.content) > 100

    favicon_res = client.get("/static/svg/zakowhat-favicon.svg")
    assert favicon_res.status_code == 200
    assert len(favicon_res.content) > 50

    girih_res = client.get("/static/svg/zakowhat-girih-pattern.svg")
    assert girih_res.status_code == 200
    assert len(girih_res.content) > 500


def test_public_catalog_never_exposes_drafts(client):
    """
    Verifies that GET /api/quizzes strictly returns published quizzes only,
    and draft packs never leak into the public catalog.
    """
    response = client.get("/api/quizzes")
    assert response.status_code == 200
    quizzes = response.json()
    from database import SessionLocal
    from models import QuizVersion
    db = SessionLocal()
    try:
        for q in quizzes:
            v = db.query(QuizVersion).filter(QuizVersion.id == q["version_id"]).first()
            assert v is not None
            assert v.status == "published"
    finally:
        db.close()



def test_owner_panel_endpoints_inaccessible_to_unauthorized(client):
    """
    Verifies that administrative Owner endpoints reject anonymous access with HTTP 401.
    """
    endpoints = [
        "/api/owner/status",
        "/api/owner/quizzes",
        "/api/owner/questions",
    ]
    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 401


def test_question_bank_inaccessible_to_unauthorized(client):
    """
    Verifies that Question Bank explorer rejects anonymous access with HTTP 401.
    """
    res = client.get("/api/questions")
    assert res.status_code == 401


def test_staging_endpoints_inaccessible_to_unauthorized(client):
    """
    Verifies that Staging endpoints reject anonymous access with HTTP 401.
    """
    res = client.get("/api/play/staging/quizzes")
    assert res.status_code == 401

    res = client.get("/api/play/staging/quizzes/1")
    assert res.status_code == 401

    res = client.post("/api/play/staging/start/1")
    assert res.status_code == 401


def test_arena_renders_catalog_even_with_active_anonymous_attempt(client):
    """
    Regression test for Production UX Bug:
    When an anonymous player has an unfinished/active attempt and navigates to /arena
    (e.g., using browser Back or typing /arena), /arena must ALWAYS render the Arena
    catalog (screen-selection), and NEVER automatically hijack or replace the view with
    the active game. The attempt remains resumable via explicit action ("Davom etish").
    """
    # 1. Fetch available quizzes to get a playable published quiz
    res_quizzes = client.get("/api/quizzes")
    assert res_quizzes.status_code == 200
    quizzes = res_quizzes.json()
    assert len(quizzes) > 0, "At least one published quiz must exist"
    quiz_id = quizzes[0]["quiz_id"]

    # 2. Anonymous player starts the quiz
    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == 201
    start_data = start_res.json()
    session_token = start_data["session_token"]
    assert session_token is not None

    # Verify attempt is in_progress
    state_res = client.get(f"/api/play/{session_token}")
    assert state_res.status_code == 200
    assert state_res.json()["status"] == "in_progress"

    # 3. Anonymous player navigates to /arena (presenting their anonymous cookie)
    arena_res = client.get("/arena")
    assert arena_res.status_code == 200
    html = arena_res.text

    # Contract A: HTML must render the Arena Catalog SPA container (screen-selection)
    assert "ZAKOWHAT ARENA" in html
    assert "Rasmiy Viktorina To‘plamlari" in html
    assert 'id="screen-selection"' in html
    assert 'id="screen-gameplay"' in html

    # Contract B: DOM layout has screen-selection visible by default and screen-gameplay hidden
    assert '<section id="screen-selection" class="space-y-8 animate-fade-in">' in html
    assert '<section id="screen-gameplay" class="hidden animate-fade-in w-full">' in html

    # Contract C: Frontend JavaScript routing guarantees /arena never automatically invokes restoreActiveSession
    # restoreActiveSession on load must be guarded strictly by /play/{id} route
    assert "const playMatch = path.match(/^\\/play\\/(\\d+)$/);" in html
    assert "if (playMatch) {" in html

    # Contract D: Single-page navigation includes popstate listener to return to catalog on browser Back
    assert "window.addEventListener('popstate'" in html
    assert "showScreen('screen-selection')" in html

    # Contract E: Catalog supports explicit resume UX ("Davom etish")
    assert "hasActiveSessionForQuiz" in html
    assert "handleCardPlayAction" in html
    assert "Davom etish" in html

    # Contract F: The active attempt was NOT destroyed by navigating to /arena; it remains resumable
    resume_check = client.get(f"/api/play/{session_token}")
    assert resume_check.status_code == 200
    assert resume_check.json()["status"] == "in_progress"


def test_arena_page_javascript_has_no_syntax_errors(client):
    """
    Regression test: Validates that all inline <script> tags served by /arena
    have valid JavaScript syntax and zero unclosed braces, brackets, or function bodies.
    Validates syntax using both structural token analysis and a real ECMAScript V8 compiler.
    """
    import re
    res = client.get("/arena")
    assert res.status_code == 200
    html = res.text

    scripts = re.findall(r"<script(?:\s+[^>]*)?>(.*?)</script>", html, re.DOTALL)
    assert len(scripts) > 0, "No <script> tags found in /arena"

    # Identify main application script
    main_scripts = [s for s in scripts if "loadQuizzes" in s]
    assert len(main_scripts) == 1, "Expected exactly one main application script in /arena"
    main_js = main_scripts[0]

    # 1. Structural balance check: ensure every brace, paren, and bracket is matched
    brace_stack = []
    paren_stack = []
    bracket_stack = []

    for line_idx, line in enumerate(main_js.splitlines(), start=1):
        for col_idx, ch in enumerate(line, start=1):
            if ch == "{":
                brace_stack.append((line_idx, col_idx))
            elif ch == "}":
                assert len(brace_stack) > 0, f"Unmatched closing brace '}}' at line {line_idx}:{col_idx}"
                brace_stack.pop()
            elif ch == "(":
                paren_stack.append((line_idx, col_idx))
            elif ch == ")":
                assert len(paren_stack) > 0, f"Unmatched closing paren ')' at line {line_idx}:{col_idx}"
                paren_stack.pop()
            elif ch == "[":
                bracket_stack.append((line_idx, col_idx))
            elif ch == "]":
                assert len(bracket_stack) > 0, f"Unmatched closing bracket ']' at line {line_idx}:{col_idx}"
                bracket_stack.pop()

    assert len(brace_stack) == 0, f"Unclosed braces in /arena script: {brace_stack}"
    assert len(paren_stack) == 0, f"Unclosed parens in /arena script: {paren_stack}"
    assert len(bracket_stack) == 0, f"Unclosed brackets in /arena script: {bracket_stack}"

    # 2. Real JS Parser Validation (V8 via headless Chrome)
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        driver = webdriver.Chrome(options=options)
        try:
            # new Function(code) invokes ECMAScript V8 compiler without side-effects
            result = driver.execute_script('''
                var code = arguments[0];
                try {
                    new Function(code);
                    return { valid: true, error: null };
                } catch (e) {
                    return { valid: false, error: e.name + ": " + e.message };
                }
            ''', main_js)
            assert result["valid"] is True, f"V8 JS Parser Syntax Error: {result.get('error')}"
        finally:
            driver.quit()
    except Exception as e:
        # If Chrome browser is not available in test runner, structural parser above is authoritative
        if "webdriver" not in str(e).lower() and "chrome" not in str(e).lower():
            raise


def test_post_q24_single_results_screen_and_review_modal_contracts(client):
    """
    Regression test: Verifies that after Q24, the client routes directly to the
    single polished final summary screen (screen-results) without showing the redundant
    intermediate 24-question review screen (screen-reveal), and the detailed review
    is cleanly accessible via the 'Javoblarni Ko‘rish' button opening modal-review.
    """
    res = client.get("/arena")
    assert res.status_code == 200
    html = res.text

    # Contract 1: Primary completion screen is screen-results
    assert 'id="screen-results"' in html
    assert "Viktorina Yakunlandi!" in html
    assert 'id="results-correct-count"' in html
    assert 'id="results-incorrect-count"' in html
    assert 'id="results-unanswered-count"' in html
    assert 'id="results-active-time"' in html
    assert 'id="results-rounds-breakdown"' in html

    # Contract 2: Review button on final summary screen invokes openReviewModal()
    assert 'onclick="openReviewModal()"' in html
    assert 'id="results-review-btn-label"' in html
    assert "Javoblarni Ko‘rish" in html

    # Contract 3: Review Modal exists with backdrop close, footer close, and summary header
    assert 'id="modal-review"' in html
    assert 'onclick="if (event.target === this) closeReviewModal()"' in html
    assert 'onclick="closeReviewModal()"' in html
    assert 'id="review-questions-list"' in html

    # Contract 4: JavaScript routing on answer submission routes quiz_completed directly to handleFinalizeAndShowResults
    assert "else if (result.quiz_completed) {" in html
    assert "await handleFinalizeAndShowResults();" in html

    # Contract 5: Escape key closes review modal
    assert "if (e.key === 'Escape') {" in html
    assert "closeReviewModal()" in html

    # Contract 6: Session restoration on final round reveal automatically finalizes to results
    assert "if (data.is_final_round || data.quiz_completed || !data.has_next_round) {" in html


def test_full_24_question_zakovat_completion_flow_and_review_data(client):
    """
    End-to-End flow verification for 24-question Zakovat:
    - Q1-Q12: Answers submitted, 3-minute break triggered at Q12 (intermediate_break=True)
    - Continue: Transitions to Round 2 (Q13)
    - Q13-Q24: Answers submitted, Q24 completion returns quiz_completed=True
    - /continue: Marks attempt completed
    - /results: Returns final summary with exact scores, time, round breakdown
    - /review: Returns full 24-question answer review with accepted answers and explanations
    """
    res_quizzes = client.get("/api/quizzes")
    assert res_quizzes.status_code == 200
    quizzes = res_quizzes.json()
    assert len(quizzes) > 0, "At least one published quiz must exist"
    quiz_id = quizzes[0]["quiz_id"]

    # Start game
    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == 201
    start_data = start_res.json()
    token = start_data["session_token"]
    assert token is not None

    # Answer 24 questions
    for q_num in range(1, 25):
        ans_text = f"Test Answer {q_num}"
        sub_res = client.post(f"/api/play/{token}/answer", json={"answer": ans_text})
        assert sub_res.status_code == 200
        sub_data = sub_res.json()

        if q_num == 12:
            # Must trigger 3-minute break between rounds
            assert sub_data["intermediate_break"] is True
            assert sub_data["quiz_completed"] is False
            assert "break_data" in sub_data
            assert sub_data["break_data"]["completed_questions"] == 12

            # Resume Round 2
            cont_res = client.post(f"/api/play/{token}/continue")
            assert cont_res.status_code == 200
            assert cont_res.json()["status"] == "in_progress"
        elif q_num == 24:
            # Q24 completes the quiz
            assert sub_data["quiz_completed"] is True
            assert sub_data["round_completed"] is True
            assert sub_data["reveal_available"] is True
        else:
            assert sub_data["quiz_completed"] is False

    # Finalize completion
    cont_res = client.post(f"/api/play/{token}/continue")
    assert cont_res.status_code == 200
    assert cont_res.json()["status"] == "completed"

    # Results summary verification
    results_res = client.get(f"/api/play/{token}/results")
    assert results_res.status_code == 200
    results = results_res.json()
    assert results["status"] == "completed"
    assert results["total_questions"] == 24
    assert len(results["rounds"]) == 2
    assert "formatted_time" in results
    assert "total_score" in results

    # Answer Review verification
    review_res = client.get(f"/api/play/{token}/review")
    assert review_res.status_code == 200
    review_data = review_res.json()
    assert review_data["status"] == "completed"
    assert len(review_data["rounds"]) == 2
    total_rev_questions = sum(len(r["questions"]) for r in review_data["rounds"])
    assert total_rev_questions == 24
    for r in review_data["rounds"]:
        assert len(r["questions"]) == 12
        for q in r["questions"]:
            assert "text" in q
            assert "correct_answers" in q
            assert "submitted_answer" in q


# =============================================================================
# AUTHENTICATION REGRESSION TESTS (USERNAME & EMAIL LOGIN)
# =============================================================================

def test_login_by_exact_display_name_and_different_case_succeeds(client):
    """
    Regression test:
    Validates that a user can authenticate using:
    1. Exact display_name / username
    2. Case-insensitive display_name
    3. Exact email
    4. Case-insensitive email
    """
    import uuid
    uid = uuid.uuid4().hex[:8]
    test_username = f"User_{uid}"
    test_email = f"user_{uid}@example.com"
    test_password = "CorrectPassword123!"

    client.cookies.clear()
    reg = client.post("/api/auth/register", json={
        "display_name": test_username,
        "email": test_email,
        "password": test_password,
    })
    assert reg.status_code == 201

    # 1. Login by exact display_name
    client.cookies.clear()
    res_exact = client.post("/api/auth/login", json={
        "email": test_username,
        "password": test_password,
    })
    assert res_exact.status_code == 200, res_exact.text
    data_exact = res_exact.json()
    assert data_exact["success"] is True
    assert data_exact["user"]["display_name"] == test_username
    assert data_exact["user"]["email"] == test_email
    assert "access_token" in client.cookies

    # Verify session via /api/auth/me
    me_exact = client.get("/api/auth/me")
    assert me_exact.status_code == 200
    assert me_exact.json()["authenticated"] is True
    assert me_exact.json()["user"]["display_name"] == test_username

    # 2. Login by lowercase display_name
    client.cookies.clear()
    res_lower = client.post("/api/auth/login", json={
        "email": test_username.lower(),
        "password": test_password,
    })
    assert res_lower.status_code == 200
    assert res_lower.json()["user"]["display_name"] == test_username

    # 3. Login by uppercase display_name
    client.cookies.clear()
    res_upper = client.post("/api/auth/login", json={
        "email": test_username.upper(),
        "password": test_password,
    })
    assert res_upper.status_code == 200
    assert res_upper.json()["user"]["display_name"] == test_username

    # 4. Login by original email
    client.cookies.clear()
    res_email = client.post("/api/auth/login", json={
        "email": test_email,
        "password": test_password,
    })
    assert res_email.status_code == 200
    assert res_email.json()["user"]["display_name"] == test_username

    # 5. Login by uppercase email
    client.cookies.clear()
    res_email_upper = client.post("/api/auth/login", json={
        "email": test_email.upper(),
        "password": test_password,
    })
    assert res_email_upper.status_code == 200
    assert res_email_upper.json()["user"]["display_name"] == test_username


def test_login_invalid_password_and_nonexistent_identifier_fails_401(client):
    """
    Regression test:
    Validates that:
    1. Correct username + wrong password returns HTTP 401 with standard Uzbek error detail.
    2. Nonexistent username / email returns HTTP 401 with standard Uzbek error detail.
    3. Empty identifier returns HTTP 401 without server error.
    """
    import uuid
    uid = uuid.uuid4().hex[:8]
    test_username = f"Player_{uid}"
    test_email = f"player_{uid}@example.com"
    test_password = "ValidPassword123!"

    client.cookies.clear()
    reg = client.post("/api/auth/register", json={
        "display_name": test_username,
        "email": test_email,
        "password": test_password,
    })
    assert reg.status_code == 201

    # 1. Correct username + wrong password
    client.cookies.clear()
    res_wrong_pw = client.post("/api/auth/login", json={
        "email": test_username,
        "password": "WrongPassword999!",
    })
    assert res_wrong_pw.status_code == 401
    assert res_wrong_pw.json()["detail"] == "Email yoki parol noto'g'ri"
    assert "access_token" not in client.cookies

    # 2. Nonexistent identifier
    client.cookies.clear()
    res_nonexistent = client.post("/api/auth/login", json={
        "email": f"nobody_exists_{uid}",
        "password": "SomePassword123!",
    })
    assert res_nonexistent.status_code == 401
    assert res_nonexistent.json()["detail"] == "Email yoki parol noto'g'ri"

    # 3. Empty identifier
    client.cookies.clear()
    res_empty = client.post("/api/auth/login", json={
        "email": "   ",
        "password": "SomePassword123!",
    })
    assert res_empty.status_code == 401
    assert res_empty.json()["detail"] == "Email yoki parol noto'g'ri"


def test_frontend_login_in_flight_submission_guard(client):
    """
    Regression test:
    Validates that handleLoginSubmit() in templates/index.html enforces the
    in-flight submission guard (state.isAuthSubmitting) preventing duplicate concurrent requests.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    with open("templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    driver = webdriver.Chrome(options=options)

    try:
        driver.get("about:blank")
        driver.execute_script("""
            document.body.innerHTML = `
                <div id="auth-error-banner" class="hidden"></div>
                <button id="login-submit-btn">Tizimga kirish</button>
                <input id="login-username" value="TestUser" />
                <input id="login-password" value="TestPass123" />
            `;
            window.tailwind = { config: {} };
        """)

        import re
        scripts = re.findall(r"<script(?:\s+[^>]*)?>(.*?)</script>", html, re.DOTALL)
        for s in scripts:
            if s.strip():
                driver.execute_script("""
                    var scriptEl = document.createElement('script');
                    scriptEl.textContent = arguments[0];
                    document.head.appendChild(scriptEl);
                """, s)

        # Verify guard behavior:
        # 1. Mock fetch with delayed resolution
        # 2. Fire handleLoginSubmit twice rapidly
        # 3. Verify exactly 1 network fetch was dispatched
        res = driver.execute_async_script("""
            var done = arguments[arguments.length - 1];
            var fetchCount = 0;
            window.fetch = function(url) {
                if (url.includes('/auth/login')) {
                    fetchCount++;
                    return new Promise(function(resolve) {
                        setTimeout(function() {
                            resolve({
                                ok: true,
                                json: function() { return Promise.resolve({ success: true, user: { id: 1 } }); }
                            });
                        }, 50);
                    });
                }
                return Promise.resolve({ ok: true, json: function() { return Promise.resolve({}); } });
            };

            var fakeEvent = { preventDefault: function() {} };
            // First call dispatches fetch
            var p1 = handleLoginSubmit(fakeEvent);
            // Second call while p1 is in-flight must be guarded (no fetch)
            var p2 = handleLoginSubmit(fakeEvent);

            Promise.all([p1, p2]).then(function() {
                done({
                    fetchCount: fetchCount,
                    isAuthSubmittingAfter: state.isAuthSubmitting
                });
            }).catch(function(e) {
                done({ error: e.name + ': ' + e.message });
            });
        """)

        assert "error" not in res, f"JS execution error: {res.get('error')}"
        assert res["fetchCount"] == 1, f"Expected exactly 1 fetch call, got {res['fetchCount']}"
        assert res["isAuthSubmittingAfter"] is False, "Expected isAuthSubmitting to reset to false after request completes"
    finally:
        driver.quit()
