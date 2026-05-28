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
| Column       | Type | Notes                           |
|--------------|------|---------------------------------|
| id           | TEXT | PRIMARY KEY "usr_9f2ec4d1"      |
| household_id | TEXT | FK -> households.id             |
| display_name | TEXT | "Nikitha"                       |
| role         | TEXT | "owner"/"admin"/"member"        |
|              |      | owner: created household,       |
|              |      |   cannot be removed             |
|              |      | admin: approves members,        |
|              |      |   manages integrations          |
|              |      | member: regular access          |
| status       | TEXT | "active"/"invited"/"suspended"  |
|              |      | invited: approval pending       |
|              |      | suspended: blocked, kept for    |
|              |      |   audit                         |
| invited_by   | TEXT | nullable, FK -> members.id      |
|              |      | who approved this member        |
|              |      | null for first member (owner)   |
| created_at   | TEXT | ISO8601                         |
| updated_at   | TEXT | ISO8601                         |

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

### Config Migrated to household_config Table
The following household.yaml fields are removed
and will be stored in household_config table
when the relevant domains are implemented:

  voice.wake_word              → when Wyoming voice is built
  voice.stt_model              → when Wyoming voice is built
  voice.tts_voice              → when Wyoming voice is built
  calendar.default_calendar_id → when Time domain is built
  shared_devices               → when Hardware domain is built
  registration.require_admin_approval → now in MemberRegistry
  registration.max_members            → now in MemberRegistry

Current household_config seeds on first run:
  timezone: America/Chicago
  locale: en-US

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
Config-driven, provider-agnostic OAuth using Authlib.
No provider subclasses. No provider-specific files.
Adding a new provider = config entry only, zero code.

### Why Config-Driven Not Subclasses
OAuth 2.0 is a standard. Authlib handles the full flow
generically — auth URL, code exchange, token refresh.
Provider differences are just config values (URLs, scopes,
auth params). A subclass per provider adds complexity
with no benefit.

### Provider Config Structure
Stored in mithra/integrations/oauth_providers.py as a dict.
Loaded at startup. Filtered by available env vars.

    OAUTH_PROVIDERS = {
        "google": {
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": [
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/contacts.readonly",
                "openid",
                "email",
                "profile",
            ],
            "auth_params": {
                "access_type": "offline",
                "prompt": "consent",
            },
            "client_id_env":     "GOOGLE_CLIENT_ID",
            "client_secret_env": "GOOGLE_CLIENT_SECRET",
        },
        "microsoft": {
            "auth_url": "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize",
            "token_url": "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            "scopes": [
                "https://graph.microsoft.com/Files.ReadWrite",
                "https://graph.microsoft.com/Calendars.ReadWrite",
                "https://graph.microsoft.com/Contacts.ReadWrite",
                "offline_access",
            ],
            "auth_params": {},
            "client_id_env":     "ONEDRIVE_CLIENT_ID",
            "client_secret_env": "ONEDRIVE_CLIENT_SECRET",
            "tenant_id_env":     "ONEDRIVE_TENANT_ID",
            "tenant_id_default": "common",
        },
    }

### Adding a New Provider
1. Add entry to OAUTH_PROVIDERS dict
2. Add CLIENT_ID + CLIENT_SECRET to .env
Done. Zero code changes anywhere else.

### Components

#### oauth_providers.py
Provider config dict only. No logic.
Auto-resolves URLs with tenant_id substitution for Microsoft.

#### OAuthTokenStore — oauth_token_store.py
Encrypted storage using StorageAdapter.
Storage schema:
    domain:  "oauth"
    key:     "{provider_id}:{user_id}"
    layer:   "credentials"

Token data fields:
    provider           str
    user_id            str
    household_id       str
    access_token       str  ENCRYPTED
    refresh_token      str  ENCRYPTED
    token_type         str  default "Bearer"
    expires_at         int  Unix timestamp
    scopes             list
    connected_at       str  ISO8601
    last_refreshed     str  ISO8601
    provider_user_id   str  user's ID at provider

Methods:
    save(provider_id, user_id, token_data) -> None
    load(provider_id, user_id) -> dict | None
    delete(provider_id, user_id) -> None
    is_connected(provider_id, user_id) -> bool

#### OAuthManager — oauth_manager.py
Single generic implementation using Authlib.
No provider-specific code inside.
Token refresh uses client.ensure_active_token()
which handles all standard OAuth2 providers automatically.

Methods:
    get_auth_url(provider_id, user_id, redirect_uri) -> str
    handle_callback(provider_id, code, user_id, redirect_uri) -> bool
    get_token(provider_id, user_id) -> str | None
    is_connected(provider_id, user_id) -> bool
    disconnect(provider_id, user_id) -> None
    available_providers() -> list[str]

### OAuth Routes (generic)
    GET /auth/{provider}           redirect to provider
    GET /auth/{provider}/callback  exchange code, store token

Returns 404 if provider not configured.
Works for any entry in OAUTH_PROVIDERS.

### Nango Decision
Remove Nango entirely from the stack:
  - Remove nango service from compose.yaml
  - Remove nango-db service from compose.yaml
  - Remove nango volumes
  - Remove NANGO_* env vars
  - Delete mithra/integrations/nango_client.py
  - Remove _setup_nango() from bootstrap.py
  - Remove NANGO_* from .env.example

### Product Migration Path
Personal: tokens in encrypted SQLite
Product:  swap OAuthTokenStore backend to cloud storage.
          OAuthManager interface unchanged.
          One-day migration when needed.

---

## 3. File Storage and Document RAG

### Decision
Two-tier storage model:
  SQLite + StorageAdapter → structured records (existing)
  FileStorageAdapter      → unstructured files (new)

ChromaDB (already running) extended to index document
content for semantic search. Tiered retrieval strategy
to minimize cost and respect privacy constraints.

### Why Two Separate Storage Systems
Structured records (calendar events, preferences, tokens)
have known schemas and are queried by exact keys.
Unstructured files (PDFs, receipts, documents) require
content extraction, chunking, and semantic search.
Mixing them in one system produces a poor fit for both.

### File Storage Architecture

#### FileStorageAdapter (abstract interface)
One-way door: all file operations go through this interface.
Swapping backends requires zero domain code changes.

    async def save_file(
        path: str,          "receipts/2026/05/costco.pdf"
        content: bytes,
        metadata: dict,     name, mime_type, domain, user_id
    ) -> str               returns file_id

    async def get_file(
        file_id: str,
    ) -> tuple[bytes, dict] content + metadata

    async def delete_file(file_id: str) -> None

    async def list_files(
        prefix: str = "",   "receipts/2026/"
        user_id: str = "",
    ) -> list[dict]         metadata list only, not content

#### Implementations
    LocalFileAdapter    /data/mithra/files/
                        Always works, no cloud needed
                        Primary for health domain (never cloud)

    OneDriveFileAdapter /Mithra/{household_id}/
                        Via MicrosoftGraphClient
                        Requires "connect microsoft"

#### FileStorageRouter
Same pattern as StorageRouter:
  health domain → LocalFileAdapter ONLY (never cloud)
  all other domains → OneDriveFileAdapter primary,
                      LocalFileAdapter fallback

#### OneDrive Folder Structure
    OneDrive/
      Mithra/
        {household_id}/
          receipts/
            {year}/{month}/
          documents/
            insurance/
            warranty/
            medical/
          exports/
            calendar_backup.ics
            contacts_backup.vcf
          [NO health/ folder — health stays local always]

### Files Metadata Table (SQLite)
Indexed locally for fast search without cloud round-trips.

    CREATE TABLE files (
        id           TEXT PRIMARY KEY,   "file_abc123"
        household_id TEXT NOT NULL,
        user_id      TEXT NOT NULL,
        domain       TEXT NOT NULL,      "money"|"documents"
        name         TEXT NOT NULL,      "costco_receipt.pdf"
        path         TEXT NOT NULL,      storage path
        storage      TEXT NOT NULL,      "local"|"onedrive"
        size_bytes   INTEGER,
        mime_type    TEXT,
        uploaded_at  TEXT NOT NULL,      ISO8601
        metadata     TEXT               JSON, domain-specific
    );

    CREATE INDEX idx_files_household ON files(household_id);
    CREATE INDEX idx_files_domain ON files(domain, household_id);
    CREATE INDEX idx_files_user ON files(user_id, household_id);

### Document RAG Architecture

#### Why Tiered Not Uniform
Not every query needs RAG. SQL is faster and free.
Health content must never be embedded via cloud APIs.
Embedding everything is expensive and unnecessary.

#### Three-Tier Retrieval
    Tier 1 — SQLite metadata search (always first)
      Fast, free, no LLM involved
      Queries: files table + FTS5 full-text on metadata
      Handles: "find files named insurance"
               "receipts from May 2026"
               "documents in health folder"

    Tier 2 — ChromaDB semantic search (when Tier 1 insufficient)
      Semantic understanding of content
      Handles: "what does my insurance cover for dental"
               "find receipts mentioning prescription"
      Cost: embedding API calls (minimize usage)

    Tier 3 — Full document load (targeted, expensive)
      Load specific file, extract content, pass to LLM
      Handles: "summarize this specific document"
               "what are the terms of my lease"
      Cost: LLM context tokens (use sparingly)

#### DocumentIndexer
Runs when a file is saved. Extracts text, chunks,
embeds, stores in ChromaDB.

    index(file_id, content, metadata) -> None
      1. Extract text (PDF, DOCX, image OCR)
      2. Chunk into segments (~500 tokens each)
      3. Embed via LLM router (local model preferred)
      4. Store chunks in ChromaDB with metadata:
           file_id, chunk_index, domain, user_id,
           household_id, source (local|onedrive)
      5. Store chunk_ids back in files table

    Health domain exception:
      Embed using LOCAL model only (phi3:mini or similar)
      Never send health content to cloud embedding API
      Store in ChromaDB with health=True flag
      Retrieval restricted to same user_id only

#### ChromaDB Collections
    "documents_{household_id}"   general document chunks
    "health_{household_id}"      health content, local only
                                 separate collection for
                                 access control enforcement

#### FileSearchService
Orchestrates tiered retrieval. Called by domain agents.

    async def search(
        query: str,
        domain: str = "",
        user_id: str = "",
        tier: int = 0,          0 = auto (try Tier 1 first)
    ) -> list[SearchResult]

    SearchResult:
        file_id:    str
        file_name:  str
        relevance:  float
        excerpt:    str         relevant text snippet
        tier_used:  int         which tier found it

### MicrosoftGraphClient
Thin wrapper around Microsoft Graph API.
Uses OAuthManager for token management (same as Google).
No separate auth, no separate Docker service.

    _headers(user_id) -> dict   gets token via OAuthManager
    upload_file(user_id, path, content) -> dict
    download_file(user_id, path) -> bytes
    list_folder(user_id, path) -> list[dict]
    delete_file(user_id, path) -> None
    create_folder(user_id, path) -> None

### Privacy Rules (one-way doors, never change)
    1. Health domain files → LocalFileAdapter ONLY
       Never uploaded to OneDrive or any cloud storage
       Embedded using local model only
       Never sent to cloud embedding APIs

    2. File content → never stored in structured records
       Files table contains metadata only (path, name, size)
       Content lives in FileStorageAdapter backends

    3. CloudRAG for health → permanently disabled
       health_collection in ChromaDB is local-only
       Any code path that would send health content
       to a cloud API must raise a hard error

### Implementation Order
    Phase 1 — Foundation (implement now):
      P1-01  Add files table to SQLite (LocalStorageAdapter)
      P1-02  FileStorageAdapter abstract interface
      P1-03  LocalFileAdapter (/data/mithra/files/)
      P1-04  MicrosoftGraphClient (Graph API wrapper)
      P1-05  OneDriveFileAdapter
      P1-06  FileStorageRouter (local + OneDrive)
      P1-07  DocumentIndexer (basic text extraction + embed)
      P1-08  FileSearchService (tiered retrieval)
      P1-09  Wire into bootstrap

    Per domain (when building each domain):
      Money domain    → auto-index receipts on save
      Documents domain → index PDFs on upload
      Health domain   → local embedding only

    Phase 2:
      OCR for receipt images
      Voice note transcription + indexing
      Cross-domain search ("find everything about my car")

### Alternatives Considered

#### External MCP Servers for OneDrive
    elyxlz/microsoft-mcp:
      Rejected: 4 critical security vulnerabilities found
                in independent security audit
    MrFixit96/onedrive-mcp-server:
      Rejected: handles auth separately from OAuthManager,
                adds Docker container, designed for Claude
                Desktop not embedded agent use
    Microsoft Remote MCP (agent365.svc.cloud.microsoft):
      Rejected: remote server, tokens leave infrastructure,
                violates privacy-first principle

    Decision: Build MicrosoftGraphClient ourselves (~150 lines)
              Uses our OAuthManager, no extra containers,
              full control over data model and folder structure

#### Single RAG Layer (index everything in ChromaDB)
    Rejected: expensive, health content would need cloud
              embedding, SQL is faster for structured queries,
              over-engineering for current use case

#### SQLite FTS Only (no ChromaDB for documents)
    Rejected: FTS5 handles keyword search but not semantic
              "find documents about dental coverage" fails
              without embedding-based search

---

## 4. Document Interaction UX

### Design Constraints
Primary interface is Telegram text chat.
No file browser, no visual UI (Fire tablet is Phase 2).
All document interactions must work via text + buttons.
Telegram file size limit: 20MB per file.

### File Ingestion Triggers

#### Trigger 1 — User sends file via Telegram
Handles files under 20MB directly.
For larger files, user sends OneDrive/Google Drive link.

    User sends PDF/photo/doc to @MithraHouseBot
      → Mithra detects file type
      → Asks domain classification if unclear:
          "What is this?
           [Receipt] [Insurance] [Warranty] [Medical] [Other]"
      → User taps category
      → Mithra stores + indexes automatically
      → Confirms: "✅ Saved [filename] to [domain]"

    User sends large file link (OneDrive/Google Drive):
      → Mithra downloads via API
      → Same classification + storage flow
      → No 20MB limit

#### Trigger 2 — Proactive capture from connected services
Gmail/Outlook integration reads incoming emails.
Extracts receipts, booking confirmations, appointments,
flight itineraries, package tracking automatically.

    Email arrives with receipt/booking/flight:
      → Mithra detects type via LLM classification
      → Auto-stores based on domain autonomy level
      → Notifies user:
          "📧 Captured: Costco receipt $234 from email"
          "✈️ Found flight confirmation: Austin→NYC May 30"
          "📦 Package from Amazon: delivery June 2"

    Autonomy levels for proactive capture:
      money.receipts      level 3 (auto-store, notify)
      time.appointments   level 3 (auto-store, notify)
      time.flights        level 3 (auto-store, notify)
      health.records      level 1 (always ask first)
      documents.legal     level 1 (always ask first)

#### Trigger 3 — Initial seed from cloud storage
One-time bulk import from existing OneDrive or
Google Drive folder. Mithra crawls iteratively.

    User: "Import my documents from OneDrive"
    OR:   "Seed from https://onedrive.live.com/..."
    OR:   "Import my Google Drive Documents folder"

    Mithra crawls folder iteratively:
      1. List all files in folder (paginated)
      2. For each file:
         a. Check if already indexed (by name + size)
         b. If new: classify domain automatically
         c. Download and index content
         d. Store metadata in files table
      3. Progress updates every 10 files:
         "Indexed 10/47 files... (receipts: 8, docs: 2)"
      4. Summary when complete:
         "✅ Import complete: 47 files indexed
          Receipts: 23 | Documents: 15 | Other: 9
          [View by category]"

    Iterative approach (not batch):
      Process one file at a time
      Can be interrupted and resumed
      Progress tracked in household_config:
        key: "seed_progress_{folder_id}"
        value: {last_processed, total, status}
      User can continue later: "continue import"

### Document Access Patterns

#### Pattern 1 — Summary (default)
LLM reads relevant content, answers in natural language.
User never sees raw file unless requested.

    User: "What's my dental coverage limit?"
    Mithra: "Your dental coverage is $1,500/year
             for in-network providers (State Farm,
             policy section 4.2, effective Jan 2026)"

#### Pattern 2 — Download link (on request)
Temporary signed URL from OneDrive.
Valid 1 hour. Generated on demand only.

    User: "Send me the actual insurance PDF"
    Mithra: "Here's your insurance document (expires in 1hr):
             [View/Download]
             https://onedrive.live.com/..."

#### Pattern 3 — List and browse
SQL query against files table metadata.
No content loaded unless user drills in.

    User: "Show my receipts from last month"
    Mithra: "April 2026 receipts (4 items, total $479):
             • Costco $234 — Apr 3
             • HEB $156 — Apr 12
             • Amazon $89 — Apr 18
             Total: $479
             [Ask about any] [Export summary]"

### Confirmation Behavior

Based on domain autonomy level. Configured per domain.
User preference remembered after first interaction.

    Level 1 (SURFACE — always confirm):
      health.*, documents.legal, documents.financial
      "Found medical receipt from CVS. Store it? [Yes] [No]"

    Level 2 (ACT_TELL — store then notify):
      documents.general, documents.warranty
      Stores immediately, notifies:
      "📄 Stored warranty for Samsung TV"

    Level 3 (ACT_SILENT — store silently):
      money.receipts, time.appointments, time.flights
      Stores without notification unless explicitly asked
      User can review: "what did you capture today?"

### File Size Handling

    Under 20MB → accept directly via Telegram
    Over 20MB  → user sends cloud link
                 (OneDrive share link or Google Drive link)

    Mithra detects link type and downloads:
      OneDrive share link → MicrosoftGraphClient
      Google Drive link   → GoogleDriveClient (future)
      Direct URL          → httpx download

### Telegram UX Patterns

#### File type detection
When file received:
  PDF → extract text, classify domain
  Image (photo) → OCR if receipt-like, otherwise prompt
  DOCX/XLSX → extract text, classify domain
  Unknown → ask user: "What type of document is this?"

#### Inline buttons for classification
Max 4 buttons per row (Telegram limit):
  [Receipt] [Insurance] [Warranty] [Other]

#### Progress for long operations
Seed import or large PDF processing:
  Send initial message with job ID
  Edit message every 10 items with progress
  Final edit when complete

#### Error handling
File too large:
  "This file is 45MB — too large for Telegram.
   Share it via OneDrive link instead."
Unsupported type:
  "I can't read .zip files yet.
   Supported: PDF, Word, Excel, images."
Cloud storage not connected:
  "Connect OneDrive first to store files there.
   [Connect OneDrive]"
  Falls back to local storage if user declines.

### Initial Seed Flow (detailed)

This is the most important interaction for new users
who already have documents in cloud storage.

    Step 1 — User initiates
      "Import my OneDrive documents"
      OR user sends a share link
      OR Mithra proactively asks on first connection:
        "You connected OneDrive. Want me to index
         your existing documents? This helps me
         answer questions about them.
         [Yes, import] [No thanks] [Ask me later]"

    Step 2 — Scope selection
      "Which folder should I import?
       [My Documents] [Desktop] [All of OneDrive] [Choose folder]"

    Step 3 — Preview before import
      Mithra lists top-level contents:
      "Found in My Documents:
       • 23 PDFs
       • 12 Word documents
       • 8 Excel files
       • 4 images
       Total: 47 files
       [Import all] [Choose types] [Cancel]"

    Step 4 — Iterative processing
      Progress updates every 10 files
      Runs in background (user can do other things)
      Resumes automatically if interrupted

    Step 5 — Completion summary
      "✅ Import complete!
       Indexed 47 files:
       • Receipts: 23 (Total value: ~$4,200)
       • Insurance docs: 3
       • Warranties: 8
       • Other: 13
       Ask me anything about your documents."

### Privacy Rules for Document UX

    Health documents:
      Never uploaded to OneDrive from Mithra
      Never summarized via cloud LLM
      Always processed locally
      User explicitly warned: "This is a health document.
      It will be stored locally only and never sent to cloud."

    Financial documents:
      DataSanitiser strips account numbers before LLM calls
      Original file stored intact, sanitised version used for RAG

    Shared documents:
      Documents tagged with user_id of uploader
      Other household members can access shared category
      Private category: only uploader can access
      Default: shared (household-level)

### Product Considerations

    Per-user storage quotas (product):
      household_config: "storage_limit_gb" = 5
      FileStorageRouter enforces quota before save

    Storage billing (product):
      Local storage: free (user's hardware)
      OneDrive: user's own Microsoft account (no Mithra cost)
      No Mithra-hosted storage → no storage costs ever

---

## 5. Document Processing Libraries

### Decision
Use Kreuzberg for all document text extraction and chunking.
No separate Docker service needed. No ML/PyTorch dependency.

### Why Kreuzberg
- Rust core: 10-100x faster than Python alternatives
- 97+ formats: PDF, DOCX, XLSX, XLS, CSV, PPTX, images + OCR
- Async-first: matches Mithra's async codebase
- Docker-ready by design: minimal system dependencies
- ~71MB pip install vs 4-9GB for Docling (PyTorch)
- All processing local: no API calls, no cloud
- Active releases: multiple per month throughout 2025-2026
- Featured in Tryolabs Top Python Libraries of 2025

### Format Coverage for Mithra

    PDF            Native Rust extractor (fast, no ML)
                   Scanned PDFs via force_ocr=True
    DOCX           Native Rust parser
    XLSX/XLS/CSV   Native Rust spreadsheet extractors
                   Bank statements, expense files
    PPTX           Presentations
    Images         JPEG, PNG, TIFF via Tesseract OCR
    HTML/MD        Web content, emails

### Usage Pattern

    from kreuzberg import extract_bytes, ExtractionConfig

    # Standard extraction (native PDFs, DOCX, XLSX)
    result = await extract_bytes(content, mime_type=mime)
    text = result.content

    # Force OCR for scanned images
    config = ExtractionConfig(force_ocr=True)
    result = await extract_bytes(content, mime_type=mime,
                                  config=config)

### System Dependencies
    Tesseract OCR required for image/scanned PDF support.
    Add to Dockerfile:
      RUN apt-get install -y tesseract-ocr

    Without tesseract: native text PDFs, DOCX, XLSX, CSV
    still work. OCR gracefully degrades.

### Risks and Mitigations
    Risk: Single maintainer (Na'aman Hirschfeld / Goldziher)
    Mitigation: DocumentIndexer abstraction means swapping
    Kreuzberg = one file change, zero domain code changes.
    Acceptable for personal household use.

    Risk: v4 is a Rust rewrite (newer, less battle-tested)
    Mitigation: Fallback path in _extract_chunks() catches
    all exceptions and returns empty list — file is still
    saved and searchable via SQLite metadata (Tier 1).

    Risk: Breaking API changes between versions
    Mitigation: Pin version in requirements.txt

### Alternatives Considered

#### Docling (IBM Research / Linux Foundation AI)
    Rejected: PyTorch dependency adds 4-9GB to Docker image.
    Unacceptable for Windows laptop with Watchtower
    auto-updates pulling on every commit.
    Better suited: GPU-based, high-volume enterprise workloads.
    Revisit: if Kreuzberg proves insufficient for complex
    scanned documents or enterprise-grade OCR is needed.

#### PyMuPDF4LLM + python-docx + openpyxl (per-format)
    Pros: each library battle-tested 8-12+ years
          no single-maintainer risk
          ~65MB total
    Cons: no unified API, 4 separate imports,
          no built-in OCR, async wrappers needed
    Decision: Deferred — migrate to this if Kreuzberg
    maintainer becomes inactive or API stability issues emerge.
    The DocumentIndexer abstraction makes this a one-day swap.

#### Apache Tika
    Rejected: requires Java server as sidecar container.
    Adds Java runtime (~200MB) and separate service.

#### Unstructured.io
    Rejected: heavy like Docling, ML dependencies,
    unsuitable for low-resource environments.

### Migration Path
If Kreuzberg needs to be replaced:
  1. Update DocumentIndexer._extract_chunks() only
  2. Replace kreuzberg imports with per-format libraries:
       PDF:  pymupdf4llm
       DOCX: python-docx
       XLSX: openpyxl
       OCR:  pytesseract
  3. Zero changes to FileStorageRouter, FileSearchService,
     domain code, or bootstrap
  DocumentIndexer abstraction exists specifically for this.

---

## 6. Phase 1 Storage Infrastructure

### Decision Summary
Two separate storage systems, each doing what it does best:

  StorageAdapter (SQLite)     → structured records
                                 tokens, sessions, domain facts
                                 always local, always fast

  FileStorageAdapter (new)    → unstructured files
                                 PDFs, images, receipts, docs
                                 local primary, OneDrive cloud

  SQLite weekly backup        → disaster recovery only
                                 compressed dump to OneDrive
                                 not sync, not live mirror

### Why Not OneDrive for Structured Records
OneDrive via Graph API has latency (100-500ms per call).
SQLite reads are <1ms. For a household assistant that
assembles context on every message, SQLite must stay
primary. OneDrive adds no value for structured records
when SQLite is already backed up weekly.

### 6a. FileStorageAdapter Interface

Abstract base. All file operations go through this.
Swap backends without changing domain code.

    class FileStorageAdapter(ABC):

        async def save_file(
            self,
            content: bytes,
            filename: str,
            domain: str,
            user_id: str,
            metadata: dict = {},
        ) -> str:
            """Store file. Returns file_id."""

        async def get_file(
            self,
            file_id: str,
        ) -> tuple[bytes, dict]:
            """Returns (content, metadata)."""

        async def delete_file(
            self,
            file_id: str,
        ) -> None: ...

        async def list_files(
            self,
            domain: str = "",
            user_id: str = "",
            prefix: str = "",
        ) -> list[dict]:
            """Returns metadata list only, not content."""

        async def file_exists(
            self,
            file_id: str,
        ) -> bool: ...

        def health(self) -> dict: ...

### 6b. LocalFileAdapter

Stores files at /data/mithra/files/{domain}/{user_id}/
File IDs are UUIDs. Metadata stored in SQLite files table.
Always works. No network calls. Used for health domain always.

    Path structure:
      /data/mithra/files/
        money/
          usr_ba6d9199/
            file_abc123.pdf
            file_def456.jpg
        documents/
          usr_ba6d9199/
            file_ghi789.pdf
        health/             ← local only, never OneDrive
          usr_ba6d9199/
            file_jkl012.pdf

### 6c. OneDriveFileAdapter

Stores files in user's own OneDrive via Microsoft Graph API.
Uses OAuthManager.get_token("microsoft", user_id) for auth.
No Mithra-hosted storage — files live in user's account.

    OneDrive path structure:
      /Mithra/
        hh_b76fef08/        ← household_id
          money/
            receipts/
              2026/05/
          documents/
            insurance/
            warranty/
          [NO health/ — enforced by FileStorageRouter]

    Requires: ONEDRIVE_CLIENT_ID + ONEDRIVE_CLIENT_SECRET
    Set up: user sends "connect microsoft" to Telegram
    Falls back to LocalFileAdapter if not connected

### 6d. FileStorageRouter

Same pattern as StorageRouter.
Routes by domain. Health domain never goes to cloud.

    Routing rules:
      health   → LocalFileAdapter ONLY (hard rule, never changes)
      all else → OneDriveFileAdapter primary
                 LocalFileAdapter fallback if not connected

    Bootstrap behavior:
      Always build LocalFileAdapter (no credentials needed)
      Build OneDriveFileAdapter only if Microsoft connected
      FileStorageRouter decides at runtime per request

### 6e. files Table (SQLite)

Metadata index for fast search without cloud round-trips.
Raw content lives in FileStorageAdapter backends.

    CREATE TABLE files (
        id           TEXT PRIMARY KEY,
        household_id TEXT NOT NULL,
        user_id      TEXT NOT NULL,
        domain       TEXT NOT NULL,
        filename     TEXT NOT NULL,
        storage      TEXT NOT NULL,
        path         TEXT NOT NULL,
        size_bytes   INTEGER,
        mime_type    TEXT,
        uploaded_at  TEXT NOT NULL,
        metadata     TEXT,
        chunk_ids    TEXT,
        INDEX idx_files_household (household_id),
        INDEX idx_files_domain (domain, household_id),
        INDEX idx_files_user (user_id, household_id)
    );

    metadata column: JSON, domain-specific tags
      money:     {"merchant": "Costco", "amount": 234.50,
                  "date": "2026-05-03"}
      documents: {"category": "insurance",
                  "expiry": "2027-01-01"}
      health:    {"type": "receipt", "provider": "CVS"}

    chunk_ids column: JSON array of ChromaDB chunk IDs
      Set after DocumentIndexer processes the file
      Used to retrieve relevant chunks for RAG

### 6f. DocumentIndexer

Processes files after storage. Extracts text,
chunks, embeds, stores in ChromaDB.
Uses Docling for extraction (MIT license, Python library,
handles PDF/DOCX/PPTX/images with OCR).

    Pipeline:
      1. Receive file_id after save_file()
      2. Load content from FileStorageAdapter
      3. Docling extracts structured text + tables
         fast_mode=True by default
         full_mode=True for scanned images (OCR)
      4. Chunk into ~500 token segments
         preserve document hierarchy (headings, sections)
      5. Embed chunks:
         health domain → local model ONLY (phi3:mini)
         all others    → LLM router (cloud ok)
      6. Store chunks in ChromaDB
         collection: "documents_{household_id}"
         health:     "health_{household_id}" (separate)
      7. Update files.chunk_ids in SQLite

    Runs async after file save — non-blocking.
    User gets confirmation immediately, indexing happens
    in background.

### 6g. FileSearchService

Tiered retrieval. Called by domain agents.

    Tier 1 — SQLite metadata (always first, free)
      Query files table by domain, user_id, metadata
      Full-text search on filename + metadata JSON
      Returns: list of file metadata dicts

    Tier 2 — ChromaDB semantic search (when Tier 1 insufficient)
      Embed query, search relevant collection
      Returns: chunk text + file_id references
      Cost: embedding API call

    Tier 3 — Full file load (targeted, expensive)
      Load specific file, extract full text
      Pass to LLM for summarization/QA
      Cost: LLM context tokens

    Interface:
      async def search(
          query: str,
          domain: str = "",
          user_id: str = "",
          tier: int = 0,    # 0=auto, 1/2/3=force tier
      ) -> list[SearchResult]

      SearchResult:
          file_id: str
          filename: str
          relevance: float
          excerpt: str
          tier_used: int
          metadata: dict

### 6h. MicrosoftGraphClient

Thin wrapper around Microsoft Graph API.
Uses OAuthManager for token — no separate auth.

    BASE_URL = "https://graph.microsoft.com/v1.0"

    async def _headers(user_id) -> dict
        # calls OAuthManager.get_token("microsoft", user_id)

    # OneDrive operations
    async def upload_file(user_id, path, content) -> dict
    async def download_file(user_id, path) -> bytes
    async def list_folder(user_id, path) -> list[dict]
    async def delete_file(user_id, path) -> None
    async def create_folder(user_id, path) -> None
    async def get_share_link(user_id, path,
                             expires_hours=1) -> str

    # Future (not now)
    # calendar, contacts, mail — add when domains need them

### 6i. SQLite Weekly Backup

Disaster recovery. Not sync. SQLite is source of truth.

    Schedule: Sunday 2am (cron in healthmonitor container)

    Process:
      1. VACUUM mithra.db (compact)
      2. gzip compress → mithra_backup_YYYY-MM-DD.db.gz
      3. Upload to OneDrive:
         /Mithra/{household_id}/backups/
      4. Keep last 4 backups (delete older)
      5. Send Telegram confirmation to admin:
         "✅ Weekly backup complete: mithra_backup_2026-05-24.db.gz
          Size: 2.3MB | OneDrive: /Mithra/.../backups/"

    Requires: Microsoft connected (OneDrive token)
    If not connected: backup to local /data/mithra/backups/
                      notify admin to connect Microsoft

    Restore procedure:
      1. Download backup from OneDrive
      2. docker compose stop api-gateway
      3. gunzip backup → /data/mithra/mithra.db
      4. docker compose start api-gateway
      Done.

### 6j. Implementation Order (Phase 1)

    P1-01  Add files table to LocalStorageAdapter.__init__
    P1-02  FileStorageAdapter abstract base
           mithra/storage/file_adapter.py
    P1-03  LocalFileAdapter
           mithra/storage/local_file.py
    P1-04  MicrosoftGraphClient
           mithra/integrations/microsoft_graph.py
    P1-05  OneDriveFileAdapter
           mithra/storage/onedrive_file.py
    P1-06  FileStorageRouter
           mithra/storage/file_router.py
    P1-07  DocumentIndexer
           mithra/storage/document_indexer.py
    P1-08  FileSearchService
           mithra/storage/file_search.py
    P1-09  Wire into bootstrap
           _build_file_storage() in bootstrap.py
    P1-10  SQLite backup job
           Add to scripts/healthcheck.py or separate
           scripts/backup.py

### 6k. Alternatives Considered

#### OneDrive as structured storage (original design)
  Rejected for Phase 1:
    Graph API latency (100-500ms) vs SQLite (<1ms)
    Complex sync logic needed
    SQLite + weekly backup achieves resilience simply
  Revisit: if household grows beyond single laptop,
           or if multi-device real-time sync is needed

#### Dedicated backup service (Velero, Restic)
  Rejected: heavyweight for single SQLite file
            weekly gzip + OneDrive upload is 20 lines
            no new Docker service needed

---

## 7. Storage Architecture

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

## 8. Dashboard and Visual Interface

### Decision
Home Assistant (Lovelace) is Mithra's visual display layer.
Mithra is the brain and single source of truth.
HA reads from Mithra API — not from Google/Microsoft directly.
Fire tablet runs HA in kiosk mode as always-on household display.

### Why Option B (Mithra API → HA) Over Option A (HA direct)
Option A: HA connects directly to Google Calendar, Microsoft etc.
  Problem: data is fragmented across providers
           HA knows about calendar but not Mithra's people notes,
           budget summaries, document alerts, or household context
           Two sources of truth — diverges over time

Option B: Mithra exposes dashboard API → HA reads it
  Mithra = single source of truth for all household data
  HA = display only, no business logic
  Richer: shows Mithra-specific data across all 15 domains
  Consistent: same data in Telegram and on dashboard
  Extensible: new domain = new dashboard card, no HA config change

### Architecture

    Mithra API (FastAPI)
      GET /api/v1/dashboard/today
      GET /api/v1/dashboard/people
      GET /api/v1/dashboard/money
      GET /api/v1/dashboard/home
      GET /api/v1/dashboard/{domain}
           ↓
    Home Assistant (Lovelace)
      Custom cards call Mithra API
      Refresh every 5 minutes
      household_id scoped per installation
           ↓
    Fire tablet (kiosk mode)
      Always-on wall display
      Shows today view by default
      Touch to navigate domains

### Dashboard API Design

Each domain exposes a dashboard-ready summary endpoint.
Standardized response format across all domains:

    GET /api/v1/dashboard/{domain}?user_id=usr_ba6d9199

    Response:
    {
        "domain": "time",
        "updated_at": "2026-05-27T10:00:00Z",
        "summary": "2 events today",
        "items": [
            {
                "id": "...",
                "title": "Dentist appointment",
                "subtitle": "3:00 PM",
                "icon": "calendar",
                "priority": "high",
                "action": "calendar.view"
            }
        ],
        "alerts": [],
        "quick_actions": [
            {"label": "Add event", "action": "calendar.create"}
        ]
    }

### Today View (primary dashboard screen)

Aggregates across all domains into one daily summary:

    GET /api/v1/dashboard/today

    Sections:
      📅 Today's calendar (Time domain)
      🎂 Upcoming birthdays (People domain)
      📦 Package deliveries (Shopping domain)
      💰 Recent spending (Money domain)
      🏠 Home status (Home domain — temp, locks)
      ✅ Pending Mithra confirmations (Autonomy engine)
      🌤 Weather summary

### HA Lovelace Integration

    Custom REST sensor in HA:
      sensor:
        - platform: rest
          name: mithra_today
          resource: https://assistant.mithra-app.com/api/v1/dashboard/today
          headers:
            Authorization: Bearer ${HA_MITHRA_TOKEN}
          scan_interval: 300
          value_template: "{{ value_json.summary }}"
          json_attributes:
            - items
            - alerts
            - quick_actions

    HA token: new token type "dashboard" with read-only
    access to dashboard endpoints only.
    Stored in household_config: "ha_dashboard_token"

### Fire Tablet Setup

    Hardware: Amazon Fire HD 8 or Fire HD 10
    Software: HA Companion app in kiosk mode
    Display: always-on, auto-brightness
    Default screen: Today view
    Touch navigation: swipe between domain views
    Voice: tap microphone → Mithra via HA Assist

### Implementation Order (when ready)

    D1. Add dashboard router to gateway.py
        GET /api/v1/dashboard/{domain}
        GET /api/v1/dashboard/today

    D2. Time domain dashboard endpoint
        (implement when Time domain is built)

    D3. People domain dashboard endpoint
        (implement when People domain is built)

    D4. HA setup (can start once Time + People exist)
        Install HA in Docker
        Configure Lovelace with REST sensors
        Fire tablet kiosk mode

    D5. Today view (aggregate endpoint)
        Once 3+ domains exist

    D6. Voice integration
        HA Assist → Wyoming → Mithra
        (requires hardware)

### Product Considerations

    Multi-household (product):
      Each household has its own HA instance
      Or: Mithra hosts a web dashboard (no HA needed)
      HA is personal-use optimized; product may use
      a custom React dashboard instead

    Authentication:
      Dashboard token scoped to household_id
      Read-only by default
      HA never writes to Mithra — Telegram is write channel

---

## 9. Migration Plan (Current -> Target)

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

## 10. Implementation Order

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

    B1. Create mithra/integrations/oauth_providers.py
        Provider config dict only. No logic.

    B2. Create mithra/integrations/oauth_token_store.py
        Encrypted storage using StorageAdapter.
        Domain: "oauth"

    B3. Create mithra/integrations/oauth_manager.py
        Generic Authlib-based orchestration.
        Uses ensure_active_token() for refresh.
        No provider-specific code.

    B4. Add generic OAuth routes to gateway.py
        GET /auth/{provider}
        GET /auth/{provider}/callback

    B5. Wire OAuthManager into bootstrap.py
        Load OAUTH_PROVIDERS, filter by env vars.

    B6. Update CalendarAdapter
        OAuthManager.get_token("google", user_id)

    B7. Remove Nango entirely
        compose.yaml, nango_client.py, bootstrap.py,
        .env.example

    B8. Update Google Cloud Console redirect URI
        https://assistant.mithra-app.com/auth/google/callback

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

## 11. Alternatives Considered

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

### IAM Systems (Keycloak, Zitadel, Logto, SuperTokens)
Decision: Rejected
Reason:
    These solve USER AUTHENTICATION — how users log
    into your app via email/password/SSO/OIDC.
    Mithra's identity comes from Telegram, not login forms.
    Adopting an IAM system adds a full auth stack
    (OIDC server, user store, another database)
    to solve a 4-table member management problem.
    The organization/team management pattern these
    systems include is always app-specific in its
    business rules. Every major company (GitHub, Slack,
    Notion) builds this themselves.
    Our member management is ~280 lines of code.

### Provider Subclasses for OAuth
Decision: Rejected
Reason:
    OAuth 2.0 is a standard. Authlib handles the full
    flow generically. Provider differences are config
    values (URLs, scopes, auth params) not code.
    A subclass per provider adds complexity with no
    benefit. Config-driven approach allows adding any
    standard OAuth2 provider with zero code changes.

---

## 12. Design Principles

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

7. START SIMPLE, EXTEND DELIBERATELY
   Add owner/admin/member roles now (costs nothing).
   Skip custom roles, RBAC, viewer roles — enterprise
   features built when enterprise customers exist.
   Skip groups within household — 2 people, no groups.
   Every correct field added now prevents a migration
   later. Every feature NOT added avoids premature
   complexity. Validate against how industry leaders
   (Slack, Notion, GitHub) model similar problems
   before designing — they start simple too.
