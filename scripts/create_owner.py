"""
ZakoWhat CLI — Initial Owner Account Creation / Password Reset Utility

This command-line script safely bootstraps or resets an owner account in the database.
Because public web self-registration is blocked for owner emails in non-test environments,
this script provides the sanctioned, secure mechanism for server administrators to initialize
their owner credentials.

Usage:
    python scripts/create_owner.py --email owner@zakowhat.uz --name "System Owner"
    python scripts/create_owner.py --email owner@zakowhat.uz --name "System Owner" --password "YourSecretPass123!"
"""

import sys
import argparse
import getpass
from database import SessionLocal, validate_database_environment
from models import User
from api.auth import hash_password, get_owner_emails


def main():
    parser = argparse.ArgumentParser(description="Create or update a ZakoWhat system owner account.")
    parser.add_argument("--email", required=True, help="Owner email address matching OWNER_EMAIL config.")
    parser.add_argument("--name", default="System Owner", help="Display name for the owner account.")
    parser.add_argument("--password", help="Account password (if omitted, will prompt securely).")
    args = parser.parse_args()

    clean_email = args.email.strip().lower()
    if "@" not in clean_email or "." not in clean_email:
        sys.exit("Error: Invalid email address format.")

    configured_owners = get_owner_emails()
    if configured_owners and clean_email not in configured_owners:
        print(f"[!] WARNING: '{clean_email}' is not in configured OWNER_EMAIL/OWNER_EMAILS.")
        print(f"    Configured owners: {configured_owners}")
        print("    Remember to set OWNER_EMAIL in your server environment to grant owner privileges.\n")

    password = args.password
    if not password:
        password = getpass.getpass("Enter owner password: ")
        confirm = getpass.getpass("Confirm owner password: ")
        if password != confirm:
            sys.exit("Error: Passwords do not match.")

    if len(password.encode("utf-8")) < 6:
        sys.exit("Error: Password must be at least 6 characters.")
    if len(password.encode("utf-8")) > 72:
        sys.exit("Error: Password cannot exceed 72 UTF-8 bytes (bcrypt limit).")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == clean_email).first()
        hashed = hash_password(password)

        if user:
            user.hashed_password = hashed
            user.display_name = args.name.strip() or user.display_name
            user.auth_provider = "local"
            db.commit()
            print(f"[OK] Successfully updated existing user '{clean_email}' with new owner password.")
        else:
            user = User(
                email=clean_email,
                hashed_password=hashed,
                display_name=args.name.strip() or "System Owner",
                auth_provider="local",
            )
            db.add(user)
            db.commit()
            print(f"[OK] Successfully created new owner account '{clean_email}'.")
    except Exception as e:
        db.rollback()
        sys.exit(f"Error during owner account creation: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
