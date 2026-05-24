# Mithra — Foundational Architecture Decisions

## Purpose
This document records the WHY behind key architectural
choices. Before implementing any component, verify it
aligns with these decisions. If a decision needs to
change, update this document first — not the code.

---

## 1. Identity & Household Management

### Decision
Members and households are stored in SQLite from day one.
household.yaml contains only the bootstrap anchor (household_id).

### Schema

#### households table
| Column       | Type | Notes                          |
|--------------|------|--------------------------------|
| id           | TEXT | PRIMARY KEY e.g. "hh_a3f8b2c1"|
| display_name | TEXT | "Gaddampalli Family"           |
| timezone     | TEXT | "America/Chicago"              |
| locale       | TEXT | "en-US"                        |
| created_at   | TEXT | ISO8601                        |
| updated_at   | TEXT | ISO8601                        |

#### members table
| Column       | Type | Notes                          |
|--------------|------|--------------------------------|
| id           | TEXT | PRIMARY KEY e.g. "usr_9f2ec4d1"|
| household_id | TEXT | FK -> households.id            |
| display_name | TEXT | "Nikitha"                      |
| role         | TEXT | "admin" or "member"            |
| created_at   | TEXT | ISO8601                        |
| updated_at   | TEXT | ISO8601                        |

#### member_channels table
| Column       | Type    | Notes                      |
|--------------|---------|----------------------------|
| user_id      | TEXT    | FK -> members.id           |
| household_id | TEXT    |                            |
| channel      | TEXT    | "telegram" or "whatsapp"   |
| channel_id   | TEXT    | e.g. Telegram chat_id      |
| verified     | INTEGER | 1 = verified               |
| created_at   | TEXT    | ISO8601                    |
| PRIMARY KEY  | (user_id, channel)         |

#### member_preferences table
| Column       | Type | Notes                        |
|--------------|------|------------------------------|
| user_id      | TEXT | FK -> members.id             |
| household_id | TEXT |                              |
| key          | TEXT | e.g. "calendar_provider"     |
| value        | TEXT | e.g. "google"                |
| updated_at   | TEXT | ISO8601                      |
| PRIMARY KEY  | (user_id, key)               |

#### household_config table
| Column       | Type | Notes                        |
|--------------|------|------------------------------|
| household_id | TEXT | FK -> households.id          |
| key          | TEXT | e.g. "default_calendar"      |
| value        | TEXT | e.g. "google"                |
| updated_at   | TEXT | ISO8601                      |
| PRIMARY KEY  | (household_id, key)          |

### ID Format
    household_id: "hh_" + secrets.token_hex(4)  e.g. "hh_a3f8b2c1"
    user_id:      "usr_" + secrets.token_hex(4) e.g. "usr_9f2ec4d1"

Generated ONCE at setup. Never regenerated. Never reused.

### household.yaml (minimal bootstrap only)
Contains exactly ONE line:
    household_id: hh_a3f8b2c1

That is the ONLY content. Everything else in SQLite.

### Bootstrap Sequence
1. Read household_id from household.yaml
2. Check if households table has this id
3. If empty -> prompt for display_name, timezone
4. Seed households table
5. All subsequent startups -> read from SQLite only

### Product Migration Path
Personal: household_id from household.yaml at startup
Product:  household_id from JWT token per request
Zero code changes below the gateway layer.

### MemberRegistry Component
Single source of truth for all identity operations.
Replaces UserIdentityResolver entirely.
All channel resolution goes through here.

Interface:
    get_member(user_id) -> Member | None
    get_members() -> list[Member]
    get_admin() -> Member | None
    is_admin(user_id) -> bool
    resolve_channel(channel, channel_id) -> Member | None
    register_channel(user_id, channel, channel_id) -> None
    get_channel_id(user_id, channel) -> str | None
    add_member(display_name, role) -> Member
    set_preference(user_id, key, value) -> None
    get_preference(user_id, key, default) -> str
    block_channel(channel, channel_id) -> None
    is_blocked(channel, channel_id) -> bool

---

## 2. OAuth Token Management

### Decision
Build provider-agnostic OAuth system using standard
libraries. No managed OAuth service.
Nango evaluated and deferred — see Alternatives Considered.

### Why Not Nango
- Free self-hosted lacks webhooks (by design, permanent)
- Enterprise self-hosted requires paid license
- No acceptable free open-source alternative exists
- Direct implementation is ~100 lines per provider
- StorageAdapter abstraction makes product migration clean

### Components

#### OAuthProvider (abstract base) — oauth_provider.py
Abstract interface all providers must implement:
    provider_id (property) -> str        e.g. "google"
    scopes (property) -> list[str]
    auth_url (property) -> str
    token_url (property) -> str
    get_auth_url(state, redirect_uri) -> str
    exchange_code(code, redirect_uri) -> dict
    refresh(token_data) -> dict

#### OAuthTokenStore — oauth_token_store.py
Encrypted storage for all provider tokens.
Storage location:
    domain: "oauth"
    key:    "{provider_id}:{user_id}"
    layer:  "credentials"

Token data fields stored:
    provider           str   provider identifier
    user_id            str   member id
    household_id       str   household id
    access_token       str   ENCRYPTED
    refresh_token      str   ENCRYPTED (optional)
    token_type         str   default "Bearer"
    expires_at         int   Unix timestamp
    scopes             list  granted scopes
    connected_at       str   ISO8601
    last_refreshed     str   ISO8601
    provider_user_id   str   user's ID at provider

Methods:
    save(provider_id, user_id, token_data) -> None
    load(provider_id, user_id) -> dict | None
    delete(provider_id, user_id) -> None
    is_connected(provider_id, user_id) -> bool

#### OAuthManager — oauth_manager.py
Orchestrates the full OAuth flow.
Auto-refreshes tokens with 5-minute buffer.

Methods:
    register(provider) -> None
    get_auth_url(provider_id, user_id, redirect_uri) -> str
    handle_callback(provider_id, code, user_id, redirect_uri) -> bool
    get_token(provider_id, user_id) -> str | None
    is_connected(provider_id, user_id) -> bool
    disconnect(provider_id, user_id) -> None
    registered_providers (property) -> list[str]

#### Implemented Providers
    GoogleOAuth     google_oauth.py    implement now
                    Uses: authlib for flow
                          google-auth-oauthlib for refresh
                    Scopes: calendar, gmail.readonly,
                            contacts.readonly, openid,
                            email, profile

    MicrosoftOAuth  microsoft_oauth.py stub only
                    Implement when OneDrive domain is built
                    Uses: authlib for flow, msal for refresh
                    Scopes: Files.ReadWrite,
                            Calendars.ReadWrite,
                            Contacts.ReadWrite, offline_access

#### Adding a New Provider (future pattern)
1. Create {provider}_oauth.py
2. Subclass OAuthProvider
3. Implement 4 abstract methods
4. Add CLIENT_ID and CLIENT_SECRET env vars
5. Bootstrap auto-registers if env vars present
Result: Zero changes to gateway, manager, store, or adapters.

### OAuth Routes (generic, provider-agnostic)
    GET /auth/{provider}           redirect to provider
    GET /auth/{provider}/callback  exchange code, store token

Works for any provider. No provider-specific route code.

### Nango — What To Do With It
Remove Nango entirely from the stack.
- Remove nango service from compose.yaml
- Remove nango-db service from compose.yaml
- Remove nango-data and nango-db-data volumes
- Remove NANGO_* env vars from all services
- Delete mithra/integrations/nango_client.py
- Remove _setup_nango() from bootstrap.py
- Remove NANGO_* from .env.example

### Product Migration Path
Personal: tokens in encrypted SQLite
Product:  swap StorageAdapter backend to cloud storage
          per tenant — CalendarAdapter unchanged.
          One-day migration when needed.

---

## 3. Storage Architecture

### Decision
StorageAdapter abstraction with primary key:
(domain, key, layer, user_id, household_id)

household_id scoping means records never collide
across households. Zero schema changes for product.

### Domain Naming Convention
    "oauth"         OAuth tokens (all providers)
    "sessions"      Conversation sessions
    "autonomy"      Action log, pending actions
    "observability" System logs
    "time"          Calendar domain data
    "people"        Contacts domain data
    "money"         Finance domain data
    "health"        Health domain (local ONLY, never cloud)

### Layer Convention
    "credentials"   Sensitive auth data (always encrypted)
    "state"         Current state (session, preferences)
    "facts"         Long-term knowledge (domain memory)
    "history"       Audit trail (append only)

---

## 4. Migration Plan (Current -> Target)

### Current State
    household.yaml -> has members list with person_a, person_b
    SQLite records -> user_id="person_a",
                      household_id="mithra_household_001"
    Identity data  -> stored in records table as domain="identity"

### Step 1 — Generate New IDs
    Generate and record these values:
    household_id: hh_XXXX  (replaces "mithra_household_001")
    user_nikitha: usr_XXXX (replaces "person_a")
    user_wife:    usr_YYYY (replaces "person_b")

    Generation command:
    python -c "import secrets; print('hh_' + secrets.token_hex(4))"
    python -c "import secrets; print('usr_' + secrets.token_hex(4))"

### Step 2 — Create New Tables
    CREATE TABLE households (...)
    CREATE TABLE members (...)
    CREATE TABLE member_channels (...)
    CREATE TABLE member_preferences (...)
    CREATE TABLE household_config (...)

### Step 3 — Seed Households Table
    INSERT INTO households (id, display_name, timezone, ...)
    VALUES ('hh_XXXX', 'Gaddampalli Family', 'America/Chicago', ...)

### Step 4 — Seed Members Table
    INSERT INTO members (id, household_id, display_name, role, ...)
    VALUES ('usr_XXXX', 'hh_XXXX', 'Nikitha', 'admin', ...)

    INSERT INTO members (id, household_id, display_name, role, ...)
    VALUES ('usr_YYYY', 'hh_XXXX', 'Wife', 'member', ...)

### Step 5 — Migrate Member Channels
    Read all records WHERE domain='identity'
    For each record with type='telegram_mapping':
        INSERT INTO member_channels (user_id, household_id,
            channel, channel_id, verified, created_at)
        Map old user_id strings to new usr_XXXX IDs
    DELETE FROM records WHERE domain='identity'

### Step 6 — Update All Existing Records
    UPDATE records
    SET household_id = 'hh_XXXX'
    WHERE household_id = 'mithra_household_001';

    UPDATE records
    SET user_id = 'usr_XXXX'
    WHERE user_id = 'person_a';

    UPDATE records
    SET user_id = 'usr_YYYY'
    WHERE user_id = 'person_b';

### Step 7 — Update household.yaml
    Remove members list entirely.
    Update household_id value to hh_XXXX.
    Final file content:
        household_id: hh_XXXX

### Step 8 — Code Changes
    Replace UserIdentityResolver with MemberRegistry.
    Update all user_id references from "person_a" style
    to MemberRegistry lookups returning "usr_XXXX" style.
    Update bootstrap to not read members from yaml.
    Update Telegram registration to use MemberRegistry.

---

## 5. Implementation Order

### Phase A — Identity Foundation
Must complete and verify before Phase B.

    A1. Create new SQLite tables
        (households, members, member_channels,
         member_preferences, household_config)
        Add to LocalStorageAdapter initialization.
        Add to OneDriveAdapter initialization.

    A2. Run migration
        Steps 1-7 from Migration Plan above.
        Write migration as a script not inline code.
        Verify: all 72 existing records updated correctly.

    A3. Implement MemberRegistry
        New file: mithra/identity/member_registry.py
        Backed by new SQLite tables.
        Full interface as defined above.

    A4. Replace UserIdentityResolver with MemberRegistry
        Remove mithra/adapters/messaging/telegram_input.py
        UserIdentityResolver class.
        Replace all usages with MemberRegistry.

    A5. Update Telegram registration flow
        /start -> MemberRegistry.get_members() for buttons
        Admin approve -> MemberRegistry.register_channel()
        All identity stored in member_channels table.

    A6. Verify Telegram end to end
        Both household members can send messages.
        Identity resolves to usr_XXXX not person_a.
        Admin approval flow still works.
        703 tests still pass.

### Phase B — OAuth
Start only after Phase A is verified.

    B1. Create mithra/integrations/oauth_provider.py
        Abstract base class. No implementation.

    B2. Create mithra/integrations/oauth_token_store.py
        Encrypted storage using StorageAdapter.
        Domain: "oauth"

    B3. Create mithra/integrations/oauth_manager.py
        Orchestration with auto-refresh logic.

    B4. Create mithra/integrations/google_oauth.py
        Full GoogleOAuth implementation.
        Uses authlib + google-auth-oauthlib.

    B5. Create mithra/integrations/microsoft_oauth.py
        Stub only. NotImplementedError on all methods.
        Document what needs implementing.

    B6. Add OAuth routes to gateway.py
        GET /auth/{provider}
        GET /auth/{provider}/callback
        Generic — works for any registered provider.

    B7. Wire OAuthManager into bootstrap.py
        _build_oauth_manager() helper.
        Auto-registers providers based on env vars.

    B8. Update CalendarAdapter
        Use OAuthManager.get_token("google", user_id).
        Remove all Nango references.

    B9. Remove Nango entirely
        compose.yaml: remove nango, nango-db services
        Remove nango volumes
        Delete mithra/integrations/nango_client.py
        Remove _setup_nango() from bootstrap.py
        Remove NANGO_* from .env.example

    B10. Update Google Cloud Console
         Authorized redirect URIs:
         https://assistant.mithra-app.com/auth/google/callback
         Remove all other URIs.

### Phase C — End to End Verification

    C1. Send "connect google" to Telegram bot
        Receive link: /auth/google?user_id=usr_XXXX
        Open link -> Google consent -> approve

    C2. Verify token stored
        docker exec mithra-api-gateway-1 python -c "
        import sqlite3
        conn = sqlite3.connect('/data/mithra/mithra.db')
        rows = conn.execute(
            'SELECT domain, key, user_id FROM records
             WHERE domain=?', ('oauth',)).fetchall()
        print(rows)
        conn.close()
        "
        Expected: [('oauth', 'google:usr_XXXX', 'usr_XXXX')]

    C3. Test calendar write
        Send: "add dentist tuesday 3pm"
        Confirm: "yes"
        Verify: event appears in Google Calendar

    C4. Test both members
        Wife connects Google independently.
        Both tokens stored separately.
        Each user's calendar written correctly.

    C5. Test token auto-refresh
        Force token expiry in SQLite.
        Send calendar request.
        Verify: token refreshed automatically.
        Verify: calendar write succeeds.

---

## 6. Alternatives Considered

### Nango Self-Hosted (Free) v0.70.4
Decision: Removed
Reason:
    - Free tier lacks webhooks (permanent, by design)
    - Dashboard requires email provider for login
    - Write APIs not available in free version
    - Connect UI requires public subdomain exposure
    - 3+ days of debugging yielded no working OAuth flow
Revisit when: Product has >100 households AND needs
              >10 OAuth providers simultaneously.

### Nango Cloud
Decision: Rejected
Reason: Tokens leave user infrastructure.
        Violates privacy-first self-hosted principle.
        No payment acceptable.

### Composio, Merge, Ampersand, Pipedream
Decision: Rejected
Reason: All managed cloud. No self-hosted option.
        Tokens leave user infrastructure.

### Arcade
Decision: Rejected
Reason: Open source and self-hostable but only 21 APIs.
        Google Calendar not in supported set.

### Auth0 / Keycloak / Zitadel
Decision: Not applicable
Reason: These are USER authentication systems.
        Mithra needs INTEGRATION auth (acting on behalf
        of users with Google/Microsoft).
        Different problem entirely.

---

## 7. Design Principles

These apply to ALL future decisions.

1. PRIVACY FIRST
   User data never leaves user infrastructure.
   Health domain: local only, never cloud. Ever.
   No paid managed services that store user tokens.

2. ABSTRACTION BOUNDARIES
   Domain code never knows about storage backend.
   Gateway never knows about action types.
   Adapters are swappable without domain changes.
   Each layer has one job.

3. PRODUCT READY FROM DAY ONE
   household_id scopes everything.
   user_id is globally unique (hh_ and usr_ prefixes).
   Schema designed for multi-tenant from start.
   No backfilling migrations later.

4. NO TACTICAL HOTFIXES
   If a fix touches more than one layer, stop.
   Redesign before implementing.
   Update this document before touching code.
   A week of correct design saves a month of hotfixes.

5. TEST BEFORE SHIP
   All components have unit tests before merging.
   No code ships without passing full test suite.
   Current baseline: 703 tests passing.

6. DOCUMENT THE WHY
   Every ADR records why, not just what.
   Alternatives considered must be listed.
   Future developers (including AI) must understand
   why a decision was made before changing it.
