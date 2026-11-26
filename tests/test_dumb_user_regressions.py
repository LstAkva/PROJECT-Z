"""
Regressions found by clicking through the site like a careless user (26 Nov 2026):
invisible names, placeholder emails for non-Latin names, shared names at sign-in, huge ids in URLs,
blank Kun savoli answers, invisible room team names.
"""
import pytest
from fastapi import status

from api.auth import is_generated_email
from models import User
from services.text import clean_visible_text
from tests.test_player_progress import _hdr, _user

ZWSP, RLO, NUL = "​", "‮", "\x00"


@pytest.fixture
def anon(client):
    client.cookies.clear()
    return client


def register(client, email, name, password="Parol123!"):
    client.cookies.clear()
    return client.post("/api/auth/register", json={"email": email, "password": password, "display_name": name,
                                                   "accept_terms": True})


def test_clean_visible_text():
    assert clean_visible_text(ZWSP * 3) == "" and clean_visible_text(RLO) == "" and clean_visible_text(NUL) == ""
    assert clean_visible_text(f"  A{ZWSP}li \n Valiyev\t") == "Ali Valiyev"
    assert clean_visible_text("O‘g‘il 😀") == "O‘g‘il 😀"           # Uzbek apostrophes and emoji stay
    assert clean_visible_text("a" * 50, 10) == "a" * 10 and clean_visible_text(None) == ""


@pytest.mark.parametrize("name", [ZWSP, ZWSP * 3, RLO, NUL, " \n\t ", f"{ZWSP} {RLO}"])
def test_invisible_names_are_refused(anon, db_session, name):
    res = register(anon, f"inv{abs(hash(name)) % 100000}@zakowhat.test", name)
    assert res.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    user = _user(db_session, f"p{abs(hash(name)) % 100000}@zakowhat.test", "Asl Ism")
    res = anon.patch("/api/auth/profile", json={"display_name": name}, headers=_hdr(user))
    assert res.status_code == 400
    db_session.refresh(user)
    assert user.display_name == "Asl Ism"


def test_hidden_characters_inside_a_name_are_removed(anon, db_session):
    assert register(anon, "hid@zakowhat.test", f"A{ZWSP}li{RLO}").status_code == 201
    assert db_session.query(User).filter_by(email="hid@zakowhat.test").one().display_name == "Ali"


@pytest.mark.parametrize("email", ["@zakowhat.uz", "a@b", "two@@x.uz", "has space@x.uz", "noat.uz"])
def test_malformed_emails_are_refused(anon, email):
    assert register(anon, email, "Kimdir").status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_name_only_signup_conflict_talks_about_the_name(anon):
    assert register(anon, "sardor@zakowhat.uz", "Sardor").status_code == 201
    res = register(anon, "sardor@zakowhat.uz", "SARDOR!")
    assert res.status_code == 409 and "nom" in res.json()["detail"]


def test_placeholder_for_non_latin_names_counts_as_generated(db_session):
    user = _user(db_session, "player3fa9c1@zakowhat.uz", "Шерзод")
    assert is_generated_email(user) is True


def test_forms_never_build_an_empty_placeholder_address(anon):
    for page in ("/arena", "/"):
        assert "'player' + Math.random()" in anon.get(page).text, page


def test_people_sharing_a_name_can_each_sign_in(anon, db_session):
    assert register(anon, "ali.one@zakowhat.test", "Ali", password="Birinchi1!").status_code == 201
    assert register(anon, "ali@zakowhat.uz", "Ali", password="Ikkinchi2!").status_code == 201
    for password, email in (("Birinchi1!", "ali.one@zakowhat.test"), ("Ikkinchi2!", "ali@zakowhat.uz")):
        anon.cookies.clear()
        res = anon.post("/api/auth/login", json={"email": "ali", "password": password})
        assert res.status_code == 200, res.text
        assert anon.get("/api/auth/me").json()["user"]["email"] == email
    anon.cookies.clear()
    assert anon.post("/api/auth/login", json={"email": "Ali", "password": "Uchinchi3!"}).status_code == 401


def test_sign_in_with_the_at_username(anon, db_session):
    user = _user(db_session, "handle@zakowhat.test", "Ism Familiya")
    user.username = "chempion_7"
    db_session.commit()
    anon.cookies.clear()
    assert anon.post("/api/auth/login", json={"email": "@chempion_7", "password": "Pass123!"}).status_code == 200


@pytest.mark.parametrize("path", ["/api/quizzes/99999999999999999999", "/api/users/99999999999999999999/public",
                                  "/api/quizzes/99999999999999999999/leaderboard"])
def test_huge_ids_in_urls_are_not_found_not_server_errors(anon, path):
    assert anon.get(path).status_code == status.HTTP_404_NOT_FOUND


def test_starting_a_game_with_a_huge_id(anon, db_session):
    user = _user(db_session, "huge@zakowhat.test", "Katta Son")
    res = anon.post("/api/play/start/99999999999999999999", json={"timer_mode": "with_timer"}, headers=_hdr(user))
    assert res.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.parametrize("answer", [" ", "   ", ZWSP, f"{ZWSP}{RLO}"])
def test_blank_daily_answers_are_refused_without_spending_a_try(anon, db_session, answer):
    user = _user(db_session, f"daily{abs(hash(answer)) % 10000}@zakowhat.test", "Kunlik")
    res = anon.post("/api/daily/submit", json={"answer": answer}, headers=_hdr(user))
    assert res.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_invisible_room_team_names_are_refused(anon, db_session, monkeypatch):
    from tests.test_player_progress import make_classic_pack
    monkeypatch.setenv("ROOMS_PLAYER_JOIN", "1")
    monkeypatch.setenv("OWNER_EMAIL", "rooms-dumb@zakowhat.uz")
    owner = _user(db_session, "rooms-dumb@zakowhat.uz", "Boshlovchi")
    pack = make_classic_pack(db_session, "Ko‘rinmas")
    code = anon.post("/api/rooms", json={"quiz_id": pack.id}, headers=_hdr(owner)).json()["code"]
    assert anon.post("/api/rooms/join", json={"code": code, "team_name": ZWSP * 4}).status_code == 422
    res = anon.post("/api/rooms/join", json={"code": code, "team_name": f"Fiz{ZWSP}iklar"})
    assert res.status_code == 201 and res.json()["team"]["name"] == "Fiziklar"


# --- pages a browser can land on by a typo or an old link --------------------------------------------------------

HTML = {"Accept": "text/html,application/xhtml+xml"}


@pytest.mark.parametrize("path", ["/quiz/abc", "/play/abc", "/play/-1", "/quiz/99999999999999999999"])
def test_non_numeric_pack_addresses_get_the_page_not_raw_json(anon, path):
    res = anon.get(path, headers=HTML)
    assert res.status_code == 200 and "text/html" in res.headers["content-type"], path
    assert '"int_parsing"' not in res.text


@pytest.mark.parametrize("path", ["/abcdef", "/arena/extra/bits", "/static-missing", "/u/"])
def test_unknown_pages_get_a_friendly_404(anon, path):
    res = anon.get(path, headers=HTML)
    assert res.status_code == 404 and "Sahifa topilmadi" in res.text and 'href="/arena"' in res.text
    assert res.headers["x-robots-tag"] == "noindex"


def test_api_and_non_browser_404s_stay_json(anon):
    assert anon.get("/api/does-not-exist", headers=HTML).json() == {"detail": "Not Found"}
    assert anon.get("/abcdef").json() == {"detail": "Not Found"}


def test_game_rules_are_shown_as_notices_and_play_links_fix_their_address(anon):
    page = anon.get("/arena").text
    assert "if (res.status < 500 && apiDetail(err)) { await zwAlert(apiDetail(err)); return; }" in page
    assert "playMatch[1] !== String(state.currentQuizId)" in page
    assert 'title: "Profil topilmadi"' in page
