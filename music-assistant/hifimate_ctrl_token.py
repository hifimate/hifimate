"""Run inside the Music Assistant image while the MA server is stopped.

Ensures a non-admin MA user for HiFiMate Control (role "user": queue and player
control only) and prints a long-lived token for it on stdout, using MA's
own database helper and JWT signer, exactly as MA's create_user /
create_token do. The installer redirects stdout into PREFIX/config/ma-token
(0600) and never echoes it.

The user has no password, so it cannot log in to MA's web UI: MA's login
checks the password link this user never has (any added in MA's UI is
removed here). Only the token works.

stdin (optional): the token currently stored by HiFi (not argv: that is
visible in the process list). When it is still a
valid token of that user with more than a year left, it is printed again
unchanged, so a reinstall does not pile up tokens. Older HiFi tokens of the user are revoked
when a new one is issued.

Exit 3 without output when MA has no real user yet: creating ours first
would make MA skip its own onboarding (the first user becomes the owner).
The installer asks for that first user before this runs (scripts/ma_setup.py).
"""
import asyncio
import hashlib
import json
import secrets
import sys
from datetime import timedelta

from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.jwt_auth import JWTHelper
from music_assistant_models.auth import User, UserRole

USERNAME = 'hifimate-ctrl'   # the MA user; existing installs already have it
DISPLAY = 'HiFiMate Control'
TOKEN_NAME = 'HiFiMate Control (managed)'   # matched by name on reinstall: keep
DAYS = 3650
RENEW_BEFORE = timedelta(days=365)
SYSTEM_USERS = {'homeassistant_system_user'}


async def main(current):
    db = DatabaseConnection('/data/auth.db')
    await db.setup()
    try:
        users = await db.get_rows('users', limit=1000)
        if not any(row['username'] not in SYSTEM_USERS | {USERNAME} for row in users):
            return 3
        secret = await db.get_row('settings', {'key': 'jwt_secret'})
        if not secret:
            return 3
        jwt = JWTHelper(secret['value'])
        row = await db.get_row('users', {'username': USERNAME})
        if row is None:
            user = User(user_id=secrets.token_urlsafe(32), username=USERNAME, role=UserRole.USER.value, display_name=DISPLAY)
            await db.insert('users', {'user_id': user.user_id, 'username': USERNAME, 'role': user.role, 'enabled': True,
                                      'created_at': utc().isoformat(), 'display_name': DISPLAY, 'avatar_url': None,
                                      'preferences': '{}', 'player_filter': '[]', 'provider_filter': '[]'})
        else:
            if row['role'] != UserRole.USER.value or not row['enabled']:
                # Keep it least-privileged and usable even if someone edited it in MA.
                await db.execute_write('UPDATE users SET role = :role, enabled = 1 WHERE user_id = :id',
                                       {'role': UserRole.USER.value, 'id': row['user_id']})
            user = User(user_id=row['user_id'], username=USERNAME, role=UserRole.USER.value)
        await db.execute_write('DELETE FROM user_auth_providers WHERE user_id = :id', {'id': user.user_id})
        if current:
            stored = await db.get_row('auth_tokens', {'token_hash': hashlib.sha256(current.encode()).hexdigest()})
            if stored and stored['user_id'] == user.user_id and stored['expires_at'] > (utc() + RENEW_BEFORE).isoformat():
                try:
                    jwt.decode_token(current)
                    print(current)
                    return 0
                except Exception:
                    pass
        await db.execute_write('DELETE FROM auth_tokens WHERE user_id = :id AND name = :name',
                               {'id': user.user_id, 'name': TOKEN_NAME})
        token_id = secrets.token_urlsafe(32)
        created = utc(); expires = created + timedelta(days=DAYS)
        token = jwt.encode_token(user=user, token_id=token_id, token_name=TOKEN_NAME, expires_at=expires, is_long_lived=True)
        await db.insert('auth_tokens', {'token_id': token_id, 'user_id': user.user_id,
                                        'token_hash': hashlib.sha256(token.encode()).hexdigest(), 'name': TOKEN_NAME,
                                        'created_at': created.isoformat(), 'expires_at': expires.isoformat(), 'is_long_lived': 1})
        print(token)
        return 0
    finally:
        await db.close()


if __name__ == '__main__':
    sys.exit(asyncio.run(main(sys.stdin.read(8192).strip())))
