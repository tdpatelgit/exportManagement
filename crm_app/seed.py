"""
seed.py
-------
Run this once, right after installing dependencies, to create the database
file and your first admin login:

    python seed.py

It is safe to run again later - it will not create a second admin if one
already exists, and it never touches existing leads/clients/data.
"""

import getpass
from app import create_app


def main():
    app = create_app()
    with app.app_context():
        container = app.container
        existing_admins = container.user_repo.list_all(role="admin")
        if existing_admins:
            print("An admin account already exists - nothing to do:")
            for admin in existing_admins:
                print(f"   - {admin.username} ({admin.full_name})")
            return

        print("No admin account found yet. Let's create the first one.\n")
        username = input("Admin username: ").strip() or "admin"
        full_name = input("Admin full name: ").strip() or "Administrator"
        password = getpass.getpass("Admin password: ").strip() or "admin123"

        container.auth_service.create_user(
            username=username, password=password, full_name=full_name, role="admin"
        )
        print(f"\nAdmin '{username}' created. You can now run `python run.py` and log in.")


if __name__ == "__main__":
    main()
