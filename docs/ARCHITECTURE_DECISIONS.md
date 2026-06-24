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

## 9. Domain Plugin Architecture

### Purpose
This section defines how Mithra's 15 domains register
with the LangGraph agent, expose tools, handle cross-domain
side effects, and migrate to industry-standard protocols.
These are the foundational decisions that gate all domain
implementation. Every domain built must follow this spec.

---

### 9.1 The Protocol Landscape

Three open standards govern how agents interact in 2026.
Understanding where each fits determines what we build now
vs what we design for later.

    MCP (Model Context Protocol) — Anthropic → Linux Foundation
      Layer: Agent ↔ Tool (external service)
      Purpose: Standardized interface between agent and
               external tools, databases, APIs
      Status: 97M monthly downloads, universal adoption
              (Anthropic, OpenAI, Google, Microsoft)

    A2A (Agent-to-Agent) — Google → Linux Foundation
      Layer: Agent ↔ Agent (cross-vendor coordination)
      Purpose: Agents from different vendors/frameworks
               discover each other and delegate tasks
      Status: 150+ organizational supporters, v1.0 (2026)

    LangChain + LangGraph — the execution layer
      LangChain: building blocks (@tool, bind_tools,
                 ToolMessage, ChatAnthropic)
      LangGraph: orchestration (StateGraph, nodes, edges,
                 ToolNode, conditional routing)
      These are complementary: LangChain = bricks,
      LangGraph = architecture

### 9.2 Protocol Usage in Mithra

    NOW (personal household, single process):
      LangChain @tool    → internal domain tools
      LangGraph          → agent orchestration
      Domain events      → cross-domain side effects
      Direct API calls   → Google Calendar, OneDrive

      Rationale: all 15 domains run in the same Python
      process. MCP and A2A add network overhead with zero
      benefit for in-process communication.

    PRODUCT (multi-tenant, third-party ecosystem):
      MCP servers        → external tool providers
                           (users bring own calendar service)
      A2A protocol       → third-party specialist domains
                           (medical domain from healthcare co.)
      LangGraph subgraphs → domain specialist agents

    MIGRATION PATH (zero domain code changes):
      Internal domain → MCP server: wrap Domain in MCP
                        adapter, domain logic unchanged
      Domain event    → A2A task:   DomainContext.request_id
                        maps to A2A task_id already

### 9.3 LangChain vs LangGraph — Role in Mithra

    LangChain provides (used in domain tools):
      @tool decorator     define domain tools
      args_schema         typed input via Pydantic
      bind_tools()        attach domain tools to LLM
      ToolMessage         tool result in message chain

    LangGraph provides (used in agent orchestration):
      StateGraph          the agent graph
      nodes               classify_intent, tool_node, respond
      conditional_edges   routing based on LLM output
      ToolNode            executes tool calls
      checkpointer        session state persistence

### 9.4 Two-Stage Tool Selection

Never inject all domain tools at once.
A 60+ tool context window degrades LLM reasoning quality.
Maximum tools per LLM call: 15.

    Stage 1 — Domain classification (existing):
      classify_intent() → domain identified
      e.g. "dentist tuesday" → "time"
      e.g. "call john" → "people"
      Already working in LangGraphAdapter.

    Stage 2 — Domain tool injection (new):
      DomainRegistry.get_tools(domain_id, context)
      Returns only that domain's tools (5-8 max)
      llm.bind_tools(domain_tools) per request
      LLM selects specific tool from small focused set

    Multi-domain requests (rare, ~9% of requests):
      "remind me to pay rent" → time + money
      Inject tools from BOTH domains (still ≤15 total)
      Single LLM chains them in one reasoning step

    Supervisor pattern (future, when needed):
      For 3+ domain requests (< 1% today)
      Build when concrete need exists, not speculatively

### 9.5 Domain Interface Contract

Every domain implements this interface.
This is a one-way door — changing it requires all domains
to be updated simultaneously.

    class Domain(ABC):

        @property
        @abstractmethod
        def domain_id(self) -> str:
            """Unique identifier e.g. 'time', 'people'.
            Immutable. Used as storage domain key.
            Must match StorageAdapter domain conventions."""

        @property
        @abstractmethod
        def version(self) -> str:
            """Semantic version e.g. '1.0.0'.
            Major bump = breaking tool interface change.
            Required for safe product-scale updates."""

        @property
        @abstractmethod
        def display_name(self) -> str:
            """Human-readable name e.g. 'Time & Calendar'"""

        @abstractmethod
        def get_tools(
            self,
            context: "DomainContext",
        ) -> list["ToolBundle"]:
            """Return tools for this domain.
            Tools are bound to context at creation.
            Max 8 tools per domain.
            Tools must be context-bound, not stateful."""

        @abstractmethod
        async def get_dashboard_data(
            self,
            user_id: str,
            household_id: str,
        ) -> "DashboardData":
            """Standardized summary for HA dashboard.
            Called every 5 minutes by HA REST sensor."""

        def get_context_keys(self) -> list[str]:
            """Memory domains to load for context assembly.
            Default: [self.domain_id]
            Override if domain needs cross-domain memory."""
            return [self.domain_id]

        def get_event_subscriptions(self) -> list[str]:
            """Domain events this domain reacts to.
            e.g. MoneyDomain returns ['appointment_created']
            Default: [] (no subscriptions)"""
            return []

        async def handle_event(
            self,
            event: "DomainEvent",
            context: "DomainContext",
        ) -> None:
            """React to events from other domains.
            Called AFTER autonomy confirmation.
            Never called if user rejects confirmation."""
            pass

### 9.6 DomainContext — Immutable Request Context

Frozen dataclass. Tools receive context at bind time.
Immutable prevents tools from mutating shared state.
This is a one-way door — tools must be pure functions
relative to context.

    @dataclass(frozen=True)
    class DomainContext:
        # Identity
        user_id: str
        household_id: str
        household_timezone: str

        # Services (injected, never imported by domains)
        oauth_manager: Any          # OAuthManager
        storage_adapter: Any        # StorageAdapter
        file_router: Any            # FileStorageRouter
        member_registry: Any        # MemberRegistry
        file_search: Any            # FileSearchService

        # Request metadata
        request_id: str             # UUID, for tracing
                                    # maps to A2A task_id
        channel: str                # "telegram"|"voice"|"dashboard"
        language: str               # "en-US"

        # Autonomy override
        autonomy_override: int | None  # if user said "just do it"

### 9.7 ToolBundle and ToolMetadata

Each tool has runtime logic (@tool) AND metadata.
Metadata is how the AutononomyEngine, DashboardAPI,
and future MCP migration know tool properties.

    @dataclass
    class ToolMetadata:
        name: str                 # matches @tool name
        domain_id: str            # owning domain
        permission: PermissionTier
        is_reversible: bool       # supports undo
        autonomy_level: int       # default: 1=SURFACE
                                  # 3=ACT_SILENT for safe reads
        event_emitted: str | None # event after success

    class PermissionTier(Enum):
        READ       = "read"       # always execute, no confirm
        WRITE      = "write"      # confirm per autonomy config
        DESTRUCTIVE = "destructive" # ALWAYS confirm, no override

    @dataclass
    class ToolBundle:
        tool: BaseTool            # LangChain @tool function
        metadata: ToolMetadata    # Mithra runtime metadata

    ONE-WAY DOOR: PermissionTier.DESTRUCTIVE never
    auto-executes regardless of any autonomy override.
    This cannot be changed without a security review.

### 9.8 Tool Contract — Description Standard

Tool descriptions are what the LLM reads to select tools.
Poor descriptions = wrong tool selection = broken agent.
Follow this pattern (from Claude Code architecture):

    GOOD description:
      """Create a calendar event in Google Calendar.
      Use when user wants to schedule an appointment,
      meeting, reminder, or any timed future event.
      Requires: connected Google account.
      Do NOT use for: past events, general notes,
      tasks without a specific time, recurring budgets."""

    BAD description:
      """Creates an event."""

    Rules:
      1. State what it does (action + service)
      2. State when to use it (use cases)
      3. State prerequisites (connected account etc.)
      4. State when NOT to use it (negative examples)
      Max length: 3-4 sentences.

    ONE-WAY DOOR: tool names use dot notation:
      {domain_id}_{action}_{object}
      e.g. calendar_create_event, people_get_contact
      Changing a tool name breaks all historical logs.

### 9.9 ToolResult Contract

All tools return ToolResult. Never raise exceptions
(except for infrastructure failures). LLM handles errors.

    @dataclass
    class ToolResult:
        success: bool
        summary: str              # ONE-WAY DOOR: always present
                                  # LLM uses this for response
                                  # e.g. "Created dentist appt Tuesday 3pm"
        data: dict | None         # structured result data
        error: str | None         # human-readable error message
        error_code: str | None    # machine-readable error code
                                  # e.g. "GOOGLE_NOT_CONNECTED"
        undo_token: str | None    # if is_reversible, token to undo
        events: list["DomainEvent"]  # side effects to emit

    ONE-WAY DOOR: summary is always present.
    Without it, LLM generates hallucinated summaries.
    Every tool must produce a meaningful summary
    even on failure: "Could not create event: Google not connected"

### 9.10 Domain Events — Cross-Domain Side Effects

Option C: AutononomyEngine wraps all tool executions.
Post-execution hooks emit domain events.
Other domains subscribe and react.

    @dataclass
    class DomainEvent:
        event_type: str           # "appointment_created"
        source_domain: str        # "time"
        payload: dict             # event-specific data
        user_id: str
        household_id: str
        request_id: str           # links to originating request
        timestamp: str            # ISO8601

    ORDERING (critical):
      1. Tool executes → ToolResult with events list
      2. AutononomyEngine confirms with user (if needed)
      3. User says YES → events fire → other domains react
      4. User says NO  → events suppressed, no side effects

    ONE-WAY DOOR: events only fire after confirmation.
    Firing before confirmation creates phantom side effects
    (e.g. MoneyDomain records expense before user confirms booking)

    EXAMPLES:
      TimeDomain.calendar_create_event →
        emits "appointment_created" {date, title, cost?}
        MoneyDomain reacts → records expense if cost present

      MoneyDomain.record_expense →
        emits "expense_recorded" {amount, merchant, category}
        (no subscriber today, reserved for analytics domain)

      ShoppingDomain.add_to_cart →
        emits "item_ordered" {item, price, eta}
        MoneyDomain reacts → records expense
        TimeDomain reacts → adds delivery reminder

### 9.11 Autonomy Integration

AutononomyEngine wraps ALL tool executions.
Tools are pure functions with no autonomy knowledge.
Engine uses PermissionTier + user config to decide.

    EXECUTION FLOW:
      1. LangGraph ToolNode receives tool_call from LLM
      2. CustomToolNode looks up ToolMetadata by tool name
      3. PermissionTier.READ → execute immediately
      4. PermissionTier.WRITE → check user autonomy config
         level 3 (ACT_SILENT) → execute
         level 2 (ACT_TELL)   → execute + notify
         level 1 (SURFACE)    → ask confirmation first
      5. PermissionTier.DESTRUCTIVE → always ask, no override

    AUTONOMY CONFIG DEFAULTS (household_config):
      time.calendar_create_event    → SURFACE (level 1)
      time.calendar_read_events     → ACT_SILENT (level 3)
      money.record_expense          → ACT_TELL (level 2)
      money.delete_expense          → SURFACE (level 1)
      people.add_note               → ACT_TELL (level 2)
      health.*                      → SURFACE always

### 9.12 DomainRegistry

Central registry. Bootstrap populates it.
LangGraph agent queries it per request.

    class DomainRegistry:
        def register(self, domain: Domain) -> None

        def get(self, domain_id: str) -> Domain | None

        def get_tools(
            self,
            domain_ids: list[str],
            context: DomainContext,
        ) -> list[ToolBundle]:
            """Return tools for requested domains.
            Used by LangGraph to bind tools per request."""

        def get_all_domain_ids(self) -> list[str]

        def get_event_subscribers(
            self,
            event_type: str,
        ) -> list[Domain]:
            """Return domains subscribed to this event."""

        def health(self) -> dict:
            """Status of all registered domains."""

### 9.13 Dashboard Integration

Every domain exposes standardized dashboard data
for Home Assistant Lovelace display.

    @dataclass
    class DashboardData:
        domain: str               # domain_id
        updated_at: str           # ISO8601
        summary: str              # "2 events today"
        items: list[DashboardItem]
        alerts: list[str]         # urgent items
        quick_actions: list[dict] # tappable actions

    @dataclass
    class DashboardItem:
        id: str
        title: str
        subtitle: str | None
        icon: str                 # HA MDI icon name
        priority: str             # "high"|"normal"|"low"
        action: str | None        # tool name to call on tap

    GET /api/v1/dashboard/{domain_id}
    GET /api/v1/dashboard/today  (aggregated across all domains)

    HA Lovelace polls every 5 minutes via REST sensor.
    Authenticated via read-only dashboard token stored
    in household_config: "ha_dashboard_token"

### 9.14 Time and Money as Metadata

Important clarification: Time and Money are domains
AND cross-cutting metadata. Resolves apparent complexity.

    AS DOMAIN (when they are the subject):
      "what's on my calendar?" → TimeDomain handles it
      "how much did I spend?"  → MoneyDomain handles it

    AS METADATA (when they are context):
      "book dentist tuesday 3pm" → TimeDomain primary
        start_time is a parameter, not a domain call
      "buy groceries for $50"  → ShoppingDomain primary
        amount is metadata on the shopping record

    RULE: classify_intent() identifies the primary domain.
    Time/money parameters are inputs to that domain's tool.
    MoneyDomain is only invoked when money is the SUBJECT.
    TimeDomain is only invoked when time is the SUBJECT.

    DOMAIN EVENTS handle side effects automatically:
      ShoppingDomain.buy_groceries →
        emits "purchase_made" {amount: 50}
        MoneyDomain reacts → records $50 expense
      User never asks twice. No multi-domain orchestration.

### 9.15 MCP and A2A Migration Path

Design now enables migration with zero domain code changes.

    MCP MIGRATION (external tool providers):
      DomainContext → MCP request context
      ToolBundle.tool.args_schema → MCP tool inputSchema
        (Pydantic generates valid JSON Schema automatically)
      ToolResult → MCP tool response
      Domain → MCPServerAdapter(domain)

      ONE-WAY DOOR: all tool inputs MUST use Pydantic
      models as args_schema. This generates the JSON Schema
      required for both LangChain bind_tools AND MCP servers.
      Never use untyped **kwargs in tools.

    A2A MIGRATION (third-party specialist domains):
      domain_id → A2A Agent Card name
      version → Agent Card version
      DomainContext.request_id → A2A task_id
      ToolResult {success, data, error} → A2A task result
      Domain.handle_event() → A2A event webhook

      For product: third-party domain builders expose
      their domains as A2A agents. Mithra delegates to them
      exactly as it delegates to internal domains today.

### 9.16 Implementation Order

    I1. Create mithra/domains/ package structure
        mithra/domains/__init__.py
        mithra/domains/base.py      (Domain ABC)
        mithra/domains/context.py   (DomainContext)
        mithra/domains/result.py    (ToolResult, DomainEvent)
        mithra/domains/bundle.py    (ToolBundle, ToolMetadata)
        mithra/domains/registry.py  (DomainRegistry)
        mithra/domains/dashboard.py (DashboardData)

    I2. Update LangGraphAdapter
        Add stage 2: load domain tools per classified intent
        Implement CustomToolNode with context injection
        Wire AutononomyEngine as tool execution wrapper
        Wire DomainRegistry for tool lookup

    I3. Add dashboard route to gateway.py
        GET /api/v1/dashboard/{domain_id}
        GET /api/v1/dashboard/today

    I4. Implement Time domain (first domain)
        mithra/domains/time/
        Tests first — domain logic is pure Python

    I5. Each subsequent domain follows same pattern

### 9.18 Deployment Architecture

Decision: Modular monolith for all phases until product scale.

    NOW — Single image (personal use):
      All domains in mithra:latest image
      One requirements.txt for all deps
      Domain = Python package in monorepo
      Deploy: docker compose up

    PRODUCT Phase 1 — Plugin system:
      Domains as pip-installable packages
      pip install git+https://github.com/org/mithra-domain-time
      Bootstrap auto-discovers via entry points:
        [project.entry-points."mithra.domains"]
        time = "mithra_domain_time:TimeDomain"
      Mithra core image unchanged when domains update

    PRODUCT Phase 2 — Separate domain image:
      mithra-core:latest   (gateway, storage, agent)
      mithra-domains:latest (all domain packages)
      Two images, updated independently

    PRODUCT Phase 3 — A2A specialist agents:
      Each domain becomes an A2A agent
      Zero domain logic changes (interface already compatible)
      See section 9.15 for migration path

    Why not microservices now:
      Prerequisites not met: single developer, 2 users,
      no Kubernetes, no distributed tracing,
      no scale requirements.
      42% of organizations that adopted microservices
      early consolidated back (CNCF 2025 survey).
      Modular monolith gives structural readiness
      without operational complexity.

    Domain dependency isolation:
      Each domain declares its pip dependencies
      in a comment block at top of __init__.py:
        # Domain dependencies:
        # google-api-python-client>=2.0
        # google-auth-oauthlib>=1.0
      When domains become pip packages,
      these move to pyproject.toml automatically.

### 9.17 Alternatives Considered

    MCP for internal domains (rejected):
      Internal domains run in same process.
      MCP adds HTTP round-trip overhead for local calls.
      LangChain @tool is the right layer for in-process tools.
      Revisit: when third-party domain builders need extension.

    A2A for domain coordination (rejected for now):
      All domains run in same Python process.
      Domain events (Option C) are simpler and sufficient.
      A2A is for cross-vendor, cross-platform agent coordination.
      Revisit: when Mithra supports third-party specialist agents.

    Supervisor pattern for all multi-domain requests (rejected):
      90% of requests are single-domain.
      Domain events handle side effects without orchestration.
      Supervisor doubles token spend on routing calls.
      Build when 3+ domain requests become common.

    Swarm pattern (rejected for now):
      Hard to debug, agents need to know about each other.
      Domain isolation principle violated.
      Revisit for specific well-defined pipelines (Phase 3+).

    One agent with all tools loaded (rejected):
      50+ tool context window degrades LLM reasoning.
      Two-stage selection keeps tools per request ≤ 15.
      Industry consensus: inject only relevant tools.

### 9.19 Confirmation Flow (Complete Design)

This section captures the full round-trip for WRITE and DESTRUCTIVE
tool confirmations. It is the authoritative reference for any code
that touches pending actions, confirm(), or user identity at
execution time.

#### Step 1 — Pending Action Creation (_node_execute_tool)

    When _execute_with_autonomy() returns requires_confirmation=True,
    _node_execute_tool constructs the pending action:

        pending_actions = [{
            "domain":       "time",
            "action_type":  "calendar_create_event",
            "tool_name":    "calendar_create_event",  # exact tool name
            "payload": {
                **tool_args,                           # all tool inputs
                "user_id": request.user_id,            # REQUIRED — see below
            },
            "output_channel": "telegram",
            "autonomy_level": 1,
            "description":  "Create 'Dentist appointment' Tuesday 3pm",
        }]

    WHY user_id is required in payload:
      confirm() is called by the gateway with the Telegram user_id
      of whoever tapped [Yes]. That may be household admin, not the
      user who made the original request. Without payload["user_id"],
      the tool executes as the wrong user → wrong OAuth token →
      "Google not connected for household" error.

#### Step 2 — Engine Storage (DefaultAutonomyEngine.process())

    process() stores pending actions in StorageAdapter:
        domain="autonomy", layer="state",
        key=f"pending_{request_id}_{action_index}"
    Expires 24 hours after creation.
    Returns ExecutionReceipt with pending list populated.

#### Step 3 — User Confirmation (Telegram)

    TelegramOutputAdapter sends inline keyboard:
        [✅ Yes, do it] [❌ No]
    Callback data encodes: request_id + action_index
    User taps → TelegramInputAdapter routes to
    gateway confirm() handler → engine.confirm() called.

#### Step 4 — confirm() Execution Paths

    exec_user_id = pending.payload.get("user_id") or user_id

    Path A — Domain Registry (preferred):
      domain = self._domain_registry.get(pending.domain)
      if domain:
          ctx = self._build_execution_context(exec_user_id)
          bundles = await asyncio.to_thread(domain.get_tools, ctx)
          bundle = next(b for b in bundles
                        if b.tool.name == pending.payload["tool_name"])
          result = await asyncio.to_thread(
              bundle.tool.invoke, pending.payload)
          # write ActionLogEntry, return receipt

    Path B — Executor Fallback:
      If domain not registered OR Path A raises exception:
          action = AgentAction(
              action_type=pending.action_type,
              domain=pending.domain,
              payload=pending.payload,
              output_channel=pending.output_channel,
          )
          exec_result = await executor.execute(action)
          # DomainToolAdapter handles via register_fallback()

    Invariant: NEVER route health domain through Path B.
    Health domain tools must only execute via Path A with a
    DomainContext that has LocalStorageAdapter.

#### Step 5 — Result to User

    ExecutionReceipt returned to gateway:
        executed: [ActionLogEntry(...)]
        pending:  []
        response_text: "Done! 'Dentist appointment' created for
                        Tuesday June 3 at 3:00 PM."
    Gateway passes response_text to TelegramOutputAdapter.send().

#### Key Invariants (one-way doors)

    1. payload["user_id"] ALWAYS set at plan time, ALWAYS read at
       confirm time. No exceptions.

    2. exec_user_id used for DomainContext — never "household" for
       personal tool execution.

    3. confirm() catches Path A exceptions with WARNING log and
       falls through to Path B. Path B exceptions use ERROR log
       and return a failed receipt. Neither path propagates to caller.

    4. ActionLogEntry written on BOTH success and failure.
       Audit trail must be complete regardless of tool outcome.

    5. PermissionTier.DESTRUCTIVE ALWAYS goes through confirmation.
       No autonomy level override bypasses this.
       Enforced in _execute_with_autonomy() — one-way door.

---

### Logging Standard for Domains

All domain tool files must use this pattern:

    import logging
    _logger = logging.getLogger(__name__)

    class SomeTool(BaseTool):
        def _run(self, **kwargs) -> dict:
            _logger.debug("%s called: %s", self.name, kwargs)
            try:
                result = do_the_work(...)
                _logger.info("%s succeeded: %s", self.name, result.summary)
                return ToolResult(success=True, ...).model_dump()
            except KnownError as exc:
                _logger.warning("%s: %s", self.name, exc)
                return ToolResult(success=False,
                                  summary=f"...: {exc}",
                                  error=str(exc)).model_dump()
            except Exception as exc:
                _logger.error("%s unexpected error", self.name,
                              exc_info=True)
                return ToolResult(success=False,
                                  summary=f"Unexpected error",
                                  error=str(exc)).model_dump()

    Rules:
      DEBUG  — entry point, kwargs (sanitise before logging PII)
      INFO   — success with key identifiers (event_id, not full data)
      WARNING — user-facing failures (Google not connected, parse fail)
      ERROR  — unexpected exceptions, always exc_info=True
      Never log access_token, refresh_token, or any OAuth credential.

---

## 10. Time Domain

### Purpose
The Time domain manages everything related to how the
household organizes time — scheduling, reminders,
recurring events, availability, and daily briefings.
It is the most frequently used domain and the first
to be implemented.

### Scope Boundaries
    IN SCOPE (Time domain owns):
      Calendar event management (CRUD)
      Recurring events and schedules
      Reminders and notifications
      Availability checking
      Daily/weekly briefings
      Multi-user calendar view

    OUT OF SCOPE (handled elsewhere):
      Contacts for event attendees → People domain
      Expense tracking for appointments → Money domain
      Flight/hotel bookings → Travel domain
      Medication reminders → Health domain
      Household maintenance schedules → Home domain (Phase 2)
      Proactive email capture of events → Documents domain

---

### 10.1 Use Case Inventory

#### Category 1 — Event Management (CRUD)
    "Add dentist appointment Tuesday 3pm"
    "Schedule dinner with John next Friday 7pm"
    "Block 2 hours tomorrow morning for deep work"
    "Add all-day event: Wife's birthday March 15"
    "Reschedule my dentist to next week same time"
    "Move all Thursday meetings to Friday"
    "Cancel tomorrow's meeting"
    "Delete all events named standup this week"
    "Update dentist appointment to 4pm"
    "Add team meeting for 1 hour with video call link"

#### Category 2 — Recurring Events
    "Add weekly trash pickup every Tuesday at 7am"
    "Add recycling every other Tuesday at 7am"
    "Set up daily standup every weekday at 9am"
    "Add rent reminder on the 1st of every month"
    "Add wife's birthday reminder every year March 15"
    "Add weekly date night every Friday 7pm"
    "Stop recurring standup after June 30"
    "Change trash day from Tuesday to Wednesday"
    "Skip trash pickup next Tuesday (holiday)"
    "Add quarterly dentist checkup every 3 months"

#### Category 3 — Querying and Reading
    "What's on my calendar today?"
    "What do I have this week?"
    "What's happening tomorrow?"
    "Show me next week's schedule"
    "When is my next dentist appointment?"
    "What did I do last Tuesday?"
    "How many meetings do I have this week?"
    "Show all events with John this month"
    "What events are on my work calendar?"
    "Show wife's schedule for today"

#### Category 4 — Availability Checking
    "Am I free Saturday afternoon?"
    "Are we both free Sunday for brunch?"
    "Find a free 2-hour slot this week"
    "Do I have any conflicts next week?"
    "When is the next free evening this week?"
    "Is Tuesday morning free for a doctor appointment?"

#### Category 5 — Reminders
    "Remind me to call mom on Sunday at 3pm"
    "Remind me 30 minutes before dentist"
    "Remind me every Monday to send weekly update"
    "Set a reminder to take medication at 8am daily"
    "Remind me the night before trash day"
    "Alert me 1 hour before any appointment downtown"

#### Category 6 — Briefings (on-demand)
    "What's happening today?"
    "Give me my daily briefing"
    "What does my week look like?"
    "Any conflicts this week?"
    "What's on for the weekend?"
    "What events do I have coming up this month?"

#### Category 7 — Household Schedules
    "Set up weekly trash pickup every Tuesday"
    "Add alternating recycling every other Tuesday"
    "Remind us the night before trash day"
    "Skip trash next week — it's a holiday"
    "Remind me to pay rent on the 1st"
    NOTES:
      Trash/recycling = recurring calendar events
      Biweekly recycling = WEEKLY interval=2 recurrence
      Schedule changes handled via update/exception
      Municipality ICS subscription → Phase 2
        (when Documents domain can handle URL import)

#### Category 8 — Multi-User Calendar
    "What does Wife have tomorrow?"
    "Are we both free Saturday?"
    "Add family dinner for both of us next Sunday"
    "Show our combined schedule this week"
    "Add dentist for Wife on Thursday 2pm"
    NOTES:
      Each member's calendar accessed via their own
      Google OAuth token (OAuthManager per user_id)
      Wife's calendar reads available once she connects Google
      Events added to specific user's calendar by default
      Shared events duplicated to both calendars

---

### 10.2 Natural Language Time Parsing

#### The Problem
LLM-based time parsing is non-deterministic:
  - Same input can produce different ISO8601 outputs
  - Timezone handling is unreliable
  - Edge cases fail: "first Tuesday of next month"
  - Not testable (different output each run)

#### The Solution: Deterministic Hybrid
    LLM:        understands intent, extracts time string
                "add dentist tuesday 3pm" →
                LLM calls calendar_create_event with
                start_time="tuesday 3pm"

    dateparser: converts deterministically
                "tuesday 3pm" + TIMEZONE=America/Chicago →
                2026-06-03T15:00:00-05:00

    This is always correct, always testable,
    always timezone-aware.

#### dateparser Configuration
    import dateparser

    def parse_datetime(
        text: str,
        timezone: str,
        prefer_future: bool = True,
    ) -> datetime | None:
        return dateparser.parse(text, settings={
            "TIMEZONE": timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_FUTURE_DATES": prefer_future,
            "PREFER_DAY_OF_MONTH": "first",
            "TO_TIMEZONE": timezone,
        })

    Handles:
      "next Tuesday"             → next Tue from today
      "first Monday of month"    → deterministic
      "in 3 days"                → relative from now
      "tuesday 3pm"              → this or next Tuesday
      "March 15"                 → next March 15
      "every Monday"             → recurrence base date
      "tomorrow morning"         → 9am tomorrow (configurable)

#### Tool Input Design (one-way door)
    Tools receive raw time strings from LLM.
    Tools call parse_datetime() internally.
    Tools NEVER receive pre-parsed ISO8601 from LLM.
    Tools NEVER ask LLM to produce ISO8601.

    WHY: LLM-produced ISO8601 is unreliable.
    parse_datetime() is deterministic and testable.

    If parse_datetime() fails → tool returns ToolResult.fail()
    with error_code="UNPARSEABLE_DATE" and asks LLM to
    reformulate: "Could not understand 'blurp' as a date.
    Try: 'next Tuesday at 3pm'"

---

### 10.3 Library Stack

#### gcsa — Google Calendar Simple API
    pip install gcsa>=2.0.0

    Why gcsa over raw Google API:
      Pythonic objects instead of raw JSON dicts
      Event, Recurrence, Attendee classes
      Recurring events with full RRULE support
      Reminders built in (popup, email)
      Pagination handled automatically

    Auth integration:
      gcsa accepts google.oauth2.credentials.Credentials
      We build Credentials from OAuthManager tokens:

        from google.oauth2.credentials import Credentials
        from gcsa.google_calendar import GoogleCalendar

        token_data = oauth_manager._store.load(
            "google", user_id)
        creds = Credentials(
            token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/calendar"],
            token_uri="https://oauth2.googleapis.com/token",
        )
        gc = GoogleCalendar(credentials=creds)

    gcsa will auto-refresh if access_token is expired.
    Our OAuthManager token refresh is the primary path;
    gcsa refresh is the fallback.

#### dateparser — Natural Language Date Parser
    pip install dateparser>=1.4.0

    Why dateparser:
      Actively maintained (v1.4.0 March 2026)
      Built on dateutil, extended for NL
      Timezone-aware output
      Handles relative expressions
      Deterministic given same input + settings

#### Deferred Libraries (for Home Assistant integration)
    gcal-sync (defer to HA phase):
      asyncio-based Google Calendar sync to local storage
      Used by Home Assistant internally
      Relevant when Mithra syncs events to HA for display
      Without HA: gcsa is sufficient for Phase 1

    beautiful_date (defer, optional):
      Companion library to gcsa for readable date code
      e.g. 15/Mar/2026 instead of date(2026, 3, 15)
      Useful for scheduled jobs and fixtures
      Not needed until scheduled briefing jobs are built

---

### 10.4 GoogleCalendarService

Thin service layer between Time domain tools and gcsa.
Per-user — each call creates/reuses a per-user gc instance.
Handles credential injection from OAuthManager.

    class GoogleCalendarService:
        """Thin wrapper around gcsa for Time domain.

        Accepts OAuthManager context and provides
        clean async interface over gcsa's sync API.
        Creates GoogleCalendar instance per user.
        """

        def __init__(
            self,
            oauth_manager,
            household_id: str,
            client_id: str,
            client_secret: str,
        ):
            self._oauth = oauth_manager
            self._household_id = household_id
            self._client_id = client_id
            self._client_secret = client_secret
            self._gc_cache: dict[str, Any] = {}

        def _get_gc(self, user_id: str) -> "GoogleCalendar":
            """Get or create GoogleCalendar for user."""
            if user_id in self._gc_cache:
                return self._gc_cache[user_id]
            token_data = self._oauth._store.load(
                "google", user_id)
            if not token_data:
                raise RuntimeError(
                    f"Google not connected for {user_id}. "
                    f"Send 'connect google' to Mithra.")
            from google.oauth2.credentials import Credentials
            from gcsa.google_calendar import GoogleCalendar
            creds = Credentials(
                token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token"),
                client_id=self._client_id,
                client_secret=self._client_secret,
                scopes=token_data.get("scopes", [
                    "https://www.googleapis.com/auth/calendar"]),
                token_uri="https://oauth2.googleapis.com/token",
            )
            gc = GoogleCalendar(credentials=creds)
            self._gc_cache[user_id] = gc
            return gc

        Methods:
          list_events(user_id, start, end) -> list[Event]
          get_event(user_id, event_id) -> Event | None
          create_event(user_id, event) -> Event
          update_event(user_id, event) -> Event
          delete_event(user_id, event_id) -> None
          get_free_busy(user_id, start, end) -> list[dict]
          list_calendars(user_id) -> list[Calendar]

---

### 10.5 Tool Set

Maximum 8 tools. All use Pydantic args_schema (MCP-compatible).
All return ToolResult. All use dateparser for time parsing.

#### Tool 1: calendar_read_events
    Permission:   READ
    Reversible:   false
    Autonomy:     ACT_SILENT (3)
    Event:        none

    Description:
      List calendar events for a time range.
      Use when user asks what is on their calendar,
      what they have today/this week/this month,
      or wants to see upcoming appointments.
      Do NOT use for: checking availability (use
      calendar_check_availability), or adding events.

    Input schema:
      time_range: str  "today", "tomorrow", "this week",
                       "next week", "this month", or
                       "from X to Y"
      user_id:    str  default: requesting user
      calendar:   str  default: "primary"

    Output summary examples:
      "3 events today: Dentist at 3pm, Gym at 6pm,
       Dinner with John at 8pm"
      "No events scheduled for tomorrow"

#### Tool 2: calendar_create_event
    Permission:   WRITE
    Reversible:   true
    Autonomy:     SURFACE (1)
    Event:        "appointment_created"

    Description:
      Create a new calendar event.
      Use when user wants to schedule an appointment,
      meeting, reminder, or any future event.
      Requires Google to be connected.
      Do NOT use for: recurring events (use
      calendar_create_recurring), or reminders without
      a specific time.

    Input schema:
      title:       str  event name
      start_time:  str  natural language e.g. "tuesday 3pm"
      duration_minutes: int  default 60
      description: str  optional
      location:    str  optional
      user_id:     str  default: requesting user
      add_reminder_minutes: int  optional, popup reminder

    Output summary:
      "Created 'Dentist appointment' for Tuesday June 3
       at 3:00 PM (1 hour). Added to your Google Calendar."

#### Tool 3: calendar_create_recurring
    Permission:   WRITE
    Reversible:   true
    Autonomy:     SURFACE (1)
    Event:        "recurring_event_created"

    Description:
      Create a recurring calendar event.
      Use for events that repeat: weekly meetings,
      monthly bills, annual birthdays, trash pickup,
      medication reminders, exercise routines.
      Do NOT use for: one-time events (use
      calendar_create_event).

    Input schema:
      title:          str   event name
      start_time:     str   first occurrence natural language
      duration_minutes: int default 30
      frequency:      str   "daily"|"weekly"|"monthly"|"yearly"
      interval:       int   default 1 (2=every other week)
      by_week_day:    str   optional "MO,TU,WE,TH,FR,SA,SU"
      by_month_day:   int   optional day of month (1-31)
      end_date:       str   optional "after 10 occurrences"
                            or "until Dec 31 2026"
      description:    str   optional
      user_id:        str   default: requesting user

    Example inputs:
      trash:      title="Trash pickup" start_time="next Tuesday 7am"
                  frequency="weekly" by_week_day="TU"
      recycling:  same but interval=2
      standup:    frequency="weekly" by_week_day="MO,TU,WE,TH,FR"
      birthday:   frequency="yearly"
      rent:       frequency="monthly" by_month_day=1

    Output summary:
      "Created recurring 'Trash pickup' every Tuesday
       at 7:00 AM. First occurrence June 3, 2026."

#### Tool 4: calendar_update_event
    Permission:   WRITE
    Reversible:   true
    Autonomy:     SURFACE (1)
    Event:        "appointment_updated"

    Description:
      Update an existing calendar event.
      Use when user wants to reschedule, rename,
      or modify an event.
      For recurring events: specify if changing
      this occurrence only or all future occurrences.
      Do NOT use for: deleting events.

    Input schema:
      event_query:   str   event title or description
                           to find the event
      new_time:      str   optional new time
      new_title:     str   optional new title
      new_duration:  int   optional new duration minutes
      new_location:  str   optional
      scope:         str   "this"|"future"|"all"
                           for recurring events
      user_id:       str   default: requesting user

    Output summary:
      "Updated 'Dentist appointment' to Friday June 6
       at 3:00 PM."

#### Tool 5: calendar_delete_event
    Permission:   DESTRUCTIVE
    Reversible:   false
    Autonomy:     always confirm (DESTRUCTIVE)
    Event:        "appointment_cancelled"

    Description:
      Delete a calendar event permanently.
      Always requires confirmation. Cannot be undone.
      Use when user explicitly wants to cancel/delete.
      For recurring events: specify scope.
      Do NOT use for: rescheduling (use update instead).

    Input schema:
      event_query:   str   event title to find
      scope:         str   "this"|"future"|"all"
      user_id:       str   default: requesting user

    Output summary:
      "Deleted 'Monday standup' on June 3. This was
       a recurring event — only this occurrence deleted."

#### Tool 6: calendar_check_availability
    Permission:   READ
    Reversible:   false
    Autonomy:     ACT_SILENT (3)
    Event:        none

    Description:
      Check if a user (or multiple users) is free
      during a time period. Use when asked about
      availability, finding free slots, or checking
      for conflicts.
      Do NOT use for: listing events (use read_events).

    Input schema:
      time_range:   str        "saturday afternoon",
                               "this week", "tuesday 2-4pm"
      duration_minutes: int    optional min duration needed
      user_ids:     list[str]  default: [requesting user]
                               ["all"] for all household members

    Output summary:
      "You're free Saturday afternoon 1pm-6pm."
      "You and Wife are both free Sunday evening."
      "No free 2-hour slot found this week. Closest:
       Monday 4-6pm or Wednesday 3-5pm."

#### Tool 7: calendar_daily_briefing
    Permission:   READ
    Reversible:   false
    Autonomy:     ACT_SILENT (3)
    Event:        none

    Description:
      Generate a structured briefing of upcoming events.
      Use when user asks about their day, week, or
      requests a schedule summary.
      Includes: today's events, upcoming reminders,
      relevant weather context, household member events.
      Do NOT use for: adding or modifying events.

    Input schema:
      period:    str   "today"|"tomorrow"|"week"|"weekend"
      user_id:   str   default: requesting user
      include_household: bool  default true

    Output summary:
      "Your day: Dentist at 3pm (in 4 hours, downtown),
       Gym at 6pm. Wife has book club at 7pm.
       Trash pickup tomorrow — remember to put bins out."

#### Tool 8: calendar_find_time
    Permission:   READ
    Reversible:   false
    Autonomy:     ACT_SILENT (3)
    Event:        none

    Description:
      Find the best available time slot for a new event.
      Use when user wants to schedule something but
      hasn't specified a time, or asks for suggestions.
      Considers all household members if requested.
      Do NOT use for: checking specific time availability
      (use check_availability).

    Input schema:
      duration_minutes:  int      required
      within_period:     str      "this week", "next week"
      preferred_time:    str      optional "morning"|"afternoon"
                                  |"evening"
      participants:      list[str] optional user_ids

    Output summary:
      "Best slots for a 1-hour meeting this week:
       Monday 2pm, Wednesday 10am, Thursday 3pm.
       Which works for you?"

---

### 10.6 Cross-Domain Side Effects

Time domain emits these events after tool execution.
Other domains subscribe and react automatically.

    appointment_created:
      Payload: {title, start_time, end_time,
                event_id, user_id, calendar_id}
      Subscribers:
        MoneyDomain → record expense if cost mentioned
        PeopleDomain → link contact if attendee mentioned

    appointment_updated:
      Payload: {event_id, old_time, new_time, user_id}
      Subscribers:
        PeopleDomain → update contact interaction log

    appointment_cancelled:
      Payload: {event_id, title, original_time, user_id}
      Subscribers:
        MoneyDomain → flag potential refund if expense linked

    recurring_event_created:
      Payload: {title, frequency, start_date, user_id}
      Subscribers: none today (reserved)

---

### 10.7 Proactive Briefings (Scheduled Jobs)

Daily briefings are BOTH a tool (on demand) AND
a scheduled job (proactive push).

    ON DEMAND (tool — Phase 1):
      User: "what's today?"
      LLM calls calendar_daily_briefing
      Returns structured summary

    SCHEDULED (proactive — Phase 1b):
      Fires at household configured time (default 7am)
      Sends Telegram message without user asking
      Uses same underlying data fetch as the tool

    Implementation for scheduled jobs:
      APScheduler in healthmonitor container
      OR FastAPI background tasks at startup
      Configured in household_config:
        "briefing_enabled": "true"
        "briefing_time": "07:00"
        "briefing_channel": "telegram"

    NOTE: Scheduled briefings require scheduler
    infrastructure. Implement tool first (simpler),
    add scheduler when Time domain is stable.
    Scheduler implementation is separate task.

---

### 10.8 Dashboard Data

    GET /api/v1/dashboard/time

    DashboardData:
      domain:    "time"
      summary:   "2 events today"
      items:
        - id: event_id
          title: "Dentist appointment"
          subtitle: "3:00 PM today"
          icon: "mdi:calendar"
          priority: "high" (if within 2 hours)
          action: "calendar_read_events"
      alerts:
        - "Trash pickup tomorrow"
        - "Rent reminder in 3 days"
      quick_actions:
        - label: "Add event"
          action: "calendar_create_event"
        - label: "Today's briefing"
          action: "calendar_daily_briefing"

---

### 10.9 Personal vs Household Events — Classification Model

#### The Core Problem
Every calendar event has a scope: personal or household.
Scope determines:
  - Whether it shows on the HA shared wall dashboard
  - Whether it appears in household briefings
  - Whether other members can see it

Personal: affects only one member
  "add gym tomorrow morning"
  "add dentist tuesday 3pm"
  "add meeting with John"

Household: relevant to the whole household
  "add trash pickup every thursday"
  "add family dinner saturday"
  "add rent reminder 1st of month"
  "add anniversary dinner march 15"

#### Classification Strategy

Three phases, increasing intelligence:

    PHASE 1 — Keyword heuristic (implemented now):
      Household triggers (any match → household):
        trash, recycling, rent, mortgage, utilities,
        family, anniversary, birthday (household members),
        both, we, us, everyone, kids, children,
        dinner (when combined with family/us/we),
        holiday, school, pickup, dropoff
      Default: personal

      Recurring events default to household if
      they match household triggers.
      Single medical/personal events always personal.

    PHASE 2 — LLM classification (future):
      For ambiguous events not matched by keywords,
      LLM classifies:
        "add dentist" → personal (medical)
        "add car service" → personal
        "add anniversary dinner" → household
        "add school pickup" → household
      LLM result stored in scope field.

    PHASE 3 — User correction (future):
      "make dentist appointment household"
      → updates scope in SQLite + Google extended property
      User has final authority over classification.

#### Source of Truth
    Mithra SQLite (calendar_events table) = source of truth
    Google Calendar extended properties = mirror/cache
    (extended properties added in Phase 3 via raw API)

    This means:
      Classification survives even if Google Calendar
      extended properties are not yet implemented.
      Phase 3 sync is additive, not a migration.

---

### 10.10 Calendar Data Architecture

#### Phase 1 — Live queries + local event index (NOW)

    Writes: gcsa creates events in Google Calendar.
    Reads: live Google Calendar API calls via gcsa.
    Index: every Mithra-created event recorded in
           calendar_events SQLite table with scope.

    Why index now:
      Events created TODAY lose their scope context
      if not recorded. Adding the table now costs
      nothing and means Phase 2 sync is additive.

    Dashboard (Phase 1):
      Live query Google Calendar for today's events.
      Returns all events for the owner user.
      No scope filtering yet (all events shown).

#### Phase 2 — Incremental sync + cached reads (HA phase)

    WHY sync at all:
      External events (flights auto-added by Gmail,
      restaurant reservations, meeting invites) need
      to appear on dashboard and in briefings.
      syncToken approach gets ALL events regardless
      of how they were created.

    HOW — Native Google Calendar syncToken:
      50 lines of code. No new library.
      Google returns only changes since last sync.
      Efficient: 24 API calls/day for 2-member household.

      Sync token stored in household_config:
        key:   "calendar_sync_token_{user_id}"
        value: opaque token string from Google

      Sync flow (triggered on dashboard poll):
        1. Load syncToken from household_config
        2. GET events?syncToken=xxx → only changes
        3. Upsert changes into calendar_events table
        4. Classify new external events (keyword heuristic)
        5. Store new syncToken
        6. On 410 Gone: full resync (token expired)

      Dashboard reads from local cache (fast).
      Tool reads (briefing, availability) still use
      live API for freshness.

    WHY NOT gcal-sync library:
      gcal-sync is correct at product scale (100+
      households, multiple calendar providers).
      For household of 2, Google Calendar only:
      native syncToken = 50 lines vs full library.
      Revisit when Mithra supports multiple calendar
      providers simultaneously.

#### Phase 3 — Categorization + extended properties

    LLM classifies new external events:
      "Delta flight NYC" → scope=household
      "Dentist"          → scope=personal

    Extended properties via raw Google API:
      PATCH event body:
        {extendedProperties: {shared: {
          mithra_scope: "household",
          mithra_household_id: "hh_b76fef08"
        }}}

    Dashboard scope filtering:
      GET /api/v1/dashboard/time?scope=household
      Returns household-tagged events from ALL members.

#### Product Scale
    SQLite per household (self-hosted): scales infinitely.
    Each household has isolated SQLite — no contention.
    Cloud product: StorageAdapter swap (SQLite → Postgres).
    Schema is identical. Zero domain code changes.
    This is the existing StorageAdapter contract (section 7).

---

### 10.11 calendar_events Table Schema

Added to SQLite in Phase 1. Schema designed for all phases.

    CREATE TABLE calendar_events (
        event_id      TEXT PRIMARY KEY,
        user_id       TEXT NOT NULL,
        household_id  TEXT NOT NULL,
        summary       TEXT,
        start_time    TEXT,         -- ISO8601 with offset
        end_time      TEXT,         -- ISO8601 with offset
        is_recurring  INTEGER DEFAULT 0,
        source        TEXT DEFAULT 'mithra',
                                    -- 'mithra' | 'external'
        scope         TEXT DEFAULT 'personal',
                                    -- 'personal' | 'household'
        calendar_id   TEXT DEFAULT 'primary',
        raw_json      TEXT,         -- full event JSON
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );

    CREATE INDEX idx_cal_events_household
        ON calendar_events(household_id, start_time);
    CREATE INDEX idx_cal_events_user
        ON calendar_events(user_id, start_time);
    CREATE INDEX idx_cal_events_scope
        ON calendar_events(household_id, scope, start_time);

    source field:
      'mithra'   → created by Mithra tools
      'external' → synced from Google (Phase 2)

    scope field:
      'personal'  → affects one member only
      'household' → relevant to whole household
      null        → not yet classified (external events
                    before Phase 2 classification)

---

### 10.12 Scope Classification Helper

Used at event creation time in all calendar tools.

    HOUSEHOLD_KEYWORDS = {
        # Household chores
        "trash", "recycling", "garbage", "bins",
        "rent", "mortgage", "utilities", "bills",
        # Family
        "family", "anniversary", "birthday",
        "kids", "children", "school", "pickup",
        "dropoff", "daycare",
        # Inclusive pronouns
        "both", "we", "us", "everyone", "together",
        # Household events
        "dinner" (only with family/us/we context),
        "holiday", "vacation", "trip",
        "hoa", "neighborhood",
    }

    def classify_event_scope(
        title: str,
        user_text: str,
        is_recurring: bool,
    ) -> str:
        """Classify event as 'personal' or 'household'.
        Phase 1: keyword heuristic.
        Returns 'household' or 'personal'.
        """
        text = (title + " " + user_text).lower()
        for keyword in HOUSEHOLD_KEYWORDS:
            if keyword in text:
                return "household"
        # Recurring events with no personal keywords
        # tend to be household (trash, reminders etc)
        personal_recurring = {
            "gym", "workout", "medication", "therapy",
            "commute", "lunch", "standup", "meeting"
        }
        if is_recurring:
            for kw in personal_recurring:
                if kw in text:
                    return "personal"
            # Ambiguous recurring → household by default
            return "household"
        return "personal"

---

### 10.13 Managed Members (Children)

    Children are managed members — no Telegram required.
    Parent manages their calendar on their behalf.

    Schema additions to members table (added now):
      managed_by     TEXT  nullable FK → members.id
      date_of_birth  TEXT  nullable ISO8601 date

    Parent connects child's Google account:
      "connect Emma's Google calendar"
      → OAuth in parent's browser
      → token stored for child's user_id

    Phase 1: children added as regular members.
             Parent manages via Mithra commands.
    Phase 2: "for Emma" natural language routing.
    Phase 3: age-based auto-transition at 18.

---

### 10.14 Multiple Google Accounts Per Person

    Phase 1: one Google account per user_id.

    Phase 2 when needed:
      member_preferences:
        google_calendar_primary → personal account
        google_calendar_work    → work account
      OAuthManager key: "google_work:{user_id}"

---

## 10.15 Time Domain vs Tasks Domain — Boundary Rules

### The One-Question Test
Ask: "Does this need a specific clock time to exist?"

  YES → Time domain (calendar event or reminder)
  NO  → Tasks domain (action item or routine)

This question resolves all ambiguity.

### Definitive Examples

  TIME DOMAIN:
    "Dentist tuesday 3pm" → fixed time
    "Trash reminder every thursday 8pm" → fixed time
    "Medication reminder daily 8am" → fixed time
    "Flight thursday 6am" → fixed time
    "Team standup every monday 9am" → fixed time
    "Remind me 30min before dentist" → relative to fixed time

  TASKS DOMAIN:
    "Fix ceiling fan" → no fixed time
    "Call insurance" → no fixed time
    "Return Amazon package by Friday" → deadline, not clock time
    "Mix baby formula" (routine checklist) → no clock time
    "Woodworking project phase 1" → no fixed time
    "Clean garage this month" → no fixed time

### The Bridge (opt-in, never automatic)
  When a task needs a calendar block:
    User: "find time this week for ceiling fan repair"
    Mithra: suggests free slots
    User: confirms → Time domain creates calendar block
    Task status → "scheduled"

  Time domain NEVER auto-creates blocks for tasks.
  Tasks domain NEVER creates calendar events directly.
  Bridge is always user-initiated.

### Reminder Classification
  "Remind me to X" requires disambiguation:
    + at specific time → Time domain
      "Remind me to call mom Sunday at 3pm"
      → calendar event with notification

    + without specific time → Tasks domain
      "Remind me to call mom this week"
      → task with due date, no clock time

    + daily checklist → Tasks domain (routine)
      "Remind me to mix baby formula daily"
      → routine checklist item, daily reset

### Recurrence Classification
  Recurring WITH fixed time → Time domain
    "Every thursday 8pm trash"
    "Every monday 9am standup"
    "Every 1st of month rent reminder at 9am"

  Recurring WITHOUT fixed time → Tasks domain (routine)
    "Mix baby formula daily" (checklist)
    "Soak lentils night before dosa" (checklist)
    "Water plants every 3 days" (task with recurrence)

### Intent Routing
  classify_intent() maps to domain:

  calendar.*   → Time domain
  reminder.*   → Time domain (if time specified)
               → Tasks domain (if no time)
  task.*       → Tasks domain
  routine.*    → Tasks domain
  project.*    → Tasks domain
  todo.*       → Tasks domain

  LLM resolves ambiguous "remind me" intents by
  checking if a specific time was mentioned.
  If yes → calendar reminder. If no → task.

### What This Means for Implementation
  Time domain tools NEVER write to tasks table.
  Tasks domain tools NEVER write to calendar_events.
  Cross-domain bridge ONLY via explicit user request.
  classify_intent() is the routing authority —
  it must correctly route to the right domain.
  Any ambiguity in intent → ask user to clarify.

---

## 11. Tasks Domain

### 11.1 Purpose and Value Proposition

The Tasks domain is Mithra's accountability system for
things that need to happen but don't have a fixed time.
Its core value: nothing important slips because everything
has an owner, a priority, and visibility.

Distinct from Time domain (section 10.15):
  Time domain: things that MUST happen at a specific time
  Tasks domain: things YOU decide when to do

### 11.2 Entity Types

Three distinct entities with different behavior:

    TASKS — one-time action items
      Created once, completed once, archived.
      May have deadline (hard or soft) or none.
      Examples: fix ceiling fan, return Amazon package,
                call insurance, install dining room light

    PROJECTS + MILESTONES — multi-step work
      Project is a container with a target date.
      Milestones are ordered phases within a project.
      Progress tracked as % milestones complete.
      Examples: backyard deck, woodworking bench,
                bathroom renovation

    ROUTINES — daily-reset checklists
      Same items every day. Both household members
      can mark done. Resets at configured time (midnight
      default or custom e.g. 9pm for evening routines).
      NOT tasks — they recur without being recreated.
      Examples: mix baby formula, warm water for baby,
                soak lentils, evening wind-down checklist

### 11.3 Schema

Five tables. Add to SQLite via local.py DDL.
Safe migrations for existing installs.

    tasks (
        id              TEXT PRIMARY KEY,
        household_id    TEXT NOT NULL,
        user_id         TEXT NOT NULL,        -- owner
        title           TEXT NOT NULL,
        notes           TEXT,                 -- context, instructions
        scope           TEXT DEFAULT 'personal',  -- personal|household
        priority        TEXT DEFAULT 'normal',    -- urgent|high|normal|someday
        status          TEXT DEFAULT 'inbox',     -- see 11.4
        due_date        TEXT,                 -- ISO8601 date, nullable
        project_id      TEXT,                 -- FK → projects.id
        milestone_id    TEXT,                 -- FK → milestones.id
        snoozed_until   TEXT,                 -- ISO8601, nullable
        source          TEXT DEFAULT 'manual',    -- manual|gmail|calendar
        source_ref      TEXT,                 -- email_id, event_id etc.
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        completed_at    TEXT                  -- nullable
    );

    CREATE INDEX idx_tasks_household ON tasks(household_id, status);
    CREATE INDEX idx_tasks_user ON tasks(user_id, status);
    CREATE INDEX idx_tasks_due ON tasks(household_id, due_date);
    CREATE INDEX idx_tasks_priority ON tasks(household_id, priority, status);

    projects (
        id              TEXT PRIMARY KEY,
        household_id    TEXT NOT NULL,
        user_id         TEXT NOT NULL,
        title           TEXT NOT NULL,
        description     TEXT,
        scope           TEXT DEFAULT 'personal',
        status          TEXT DEFAULT 'active',  -- active|completed|on_hold
        target_date     TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );

    milestones (
        id              TEXT PRIMARY KEY,
        project_id      TEXT NOT NULL,          -- FK → projects.id
        household_id    TEXT NOT NULL,
        title           TEXT NOT NULL,
        order_index     INTEGER NOT NULL,        -- 1,2,3...
        status          TEXT DEFAULT 'pending',  -- pending|active|done
        target_date     TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );

    routines (
        id              TEXT PRIMARY KEY,
        household_id    TEXT NOT NULL,
        title           TEXT NOT NULL,
        scope           TEXT DEFAULT 'household', -- personal|household
        reset_time      TEXT DEFAULT '00:00',      -- HH:MM local time
        active          INTEGER DEFAULT 1,
        created_at      TEXT NOT NULL
    );

    routine_completions (
        id              TEXT PRIMARY KEY,
        routine_id      TEXT NOT NULL,            -- FK → routines.id
        household_id    TEXT NOT NULL,
        completed_by    TEXT NOT NULL,            -- user_id
        completion_date TEXT NOT NULL,            -- DATE YYYY-MM-DD
        completed_at    TEXT NOT NULL             -- full ISO8601 timestamp
    );

    CREATE UNIQUE INDEX idx_routine_completion_day
        ON routine_completions(routine_id, completion_date);

    conversation_state (
        user_id         TEXT PRIMARY KEY,
        last_task_id    TEXT,     -- task being captured/enriched
        capture_step    TEXT,     -- which question we're on
        context_json    TEXT,     -- arbitrary JSON for context
        updated_at      TEXT NOT NULL
    );

### 11.4 Task Status Flow

    INBOX → just captured, missing details
      Default for quick capture ("add task X")
      Mithra asks follow-up questions to enrich
      Shows in inbox view, not daily view

    ACTIVE → has enough info, ready to work on
      Set when user provides priority/deadline
      Shows in daily view based on priority

    SCHEDULED → has a calendar block (opt-in only)
      User explicitly asked Mithra to block time
      Linked to Google Calendar event via source_ref
      Time domain creates the block, Tasks domain tracks it

    WAITING → blocked on something external
      "Waiting for Amazon response"
      Hidden from daily view until user updates
      Auto-resurfaces after 7 days with follow-up

    SNOOZED → intentionally deferred
      Hidden until snoozed_until date
      Then auto-returns to ACTIVE
      "Snooze ceiling fan to next month"

    DONE → completed
      completed_at timestamp set
      Stays in history
      Shows in "completed this week" view

    CANCELLED → decided not to do
      Stays in history (not deleted)
      Different from DONE — intentional abandonment

### 11.5 Priority Model

    URGENT   — deadline ≤ 7 days OR explicitly set
               Mithra surfaces EVERY DAY
               Auto-promoted: NORMAL→URGENT when ≤7 days
               Example: ceiling fan warranty June 15

    HIGH     — important, want done this month
               Mithra surfaces WEEKLY
               Example: install dining room light

    NORMAL   — want to do at some point
               Mithra surfaces MONTHLY or on request
               Example: research NAS for home backup

    SOMEDAY  — nice to have, no commitment
               NEVER surfaced unless explicitly asked
               Example: learn woodworking technique X

    Auto-promotion rules:
      NORMAL with due_date ≤ 7 days → URGENT
      HIGH with due_date ≤ 3 days → URGENT
      Mithra sends Telegram: "Ceiling fan moved to urgent —
                              warranty expires in 5 days"

### 11.6 Capture Flow — Conversational Enrichment

    "add task fix ceiling fan"

    Step 1: task_create runs IMMEDIATELY
      title="Fix ceiling fan"
      status=INBOX, priority=NORMAL
      Task ID: tsk_abc123 stored in SQLite
      conversation_state: last_task_id=tsk_abc123,
                          capture_step="deadline"

    Step 2: Response with ONE question + options
      "Added: Fix ceiling fan ✓
       Any deadline? [This week] [This month] [Custom] [Skip]"

    Step 3: User taps or types
      → task_update(tsk_abc123, due_date=...)
      → Next question: priority (if not inferred)
      → Then: notes/context (optional, user can skip)

    Step 4: User says "done" or "skip" or changes topic
      → conversation_state cleared
      → Task finalized in ACTIVE status

    KEY PRINCIPLE:
      Task exists from Step 1 even if user stops.
      Questions are enrichment, not prerequisites.
      Even an INBOX task is better than no task.
      Never require all fields before creating.

    RICH CAPTURE (single message):
      "add task return Amazon package deadline Friday urgent
       notes: QR code in email, drop off at UPS"
      → All fields extracted in one shot
      → No follow-up questions needed
      → status=ACTIVE immediately

### 11.7 Tool Set

13 tools. All use Pydantic args_schema. All return ToolResult.

    task_create          WRITE    autonomy=1 (SURFACE)
      Creates task immediately with available info.
      Stores last_task_id in conversation_state.
      Asks follow-up questions in response.
      Never waits for all fields before creating.

    task_list            READ     autonomy=3 (ACT_SILENT)
      Queries: today|inbox|overdue|urgent|household|personal
      period: today|week|month|all
      scope: personal|household|all

    task_complete        WRITE    autonomy=2 (ACT_TELL)
      Marks task done, sets completed_at.
      Low friction — executes and notifies, no confirm.
      "Mark ceiling fan done" → done immediately.

    task_update          WRITE    autonomy=1 (SURFACE)
      Updates: priority|due_date|status|notes|title|owner
      Used in capture flow and explicit updates.

    task_snooze          WRITE    autonomy=2 (ACT_TELL)
      Sets snoozed_until, status=SNOOZED.
      "Snooze ceiling fan to next Monday"

    task_delete          DESTRUCTIVE   always confirm
      Soft delete or hard delete.
      Always requires confirmation.

    task_suggest         READ     autonomy=3 (ACT_SILENT)
      "What should I work on today?"
      Returns top 3 suggestions based on:
      priority, deadline proximity, last activity.

    project_create       WRITE    autonomy=1 (SURFACE)
      Creates project + asks about milestones.
      "Add project backyard deck by July 4"

    project_update       WRITE    autonomy=1 (SURFACE)
      Updates project status, target date, description.

    milestone_complete   WRITE    autonomy=2 (ACT_TELL)
      Marks milestone done, updates project % complete.
      "Complete phase 1 of backyard deck"

    routine_create       WRITE    autonomy=1 (SURFACE)
      Creates routine with reset_time and scope.
      "Add daily routine mix baby formula for household"

    routine_complete     READ     autonomy=3 (ACT_SILENT)
      Marks today's routine completion for user.
      Also callable via HA tablet REST endpoint.
      "Mark baby formula done"

    routine_list         READ     autonomy=3 (ACT_SILENT)
      Shows today's routines with done/not-done state.
      "What routines are done today?"

### 11.8 Scope Classification

    classify_task_scope(title, user_text) → personal|household

    HOUSEHOLD KEYWORDS:
      household, family, both, we, us, everyone,
      kids, children, baby, shared,
      trash, recycling, garbage,
      rent, mortgage, utilities,
      school, pickup, dropoff,
      pest control, plumber, contractor,
      lawn, yard, garden (when shared)

    PERSONAL KEYWORDS (override household default for recurring):
      gym, workout, run, yoga, meditation,
      medication, therapy, doctor, dentist,
      commute, work, office

    DEFAULT: personal (safer — user can override)

### 11.9 Multi-Turn Capture State

    conversation_state table tracks active capture:

    When task_create fires:
      INSERT OR REPLACE INTO conversation_state
      (user_id, last_task_id, capture_step, updated_at)
      VALUES (uid, task_id, 'deadline', now)

    When task_update fires (from follow-up):
      Reads conversation_state to find which task
      Updates task, advances capture_step
      Clears state when capture complete

    Capture steps: deadline → priority → notes → done

    State cleared when:
      User says "done", "skip", "that's it"
      User changes topic (classify_intent → different domain)
      24 hours pass (stale state auto-cleared)

### 11.10 Proactive Surfacing

    DAILY (morning briefing, same time as calendar):
      "You have 2 urgent tasks:
       • Ceiling fan — warranty expires in 5 days
       • Return package — window closes tomorrow"

    DEADLINE ALERT (3 days before):
      "⚠️ Ceiling fan warranty expires in 3 days.
       Mark done or snooze?"

    WAITING FOLLOW-UP (after 7 days):
      "Any update on ceiling fan?
       Still waiting for Amazon? [Yes] [No — mark done]"

    WEEKLY INBOX SUMMARY (Sunday morning):
      "You have 8 inbox tasks unprocessed.
       Want to go through them? [Yes] [Later]"

    AUTO-PROMOTION ALERT:
      "Ceiling fan moved to URGENT —
       deadline is in 5 days."

    Scheduler: APScheduler in healthmonitor container
    (same infrastructure as calendar briefing scheduler)

### 11.11 API Layer

    Business logic never returns HA-specific formats.
    API layer handles all format translation.
    This decouples domain from any display consumer.

    GET /api/v1/tasks/today
      Returns Mithra format:
      {tasks: [...], count: N, urgent_count: N}

    GET /api/v1/tasks/inbox
      Unprocessed tasks (status=INBOX)

    GET /api/v1/tasks/overdue
      Past due_date, not done

    GET /api/v1/dashboard/tasks
      DashboardData format (same as Time domain)
      summary: "3 urgent, 2 overdue"
      items: today's active tasks
      alerts: expiring deadlines
      quick_actions: add task, show inbox

    GET /api/v1/routines/today
      Today's routines with completion state
      Both personal and household for user
      {routines: [{id, title, done, done_by, done_at}]}

    POST /api/v1/routines/{id}/complete
      Mark routine done via HTTP (HA tablet tap)
      No Telegram needed
      Body: {user_id: "usr_ba6d9199"}

    ONE-WAY DOOR: no HA-specific format in domain tools.
    /api/v1/routines/today is Mithra's format.
    If HA needs different format, API adapter translates.

### 11.12 Time vs Tasks Boundary (reference to 10.15)

    See section 10.15 for complete boundary rules.
    Summary:
      Has specific clock time → Time domain
      No clock time → Tasks domain
      Bridge (calendar block for task) → opt-in only

### 11.13 Gmail Integration — Phase 2

#### Why Gmail
    Gmail is a source that auto-generates tasks:
      Return labels     → "Return [item] by [date]"
      Flight bookings   → Travel domain (Phase 3)
      Package shipping  → Shopping domain (Phase 4)
      Receipts          → Money domain (Phase 3)

    For Tasks domain specifically:
      Return labels → task with deadline
      Any actionable email user flags → task

#### Sync Method
    NOT polling every 30 seconds (exhausts API quota).
    NOT Google Cloud Pub/Sub (requires GCP, overkill
    for self-hosted household).

    RECOMMENDED (per Google docs for personal use):
    historyId incremental sync — same pattern as
    Google Calendar syncToken.

      FIRST RUN:
        GET /gmail/v1/users/me/messages (full sync)
        Extract actionable emails
        Store historyId in household_config:
          key: "gmail_history_id_{user_id}"

      EVERY 15 MINUTES (scheduler):
        GET /gmail/v1/users/me/history?startHistoryId=xxx
        Get only new messages since last check
        Classify each new email via LLM
        Route actionable emails to correct domain
        Store new historyId

    API quota: ~200 calls/day. Well within free tier.

#### Email Classification
    LLM classifies each new email:
      "return_label"        → Tasks: create return task
      "flight_booking"      → Travel domain (Phase 3)
      "hotel_booking"       → Travel domain (Phase 3)
      "package_shipped"     → Shopping domain (Phase 4)
      "receipt"             → Money domain (Phase 3)
      "bill_statement"      → Money domain (Phase 3)
      "appointment_confirm" → Time domain (create event)
      "other"               → ignore

#### Return Task Auto-Creation
    Gmail email: "Your return label for [product]"

    Mithra extracts:
      Product name from subject/body
      Return deadline from email body (LLM extracts)
      Return method (UPS, FedEx, USPS)

    Creates task:
      title: "Return [product] to [retailer]"
      due_date: extracted deadline
      priority: URGENT if <7 days, HIGH if <30 days
      notes: "Return label in Gmail"
      source: "gmail"
      source_ref: email_message_id
      scope: personal

    Telegram notification:
      "📦 Return label found: [product]
       Created task — return by [date]. [View task]"

#### Gmail OAuth Scope
    Current scope: calendar only
    Gmail needs: gmail.readonly
    Added separately from Calendar auth
    User authorizes independently:
      "connect gmail" → OAuth flow
      Only read access, never write

#### Architecture
    mithra/integrations/gmail/
      gmail_client.py       → Gmail API wrapper
                               historyId sync
                               email body extraction
      email_classifier.py   → LLM classifies email type
      email_processor.py    → routes to domain handlers

    Scheduler (in healthmonitor or FastAPI background):
      Every 15 minutes per connected user
      Reads historyId from household_config
      Processes new emails
      Updates historyId

### 11.14 Implementation Order

    T0. Pre-implementation (done separately):
        INTENT_DOMAIN_MAP: task/todo/routine/project → tasks
        conversation_state table in SQLite
        source + source_ref columns in tasks table

    T1. Schema — all 5 tables + indexes
        tasks, projects, milestones,
        routines, routine_completions
        Migration safe for existing installs

    T2. classify_task_scope() + classify_task_priority()
        Keyword heuristics
        Auto-promotion logic

    T3. Core task tools (tested first)
        task_create (with conversation_state)
        task_list (today/inbox/overdue/urgent)
        task_complete
        task_update (used in capture flow)
        task_snooze

    T4. Project + milestone tools
        project_create (asks about milestones)
        project_update
        milestone_complete

    T5. Routine tools
        routine_create
        routine_complete
        routine_list

    T6. task_delete + task_suggest

    T7. TasksDomain class implementing Domain ABC
        get_tools(context)
        get_dashboard_data()
        get_event_subscriptions()

    T8. Register TasksDomain in bootstrap

    T9. API routes
        GET /api/v1/tasks/today
        GET /api/v1/tasks/inbox
        GET /api/v1/tasks/overdue
        GET /api/v1/dashboard/tasks
        GET /api/v1/routines/today
        POST /api/v1/routines/{id}/complete

    T10. DoD verification (all manual tests)

    T11. Gmail integration (Phase 2 — separate sprint)
         gmail_client.py + email_classifier.py
         gmail.readonly OAuth scope
         Return task auto-creation
         historyId sync scheduler

### 11.15 Alternatives Considered

    Google Tasks as backend (rejected):
      No task sharing between accounts.
      Household tasks require sharing — breaks immediately.
      No priorities, projects, milestones, or routines.
      Source of truth must be Mithra.

    Vikunja self-hosted (rejected):
      Designed for software teams, not households.
      Separate UI conflicts with "one interface" goal.
      Every feature we need would be custom anyway.
      SQLite in Mithra is simpler and fully controlled.

    Google Cloud Pub/Sub for Gmail (rejected for Phase 1):
      Requires GCP account, topic setup, service accounts.
      Overkill for 2-user household.
      historyId polling at 15-min intervals is sufficient.
      Revisit for product at scale.

    Calendar blocking automatic (rejected):
      Auto-blocking creates calendar noise.
      User should decide when to work on a task.
      Bridge is always opt-in and user-initiated.
      See section 10.15.

---

## 12. Response Message Standards

These apply to ALL domain tools. Enforce in every tool, every domain.
New domains inherit these rules automatically.

### 12.1 Success Messages

Every success message follows this template:
  ✓ {Title} — {Key Detail}

Rules:
  - Start with ✓ for all confirmed actions
  - No quotes around titles
  - Use — (em dash + spaces) to separate title from detail
  - One line where possible; second line only for grouped items
  - No verbose system text ("Added to Google Calendar", "Done successfully")
  - No internal IDs, field names, filter labels, or tool names in output
  - Scope emoji (🏠) always at end of line, never in the middle

Examples:
  ✓ Dentist — Tuesday June 2, 3pm
  ✓ Trash Reminder — every Thursday 8pm 🏠
  ✓ Fix ceiling fan — removed
  ✓ Buy Materials — done (2/3 milestones)

### 12.2 Empty State Messages

Never expose internal filter names, query parameters, or field names.

  Bad:  "No tasks for filter 'today'."
  Bad:  "No events matching 'urgent'."
  Good: "You're all clear for today! 🎉"
  Good: "No urgent tasks."
  Good: "Inbox is empty."

Per-filter empty states for task_list:
  today:     "You're all clear for today! 🎉"
  inbox:     "Inbox is empty."
  overdue:   "Nothing overdue. ✓"
  urgent:    "No urgent tasks."
  household: "No household tasks."
  done:      "No completed tasks yet."
  waiting:   "No waiting tasks."
  all:       "No tasks found."
  personal:  "No personal tasks."

### 12.3 Confirmation Messages

When asking for confirmation (WRITE/DESTRUCTIVE tier):
  - Plain English, no JSON, no IDs
  - Start with the action verb: "Add", "Delete", "Move"
  - Include the title and key detail only

  Bad:  "Confirm: calendar_create_event {'title': 'Dentist', ...}"
  Good: "Add Dentist to Tuesday June 2 at 3pm?"
  Good: "Delete Fix ceiling fan?"

### 12.4 Scope Emoji

  Household scope:  append 🏠 at end of line
  Personal scope:   no emoji (personal is the default, unmarked)
  Always at end of line, after all other content

### 12.5 List Headers

Headers use Title Case labels, not internal field values.
Count format: "N tasks" (never "N task(s)").

  Bad:  "3 task(s) — today:"
  Good: "Today — 3 tasks:"
  Good: "Inbox — 1 task:"

---

## 13. Design Principles

These apply to ALL future decisions.

1. PRIVACY FIRST
   Health domain: local only, never cloud. Ever.
   PermissionTier.DESTRUCTIVE: always confirm, no override.
   Health domain tools: never in non-health domain context.

2. ABSTRACTION BOUNDARIES
   Domain code never imports other domain code.
   Domains communicate only via events and DomainContext.
   Gateway never knows about tool internals.
   Tools are pure functions relative to DomainContext.

3. PRODUCT READY FROM DAY ONE
   household_id scopes everything.
   domain_id + version enables safe product updates.
   Pydantic args_schema = MCP-ready JSON Schema.
   DomainContext.request_id = A2A task_id.

4. NO TACTICAL HOTFIXES
   If a fix touches more than one domain, stop.
   Redesign before implementing.
   Update ARCHITECTURE_DECISIONS.md before touching code.

5. TEST BEFORE SHIP
   All domain tools have unit tests.
   Tests use mock DomainContext injection.
   No domain ships without passing full test suite.
   Current baseline: 834 tests.

6. DOCUMENT THE WHY
   Every ADR records alternatives considered.
   Tool descriptions must explain WHEN NOT to use.
   Version changes must document what broke.

7. START SIMPLE, EXTEND DELIBERATELY
   Single agent + domain events now.
   Supervisor pattern when 3+ domains needed.
   MCP wrapping when third-party providers needed.
   A2A when cross-vendor agent coordination needed.
   Never build for scale you don't yet have.

8. INDUSTRY ALIGNMENT
   Tool contracts follow Claude Code patterns.
   Tool schemas follow MCP/A2A JSON Schema standard.
   Pydantic args_schema = zero-effort MCP compatibility.
   LangChain + LangGraph = industry standard execution layer.

---

## 14. Mithra Event System (MES)

### 14.0 Status and Context

Status: DESIGNED — not yet implemented.
Implementation begins after Tasks domain is stable.
This ADR locks all contracts before any code is written.

Context: as Mithra grows beyond the first two domains, two
distinct event flows have emerged that require different
routing, durability, and isolation guarantees:

1. External signals arrive from Gmail, Home Assistant sensors,
   Telegram webhooks, and future integrations. These are raw
   inbound data that may or may not produce domain actions.

2. Internal side effects flow between domains after a tool
   executes and the user confirms. These are DomainEvents —
   they carry semantic meaning and trigger downstream domain
   reactions.

Mixing these two flows in one mechanism creates coupling.
MES separates them clearly with different contracts, topics,
and routing rules.

---

### 14.1 The Core Distinction

    InboundEvent — external signal, no domain meaning yet
      Source: Gmail, HA sensor, Telegram webhook, scheduler
      Flow:   external system → InboundAdapter → EventBus
              → routing layer → classify → domain tool
      Example: email arrives with Amazon receipt
               → inbound.gmail.email_received
               → email_classifier determines domain
               → tasks domain creates return_label task

    DomainEvent — internal side effect, semantic meaning
      Source: domain tool after successful execution
      Flow:   tool.run() → ToolResult.events → EventBus
              → AutononomyEngine confirms → handlers fire
      Example: calendar_create_event succeeds
               → domain.time.appointment_created
               → MoneyDomain records expense if cost present
               → only after user confirmation

    ONE-WAY DOOR: InboundEvents never fire DomainEvents directly.
    The routing layer classifies and calls domain tools.
    DomainEvents only fire AFTER tool execution AND confirmation.
    This enforces the autonomy confirmation contract from section 9.10.

---

### 14.2 The Two-Way Door — EventBus Migration Path

EventBus is an abstract interface. Swapping the backend
requires zero changes to publishers or handlers.

    Phase 0 (now): AsyncioSQLiteEventBus
      Python asyncio queues for in-process delivery
      SQLite events table for durability + replay
      Sufficient for single-process, 2-user household
      Zero new Docker services needed

    Phase 2 (when needed): RedisEventBus
      Redis Streams for reliable delivery
      Requires new Redis Docker service
      Add when: second household instance, or
                Gmail polling needs background worker

    Phase 3 (product): KafkaEventBus
      Apache Kafka for multi-tenant, high-throughput
      Trigger: 1000+ households, or enterprise integrations

    Migration: swap one class in bootstrap.py
               All publishers and handlers unchanged

---

### 14.3 Contracts

#### BaseEvent

    @dataclass
    class BaseEvent:
        event_id:        str          # "evt_" + uuid hex, globally unique
        topic:           str          # "inbound.gmail.email_received"
        household_id:    str          # scope — never crosses boundaries
        user_id:         str          # "person_a" | "person_b" | "household"
        timestamp:       str          # ISO8601 UTC
        causation_id:    str | None   # event_id of parent event
        causation_depth: int          # 0 = original, max 5

    ONE-WAY DOOR: event_id is globally unique and immutable.
    Used for idempotency. Never regenerate on retry.

#### InboundEvent

    @dataclass
    class InboundEvent(BaseEvent):
        source:     str     # "gmail" | "ha_sensor" | "scheduler"
        raw_data:   dict    # original payload, unmodified
        classified: bool    # False until routing layer processes

    Topics: inbound.{source}.{signal_type}
      inbound.gmail.email_received
      inbound.ha_sensor.motion_detected
      inbound.scheduler.morning_briefing_due

#### DomainEvent

    @dataclass
    class DomainEvent(BaseEvent):
        event_type:    str     # "appointment_created"
        source_domain: str     # "time"
        payload:       dict    # semantic, domain-specific
        request_id:    str     # links to originating AgentRequest

    Topics: domain.{source_domain}.{event_type}
      domain.time.appointment_created
      domain.tasks.task_completed
      domain.money.expense_recorded

    This extends the existing DomainEvent from section 9.10
    with BaseEvent fields. Migration: add event_id, topic,
    causation_id, causation_depth to existing DomainEvent dataclass.

#### EventBus

    class EventBus(ABC):
        async def publish(
            self,
            event: BaseEvent,
        ) -> None:
            """Publish event to all subscribers of its topic."""

        def subscribe(
            self,
            topic_pattern: str,    # supports * wildcard
            handler: "EventHandler",
        ) -> None:
            """Register handler for topic pattern.
            Wildcard: 'domain.time.*' matches all time events."""

        def unsubscribe(
            self,
            topic_pattern: str,
            handler: "EventHandler",
        ) -> None: ...

        async def replay(
            self,
            topic_pattern: str,
            since: str,            # ISO8601 timestamp
        ) -> None:
            """Replay unprocessed events from persistence store.
            Used on restart to recover missed events."""

        def health(self) -> dict: ...

#### InboundAdapter

    class InboundAdapter(ABC):
        """Connects an external source to the EventBus."""

        @property
        @abstractmethod
        def source(self) -> str:
            """Identifies the external source e.g. 'gmail'"""

        @abstractmethod
        async def start(self) -> None:
            """Begin polling or listening for inbound events."""

        @abstractmethod
        async def stop(self) -> None:
            """Stop polling cleanly."""

        @abstractmethod
        async def _normalise(self, raw: dict) -> InboundEvent:
            """Convert raw source payload to InboundEvent."""

        def health(self) -> dict: ...

#### EventHandler

    class EventHandler(ABC):
        """Processes one published event."""

        @property
        @abstractmethod
        def topic_patterns(self) -> list[str]:
            """Topics this handler subscribes to."""

        @abstractmethod
        async def handle(
            self,
            event: BaseEvent,
            context: "DomainContext",
        ) -> None:
            """Process the event. Never raises — errors logged."""

---

### 14.4 Topic Naming Convention

All topics use dot-notation namespaces. Four top-level namespaces:

    inbound.*       External signals not yet classified
      inbound.gmail.email_received
      inbound.ha_sensor.{sensor_id}
      inbound.scheduler.{job_name}
      inbound.webhook.{source}

    domain.*        Confirmed domain actions (side effects)
      domain.{source_domain}.{event_type}
      domain.time.appointment_created
      domain.tasks.task_completed
      domain.tasks.task_overdue
      domain.money.expense_recorded
      domain.people.birthday_approaching

    notification.*  Outbound delivery events (for ActionLog)
      notification.telegram.sent
      notification.telegram.failed
      notification.whatsapp.sent
      notification.tts.spoken

    system.*        Infrastructure events
      system.startup.complete
      system.health.degraded
      system.backup.complete
      system.error.dead_letter

    ONE-WAY DOOR: top-level namespace is fixed.
    Adding a new source = new inbound.{source}.* subtopic.
    Never create ad-hoc topics outside these four namespaces.

---

### 14.5 Persistence — events Table

AsyncioSQLiteEventBus writes every event to SQLite
before delivery. Events survive restarts and enable replay.

    CREATE TABLE events (
        event_id        TEXT PRIMARY KEY,
        topic           TEXT NOT NULL,
        household_id    TEXT NOT NULL,
        user_id         TEXT NOT NULL,
        timestamp       TEXT NOT NULL,      -- ISO8601 UTC
        causation_id    TEXT,               -- nullable
        causation_depth INTEGER NOT NULL DEFAULT 0,
        payload_json    TEXT NOT NULL,      -- full event JSON
        processed       INTEGER NOT NULL DEFAULT 0,  -- 0|1
        processed_at    TEXT,               -- ISO8601 when processed
        error_count     INTEGER NOT NULL DEFAULT 0,
        last_error      TEXT                -- last exception message
    );

    CREATE INDEX idx_events_topic ON events(topic, processed);
    CREATE INDEX idx_events_household ON events(household_id, timestamp);
    CREATE INDEX idx_events_causation ON events(causation_id);

    Retention: events older than 30 days marked for deletion.
    Health domain events: purged after 7 days (privacy).
    system.* events: kept 90 days for audit.

---

### 14.6 Idempotency Contract

Every handler MUST be idempotent — safe to call twice
with the same event_id. AsyncioSQLiteEventBus marks
events processed=1 after all handlers complete. On
restart, only processed=0 events are replayed.

    Idempotency implementation per handler type:
      SQLite writes:   INSERT OR IGNORE with event_id as dedup key
      Calendar writes: check_exists(event_id) before Google API call
      Telegram sends:  message_id stored; skip if already sent
      Task creation:   source_ref=event_id, INSERT OR IGNORE

    EventBus enforces at-least-once delivery.
    Handlers enforce exactly-once semantics via idempotency.

---

### 14.7 Error Isolation

Handler failures must never affect other handlers or the
event publisher. Each handler runs in an isolated try/except.

    Delivery order per event:
      1. Persist event (SQLite write) — if this fails, publish() raises
      2. For each handler[i] subscribed to topic:
         try:
             await handler[i].handle(event, context)
             _mark_handler_success(event_id, handler[i].name)
         except Exception as exc:
             _logger.error("handler %s failed for %s",
                           handler[i].name, event.event_id,
                           exc_info=True)
             event.error_count += 1
             event.last_error = str(exc)
             # CONTINUE to handler[i+1] — never propagate
      3. If ALL handlers succeed → processed=1
         If ANY handler fails → processed=0, error_count incremented
         Retry on next restart or manual replay

    Dead letter: events with error_count >= 3
      Written to StorageAdapter domain="observability"
      Surfaced on health dashboard
      Admin notified via Telegram

---

### 14.8 Causation Chain Protection

Events that trigger other events create causation chains.
Unbounded chains cause infinite loops. Max depth: 5.

    Each BaseEvent carries:
      causation_id:    event_id of the parent event (or None)
      causation_depth: parent.causation_depth + 1

    EventBus.publish() enforcement:
      if event.causation_depth > 5:
          _logger.error("causation_depth exceeded for %s",
                        event.event_id)
          # write to dead letter, do NOT deliver
          return

    Example chain (depth 0→3):
      Gmail email arrives (depth 0, causation_id=None)
        → email_classifier → tasks.task_create (depth 1)
          → domain.tasks.task_created (depth 2)
            → notification.telegram.sent (depth 3)

    Legitimate chains rarely exceed 3 levels.
    Depth 5 always indicates a cycle or design error.

---

### 14.9 Fan-Out

One event may trigger multiple handlers. EventBus delivers
to all subscribers matching the topic pattern.

    Example: domain.time.appointment_created published
      → MoneyDomainHandler (checks for cost, records expense)
      → TasksDomainHandler (creates pre-appointment task)
      → NotificationHandler (sends calendar confirmation)
    All three receive the same event independently.

    Topic wildcards:
      "domain.time.*"     matches any time domain event
      "domain.*.*"        matches any domain event
      "inbound.gmail.*"   matches all Gmail signals
      Exact match takes priority over wildcard.

    Handler registration order: deterministic (registration order).
    Handlers must not depend on each other's execution order.

---

### 14.10 Alternatives Considered

#### Celery + Redis (task queue)
    Rejected: Celery is a task queue, not an event bus.
    Push model only — no topic routing, no fan-out patterns.
    Adds Redis + Celery worker containers (2 new services).
    Overhead unwarranted for 2-user household scale.

#### Kafka from day one
    Rejected: Kafka requires ZooKeeper or KRaft,
    minimum 3 containers, complex admin.
    Unnecessary for single-household, single-process use.
    EventBus abstraction makes Kafka a later swap.

#### Direct domain function calls (no bus)
    Rejected: this is the status quo. Creates tight coupling.
    Domain A imports Domain B to call handlers directly.
    Violates domain isolation principle (section 13.2).
    Testing requires all domains present simultaneously.

#### Redis Pub/Sub
    Rejected for Phase 0: requires Redis container,
    no persistence (fire-and-forget), no replay after restart.
    Redis Streams (not Pub/Sub) is the Phase 2 target.

#### asyncio.Queue per topic (no SQLite)
    Rejected: events lost on restart or crash.
    Replay on startup is a hard requirement — Gmail emails
    arrive while the container is restarting.

---

### 14.11 Implementation Order

    Phase 0 — Contracts (implement before next domain):
      MES-00  BaseEvent, InboundEvent, DomainEvent dataclasses
              mithra/events/base.py
      MES-01  EventBus ABC
              mithra/events/bus.py
      MES-02  AsyncioSQLiteEventBus implementation
              mithra/events/asyncio_sqlite_bus.py
      MES-03  events table DDL in LocalStorageAdapter.__init__
      MES-04  InboundAdapter ABC
              mithra/events/inbound.py
      MES-05  EventHandler ABC
              mithra/events/handler.py
      MES-06  Wire EventBus into bootstrap.py
              build_state() injects bus into DomainContext
      MES-07  Unit tests: publish/subscribe, idempotency,
              causation depth guard, error isolation, replay
              35+ tests

    Phase 1 — Notification Refactor (after People domain):
      MES-08  notification.* topic wired from OutputAdapters
      MES-09  DomainEvent emission from TasksDomain tools
      MES-10  DomainEvent emission from TimeDomain tools
      MES-11  Cross-domain: tasks → time
              task_overdue → calendar_create_event (time-block)
      MES-12  Cross-domain: time → tasks
              appointment_created → task_create (prep task)
      MES-13  Integration tests: event published → handler called

    Phase 2 — Gmail Integration:
      MES-14  GmailInboundAdapter
              mithra/events/adapters/gmail_inbound.py
              historyId incremental polling, 15 min interval
              Publishes inbound.gmail.email_received
      MES-15  EmailClassifier
              mithra/events/adapters/email_classifier.py
              LLM: return_label | flight_booking | receipt | other
      MES-16  inbound.gmail.* router → classify → domain tool
              return_label → task_create (source_ref=message_id)
      MES-17  Return task auto-creation from email
              Telegram notification with [View task] button
      MES-18  DoD: Amazon return email → task created automatically
              historyId stored in household_config

    Phase 3 — Deferred handlers:
      MES-19  Travel domain: FlightReservation JSON-LD handler
      MES-20  Travel domain: auto-create calendar events
              from inbound.gmail.flight_booking
      MES-21  Money domain: expense_recorded fan-out handler
      MES-22  HA sensor handler: inbound.ha_sensor.* → domain tools
      MES-23  RedisEventBus adapter (swap when scale needs it)

---

## 15. Dependency Management Standards

### 15.1 Three Categories of Dependencies

    CATEGORY 1 — Request-scoped (DomainContext)
      What belongs here:
        user_id, household_id, household_timezone,
        local_db_path, oauth_manager, member_registry,
        request_id, channel, language
      How accessed: ctx.field_name in tool _run()
      Lifecycle: built fresh per request
      Why: these differ per user/request

    CATEGORY 2 — Process-scoped singletons
      What belongs here:
        event_bus, llm_router client, vector_store client,
        telegram bot client, file_router
      How accessed: from mithra.X import get_X
      Lifecycle: built once in bootstrap, never change
      Why: same instance for all requests, all users

      Pattern:
        # In the module that owns the singleton:
        _instance: SomeClass | None = None

        def get_instance() -> SomeClass | None:
            return _instance

        def set_instance(obj: SomeClass) -> None:
            global _instance
            _instance = obj

        # In bootstrap.py:
        set_instance(build_the_thing())

        # Anywhere in codebase:
        obj = get_instance()
        if obj:
            obj.do_thing()

    CATEGORY 3 — Configuration (household_config dict)
      What belongs here:
        timezone, feature flags, integration settings
      How accessed: household_config.get("key")
      Lifecycle: loaded at startup, refreshed on change
      Why: shared read-only data

---

### 15.2 The Patch Anti-Pattern (FORBIDDEN)

    Never do this:
      component = build_component()
      dependency = build_dependency()
      component._dependency = dependency  ← FORBIDDEN

    Why it fails:
      1. Violates Dependency Inversion Principle
      2. Construction order becomes implicit contract
      3. New code paths silently get None
      4. Tests cannot easily mock individual dependencies
      5. Adding new components = updating bootstrap +
         remembering to patch = easy to miss

    Instead:
      If Category 1: add to DomainContext constructor
      If Category 2: use module-level singleton pattern
      If Category 3: read from household_config dict

---

### 15.3 bootstrap.py Responsibility

    bootstrap.py should ONLY:
      1. Build singletons in dependency order
      2. Call set_X() for each singleton
      3. Build initial state dict
      4. Return state

    bootstrap.py should NOT:
      1. Patch attributes onto already-constructed objects
      2. Know which components need which dependencies
      3. Wire request-scoped dependencies (that is DomainContext's job)

---

### 15.4 DomainContext Rules

    Add a field to DomainContext ONLY if:
      - It varies per user OR per request
      - It comes from the request or user's stored state

    Do NOT add to DomainContext:
      - Process-level singletons (use get_X() instead)
      - Configuration that does not vary per user
      - Clients that are the same for all users

---

### 15.5 Applying to Existing Code

    AUDIT — DomainContext fields:
      event_bus        → Category 2, removed from DomainContext
                         use get_event_bus() ✅ DONE
      file_router      → Category 2, candidate for singleton (future)
      local_db_path    → Category 1 ✅ (varies per household)
      oauth_manager    → Category 1 ✅ (user-specific tokens)
      member_registry  → Category 1 ✅ (household-specific)

    AUDIT — bootstrap.py patch blocks to remove:
      agent._event_bus        ← removed ✅ DONE
      agent._file_router      ← Category 2, future cleanup
      agent._member_registry  ← Category 1, already in ctx ✅
      agent._local_db_path    ← Category 1, already in ctx ✅

---

## 16. Dashboard Aggregation Layer (DAL)

### 16.0 Status and Context

    Status:  ACCEPTED
    Date:    2026-06-22
    Context:
      HA setup is the next milestone. Today only
      GET /api/v1/dashboard/tasks exists — a single,
      Tasks-domain-specific endpoint. Time domain has
      no dashboard data method at all. Building HA
      widgets directly against one domain's bespoke
      JSON shape means every future domain (People,
      Shopping, Travel) requires hand-building new
      HA config from scratch, and HA must be told
      about each new domain explicitly.

      This mirrors the exact problem MES solved for
      events (Section 14): without a standard contract,
      every new integration is bespoke and coupling grows
      with every domain added.

      Household tablet and personal mobile also need
      different views of the same data (household-only
      vs household+personal), which must be a query
      parameter, not separate hardcoded endpoints.

### 16.1 Decision

    Every domain implements get_dashboard_data() against
    a shared contract. A single aggregator endpoint loops
    all registered domains and returns combined JSON.
    Home Assistant (and any future presentation layer —
    mobile app, voice display) renders generically against
    this contract. Adding a domain later requires zero
    changes to HA or the aggregator.

    Scope for this round: Tasks (refactor existing) +
    Time (new) domains. Tier 1 (glance tiles) and Tier 2
    (tap-to-complete lists) only. Tier 3 (full custom
    per-domain views, e.g. project status, calendar week
    view) is explicitly deferred (YAGNI) until a concrete
    need arises — same discipline as MES Phase 3.

### 16.2 Contracts (LOCKED — additive only)

    @dataclass
    class DashboardItem:
        id:        str
        domain:    str    # "tasks" | "time" | future domains
        title:     str
        subtitle:  str = ""
        icon:      str = ""   # mdi: icon name
        priority:  str = "normal"  # "high" | "normal"
        scope:     str = "household"
                   # Defaults to "household" if a domain
                   # has no personal/household concept
                   # (e.g. a future People contact).
        action:    str = ""
                   # Name of the existing REST action this
                   # item supports, e.g. "task_complete",
                   # "routine_complete". Empty = read-only.

    @dataclass
    class DashboardAlert:
        type:      str
        message:   str
        severity:  str = "low"  # "low" | "medium" | "high"

    @dataclass
    class DashboardData:
        domain:    str
        summary:   str
        items:     list[DashboardItem]
        alerts:    list[DashboardAlert]
        error:     str | None = None
                   # Set if this domain's query failed.
                   # Aggregator includes the domain with
                   # error set rather than dropping it or
                   # failing the whole response.

    Domain ABC gains (additive to existing get_tools()):

        def get_dashboard_data(
            self,
            context: DomainContext,
            scope_filter: str = "household",
                          # "household" | "personal" | "all"
            viewer_user_id: str = "",
        ) -> DashboardData:
            ...

### 16.3 Aggregator Endpoint

    GET /api/v1/dashboard?scope=household
    GET /api/v1/dashboard?scope=personal&user_id=usr_xxx

    Response:
        {
          "generated_at": "2026-06-22T...",
          "scope": "household",
          "domains": [DashboardData, DashboardData, ...],
          "errors": []   // list of domain names that failed
        }

    Behaviour:
      - Loops every domain in the domain registry
      - Calls get_dashboard_data() in try/except per domain
      - A domain that throws is included with error set,
        NEVER causes a 500 for the whole response (WC2)
      - Existing GET /api/v1/dashboard/tasks is KEPT
        unchanged for direct domain testing/debugging —
        the aggregator is the new endpoint HA actually polls

### 16.4 Tier 2 — Tap-to-Complete Requires New Task Endpoint

    Routines already have:
        POST /api/v1/routines/{routine_id}/complete
        (unauthenticated for HA, idempotent)

    Tasks do NOT have an equivalent. Add:
        POST /api/v1/tasks/{task_id}/complete
        (unauthenticated for HA tablet/mobile via
        Cloudflare Access, idempotent — same pattern
        as routine completion)

    This must call the SAME underlying completion logic
    Telegram's task_complete tool uses (direct SQLite
    UPDATE status='done'), not go through the LLM pipeline.
    HA taps must never touch classify_intent or tool
    selection — same principle as routine completion
    (see LESSONS_LEARNED L-01 through L-08 for why
    LLM-on-critical-path is avoided wherever possible).

    Idempotency: tapping an already-completed task returns
    a friendly "already done" response, not an error
    (mirrors routine_complete's IntegrityError handling).

### 16.5 Cloudflare — Unified Public Access for HA's UI

    Decision: HA's own web UI (port 8123) is exposed via
    a single new Cloudflare tunnel hostname, used by BOTH
    the household tablet and personal mobile. No LAN IP
    is referenced anywhere in the setup.

    Why unified rather than LAN-direct for the tablet:
      The Windows host's LAN IP is not stable (observed
      changing across DHCP renewals during the WSL2
      migration). A wall-mounted kiosk tablet hardcoded
      to a LAN IP would silently go blank on the next
      DHCP renewal — a worse failure mode on a bolted-to-
      the-wall device than on a laptop with SSH access.
      Telegram, calendar sync, and LLM calls already
      depend on internet connectivity; adding the tablet
      to that list is consistency, not a new single point
      of failure.

    Architecture:
      home.mithra-app.com → cloudflared → homeassistant:8123
      (new hostname, same cloudflared container already
      routing assistant.mithra-app.com → api-gateway:8000 —
      ADD a route, do not modify the existing one)

      Cloudflare Access policy on home.mithra-app.com:
        Allow — login with Google/email, your identity only
        Session duration: long (kiosk-friendly — log in once,
        Fully Kiosk Browser session persists)
        NO service token needed — nothing machine-to-machine
        crosses this hostname. HA's REST sensor talking to
        Mithra's API stays entirely on the internal Docker
        network and is unaffected by any of this.

    HA-side reverse proxy requirement:
      Home Assistant must trust Cloudflare's proxy headers.
      Add to HA's configuration.yaml:
        http:
          use_x_forwarded_for: true
          trusted_proxies:
            - <cloudflared container's docker network subnet>
      Without this, HA may reject or mis-log requests
      arriving via the tunnel.

### 16.6 Worst Cases Addressed

    WC1  New domain added later            → generic ABC method,
                                              zero HA changes needed
    WC2  One domain's query throws         → try/except per domain,
                                              partial result + error field
    WC3  Cloudflare Access blocks REST      → service token for
         sensor (no interactive login)        machine traffic
    WC4  Schema changes break old sensors   → additive-only payload
    WC5  Domain has no scope concept        → defaults to "household"
    WC6  Tablet polls too aggressively      → documented scan_interval
                                              guidance (30-60s, not secs)
    WC7  api-gateway down → all tiles fail  → accepted, matches
                                              existing health-check pattern
    WC8  Stale tile after tap (cache lag)   → accepted latency, governed
                                              by HA's own scan_interval
    WC9  Personal dashboard needs merged    → scope is a query param,
         household+personal view              not hardcoded per device
    WC10 Generic action dispatch            → DEFERRED (YAGNI). Tier 2
         abstraction with no real need         uses existing per-domain
                                              REST actions (routine
                                              complete, task complete)

### 16.7 Alternatives Considered

    Per-domain bespoke HA sensors (status quo) — rejected:
      Already proven unsustainable; every new domain
      requires new HA YAML written by hand against a
      one-off JSON shape.

    Generic action dispatch endpoint now
    (POST /dashboard/action {domain, action, params}) — rejected:
      Zero domains need more than 2 action types today
      (routine_complete, task_complete). Building a
      dispatch abstraction for a problem that doesn't
      exist yet is the same mistake MES-11/12 almost made.

    Full Tier 3 custom views now (project status card,
    calendar week view) — rejected:
      No concrete need yet. Defer until a domain actually
      requires richer-than-list-of-items presentation.

### 16.8 Implementation Order

    HA-00  ADR section 16 committed ← this commit
    HA-01  DashboardItem/Alert/Data contracts
           Domain.get_dashboard_data() ABC method
    HA-02  Tasks domain: refactor existing
           get_dashboard_data() to match contract
    HA-03  Time domain: new get_dashboard_data()
           (next event tile + today's events list)
    HA-04  Aggregator: GET /api/v1/dashboard endpoint
    HA-05  POST /api/v1/tasks/{task_id}/complete
           (idempotent, unauthenticated for HA, mirrors
           routine_complete pattern)
    HA-06  Enable homeassistant container
           (compose.yaml profile adjustment)
    HA-07  Cloudflare Access service token + policy
           (manual Cloudflare dashboard steps)
    HA-08  HA REST sensors (Tier 1 tiles)
    HA-09  HA Lovelace dashboard (Tier 1 + Tier 2)
    HA-10  Manual DoD verification on personal tablet
           and personal mobile via Cloudflare
