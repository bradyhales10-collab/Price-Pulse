from __future__ import annotations

import json


def test_newer_json_backup_is_imported_once(tmp_path, monkeypatch) -> None:
    import app.auth_session as auth
    import app.browser_profile as profiles

    monkeypatch.setattr(auth, "PRIVATE_DIR", tmp_path)
    monkeypatch.setattr(profiles, "PRIVATE_DIR", tmp_path)
    state_path = auth.auth_state_path_for("partzilla")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "__Host-session",
                        "value": "fresh",
                        "domain": "www.partzilla.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )

    class Context:
        def __init__(self) -> None:
            self.added = []

        def add_cookies(self, cookies) -> None:
            self.added.extend(cookies)

    context = Context()
    profile_dir = profiles.browser_profile_dir("partzilla")
    profile_dir.mkdir(parents=True)

    profiles._import_newer_storage_backup(context, "partzilla", profile_dir)
    profiles._import_newer_storage_backup(context, "partzilla", profile_dir)

    assert [cookie["value"] for cookie in context.added] == ["fresh"]
