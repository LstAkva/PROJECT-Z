"""
Result sharing: /api/play/{token}/results returns a server-built share_text for real games only.
It names the pack, the score and the time and links to the pack — never a question, an answer, the
player's name or any id but the pack's. Owner demos and staging previews get none.
"""
import pytest
from fastapi import status

from tests.test_owner_demo import OWNER_EMAIL, start_demo
from tests.test_player_progress import CLASSIC_Q, _answer_classic, _finish, _hdr, _start, _user, make_classic_pack
from tests.test_svoyak_gameplay import create_svoyak_test_game


@pytest.fixture
def anon(client):
    client.cookies.clear()
    return client


@pytest.fixture
def player(db_session):
    return _user(db_session, "sharer@zakowhat.uz", "Ulashuvchi Ismi")


def results(client, token, user):
    res = client.get(f"/api/play/{token}/results", headers=_hdr(user))
    assert res.status_code == status.HTTP_200_OK, res.text
    return res.json()


def test_ranked_classic_share_text(anon, db_session, player):
    quiz = make_classic_pack(db_session, "Classic Zakovat — Pack 07")
    token = _start(anon, quiz, player)
    _answer_classic(anon, token, player, CLASSIC_Q, wrong={2})
    _finish(anon, token, player)
    text = results(anon, token, player)["share_text"]
    assert "🧠 ZakoWhat — Zakovat" in text
    assert "📚 Zakovat — Pack 07" in text                          # "Classic Zakovat" shortened, as in the UI
    assert f"🎯 {CLASSIC_Q - 1}/{CLASSIC_Q} to‘g‘ri javob" in text
    assert "mashq" not in text                                       # the first timed game is ranked
    assert text.rstrip().endswith(f"https://zakowhat.uz/quiz/{quiz.id}")
    # nothing private or spoiling
    for leak in ("Ulashuvchi", "sharer@", "Javob 1", "savol 1", str(player.id) + "\n"):
        assert leak not in text, leak


def test_practice_game_is_labelled(anon, db_session, player):
    quiz = make_classic_pack(db_session, "Mashq Pack")
    first = _start(anon, quiz, player)
    _answer_classic(anon, first, player, CLASSIC_Q)
    _finish(anon, first, player)
    again = anon.post(f"/api/play/{first}/restart", json={}, headers=_hdr(player)).json()["session_token"]
    _answer_classic(anon, again, player, CLASSIC_Q)
    _finish(anon, again, player)
    assert "mashq o‘yini" in results(anon, again, player)["share_text"]


def test_svoyak_share_text_uses_points(anon, db_session, player):
    quiz = create_svoyak_test_game(anon, db_session, "Svoyak Ulashish")
    token = _start(anon, quiz, player, svoyak_mode="free_choice")
    board = anon.get(f"/api/play/{token}/board", headers=_hdr(player)).json()["board"]
    for theme in board["themes"]:
        for cell in theme["cells"]:
            anon.post(f"/api/play/{token}/select", headers=_hdr(player),
                      json={"round_index": cell["round_index"], "question_index": cell["question_index"]})
            anon.post(f"/api/play/{token}/answer", json={"answer": "xato"}, headers=_hdr(player))
            anon.post(f"/api/play/{token}/continue", headers=_hdr(player))
    body = results(anon, token, player)
    text = body["share_text"]
    assert "🧠 ZakoWhat — Svoya Igra" in text and f"🏆 {body['total_score']} ball" in text
    assert "savol 10" not in text


def test_no_share_text_for_owner_demos(anon, db_session, monkeypatch):
    monkeypatch.setenv("OWNER_EMAIL", OWNER_EMAIL)
    owner = _user(db_session, OWNER_EMAIL, "Egasi")
    quiz = make_classic_pack(db_session, "Demo Pack")
    token = start_demo(anon, quiz, owner)["session_token"]
    anon.post(f"/api/demo/{token}/end", json={}, headers=_hdr(owner))
    assert results(anon, token, owner)["share_text"] is None


def test_results_screen_has_the_share_button(anon):
    html = anon.get("/arena").text
    assert 'id="results-share"' in html and "shareGameResult()" in html and "Natijani ulashish" in html
