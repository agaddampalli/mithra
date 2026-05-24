# Nango OAuth Setup

Mithra uses self-hosted Nango (`nangohq/nango-server:hosted`) for all Google OAuth.
The integration credentials and scopes are configured once via the Nango dashboard
or direct PostgreSQL — not at runtime.

## One-time integration setup

Nango's hosted image has no write API for integrations (PUT/PATCH/DELETE all return
404). Configure via psql on the host running the stack:

```bash
docker exec -it mithra-nango-db-1 psql -U nango -d nango
```

```sql
INSERT INTO _nango_configs (
  unique_key, provider, oauth_client_id, oauth_client_secret,
  oauth_scopes, environment_id, created_at, updated_at
) VALUES (
  'google-calendar', 'google',
  '<GOOGLE_CLIENT_ID>', '<GOOGLE_CLIENT_SECRET>',
  'openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/calendar.events',
  1,  -- prod environment
  NOW(), NOW()
) ON CONFLICT (unique_key, environment_id) DO UPDATE
  SET oauth_client_id = EXCLUDED.oauth_client_id,
      oauth_client_secret = EXCLUDED.oauth_client_secret,
      oauth_scopes = EXCLUDED.oauth_scopes,
      updated_at = NOW();
```

## Check the prod secret key

```bash
docker exec -it mithra-nango-db-1 psql -U nango -d nango \
  -c "SELECT id, name, secret_key FROM _nango_environments;"
```

Set `NANGO_SECRET_KEY` in `.env` to the `secret_key` from the prod row (id=1).

## Remove stale direct OAuth tokens (one-time cleanup after migration)

Previous versions stored Google tokens directly in SQLite via GoogleIntegration.
After deploying this version, remove those stale records:

```bash
docker exec -it mithra-api-gateway-1 python -c "
import sqlite3
conn = sqlite3.connect('/data/mithra/mithra.db')
conn.execute(
    'DELETE FROM records WHERE domain=? AND key LIKE ?',
    ('integrations', 'google:%'))
conn.commit()
print('Removed stale direct OAuth tokens.')
conn.close()
"
```

Nango connection IDs are stored at `domain=integrations, key=nango_connection:{user_id}`
and are written by the Nango webhook after each successful OAuth flow.

## OAuth flow (after setup)

1. User sends "connect google" in Telegram
2. Bot calls `NangoClient.create_connect_session()` → gets `connect_link`
3. Bot sends `connect_link` directly to the user
4. User completes OAuth in browser
5. Nango sends POST to `https://assistant.mithra-app.com/api/v1/webhook/nango`
6. Webhook stores the Nango UUID connection_id for the user
7. Calendar API calls use that UUID to fetch fresh tokens from Nango
