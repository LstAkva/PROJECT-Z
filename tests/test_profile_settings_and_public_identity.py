import base64
import secrets
import pytest
from fastapi import status


def test_update_profile_display_name_and_bio_and_preferences(client):
    uid = secrets.token_hex(4)
    email = f"profiler_{uid}@zakowhat.uz"
    
    # Register user
    reg = client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
        "display_name": "Original Name"
    })
    assert reg.status_code == status.HTTP_201_CREATED

    # Patch profile
    patch_res = client.patch("/api/auth/profile", json={
        "display_name": "Updated Name",
        "bio": "Intellektual o'yinlar ishqibozi.",
        "is_public": False,
        "show_stats": False,
        "theme_preference": "light"
    })
    assert patch_res.status_code == status.HTTP_200_OK
    data = patch_res.json()["user"]
    assert data["display_name"] == "Updated Name"
    assert data["bio"] == "Intellektual o'yinlar ishqibozi."
    assert data["is_public"] is False
    assert data["show_stats"] is False
    assert data["theme_preference"] == "light"

    # Verify via /api/auth/me
    me_res = client.get("/api/auth/me")
    assert me_res.status_code == status.HTTP_200_OK
    me_data = me_res.json()["user"]
    assert me_data["display_name"] == "Updated Name"
    assert me_data["is_public"] is False


def test_update_profile_username_validation_and_uniqueness(client):
    uid1 = secrets.token_hex(4)
    uid2 = secrets.token_hex(4)
    u_handle = f"alpha_{uid1}"
    
    # Register user 1
    reg1 = client.post("/api/auth/register", json={
        "email": f"user_{uid1}@zakowhat.uz",
        "password": "Password123!",
        "display_name": "User Alpha"
    })
    assert reg1.status_code == status.HTTP_201_CREATED

    # Set valid username
    res1 = client.patch("/api/auth/profile", json={"username": u_handle})
    assert res1.status_code == status.HTTP_200_OK
    assert res1.json()["user"]["username"] == u_handle

    # Test invalid username characters
    res_inv = client.patch("/api/auth/profile", json={"username": "alpha@bad#name!"})
    assert res_inv.status_code == status.HTTP_400_BAD_REQUEST

    # Test too short username
    res_short = client.patch("/api/auth/profile", json={"username": "al"})
    assert res_short.status_code == status.HTTP_400_BAD_REQUEST

    # Register user 2 (overwrites auth cookie)
    reg2 = client.post("/api/auth/register", json={
        "email": f"user_{uid2}@zakowhat.uz",
        "password": "Password123!",
        "display_name": "User Beta"
    })
    assert reg2.status_code == status.HTTP_201_CREATED

    # Try claiming user 1's username
    res_dup = client.patch("/api/auth/profile", json={"username": u_handle})
    assert res_dup.status_code == status.HTTP_400_BAD_REQUEST
    assert "band" in res_dup.json()["detail"].lower()


def test_upload_avatar_valid_and_delete(client):
    uid = secrets.token_hex(4)
    reg = client.post("/api/auth/register", json={
        "email": f"avatar_{uid}@zakowhat.uz",
        "password": "Password123!",
        "display_name": "Avatar Tester"
    })
    assert reg.status_code == status.HTTP_201_CREATED

    # 1x1 valid PNG transparent pixel
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    b64_png = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    up_res = client.post("/api/auth/avatar", json={"image_data": b64_png})
    assert up_res.status_code == status.HTTP_200_OK
    avatar_url = up_res.json()["avatar_url"]
    assert avatar_url.startswith("/static/uploads/avatars/avatar_u")

    # Check via me
    me = client.get("/api/auth/me").json()["user"]
    assert me["avatar_url"] == avatar_url

    # Delete avatar
    del_res = client.delete("/api/auth/avatar")
    assert del_res.status_code == status.HTTP_200_OK
    assert del_res.json()["avatar_url"] is None

    me2 = client.get("/api/auth/me").json()["user"]
    assert me2["avatar_url"] is None


def test_upload_avatar_rejects_fake_file(client):
    uid = secrets.token_hex(4)
    reg = client.post("/api/auth/register", json={
        "email": f"fake_{uid}@zakowhat.uz",
        "password": "Password123!",
        "display_name": "Fake Avatar"
    })
    assert reg.status_code == status.HTTP_201_CREATED

    fake_bytes = b"Hello this is not an image file at all!"
    b64_fake = "data:image/png;base64," + base64.b64encode(fake_bytes).decode("ascii")

    up_res = client.post("/api/auth/avatar", json={"image_data": b64_fake})
    assert up_res.status_code == status.HTTP_400_BAD_REQUEST


def test_change_password(client):
    uid = secrets.token_hex(4)
    email = f"pwd_{uid}@zakowhat.uz"
    reg = client.post("/api/auth/register", json={
        "email": email,
        "password": "OldPassword123!",
        "display_name": "Pwd Tester"
    })
    assert reg.status_code == status.HTTP_201_CREATED

    # Wrong current password
    bad_res = client.post("/api/auth/change-password", json={
        "current_password": "WrongPassword!",
        "new_password": "NewPassword456!"
    })
    assert bad_res.status_code == status.HTTP_400_BAD_REQUEST

    # Correct current password
    ok_res = client.post("/api/auth/change-password", json={
        "current_password": "OldPassword123!",
        "new_password": "NewPassword456!"
    })
    assert ok_res.status_code == status.HTTP_200_OK

    # Log out
    client.post("/api/auth/logout")

    # Login with old password fails
    fail_login = client.post("/api/auth/login", json={
        "email": email,
        "password": "OldPassword123!"
    })
    assert fail_login.status_code == status.HTTP_401_UNAUTHORIZED

    # Login with new password succeeds
    good_login = client.post("/api/auth/login", json={
        "email": email,
        "password": "NewPassword456!"
    })
    assert good_login.status_code == status.HTTP_200_OK


def test_public_profile_respects_privacy(client):
    uid = secrets.token_hex(4)
    email = f"public_{uid}@zakowhat.uz"
    u_handle = f"master_{uid}"
    
    # Create public user
    reg = client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
        "display_name": "Public Master"
    })
    assert reg.status_code == status.HTTP_201_CREATED
    
    patch_res = client.patch("/api/auth/profile", json={
        "username": u_handle,
        "bio": "Chempion 2026",
        "is_public": True,
        "show_stats": True
    })
    assert patch_res.status_code == status.HTTP_200_OK

    # Look up by username
    pub_res = client.get(f"/api/users/{u_handle}/public")
    assert pub_res.status_code == status.HTTP_200_OK
    pub_data = pub_res.json()
    assert pub_data["display_name"] == "Public Master"
    assert pub_data["username"] == u_handle
    assert pub_data["bio"] == "Chempion 2026"
    assert pub_data["is_private"] is False
    assert pub_data["stats"] is not None
    assert "email" not in pub_data

    # Make profile private
    priv_patch = client.patch("/api/auth/profile", json={"is_public": False})
    assert priv_patch.status_code == status.HTTP_200_OK

    # Query public endpoint again
    priv_res = client.get(f"/api/users/{u_handle}/public")
    assert priv_res.status_code == status.HTTP_200_OK
    priv_data = priv_res.json()
    assert priv_data["is_private"] is True
    assert priv_data.get("bio") is None
    assert priv_data.get("stats") is None
    assert "email" not in priv_data


def test_consent_endpoint(client):
    uid = secrets.token_hex(4)
    reg = client.post("/api/auth/register", json={
        "email": f"consent_{uid}@zakowhat.uz",
        "password": "Password123!",
        "display_name": "Consent Tester"
    })
    assert reg.status_code == status.HTTP_201_CREATED

    res = client.post("/api/auth/consent", json={"cookie_consent": "all"})
    assert res.status_code == status.HTTP_200_OK
    assert res.json()["cookie_consent"] == "all"
