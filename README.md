# Insight Vault

**A private, configurable workspace that turns interview transcripts and support tickets into evidence-backed insights.**

- Highlight and tag quotes in transcripts, then group them into insights that stay linked to their source.
- Ask an LLM to suggest insights. Every suggestion is validated against your real quotes.
- See which issues were diagnosed in the last 7, 14, 30, 60 or 90 days and how support agents fixed them.
- Turn that report into a document and send it through an approval workflow (a mock: nothing is emailed).
- Sign in with Google, LinkedIn, Microsoft or your own OpenID Connect provider, with roles and an audit log.
- Configure almost everything from one `.env` file. The LLM is optional: without one, everything still works using rules.

| Notes and highlights | AI-suggested insights | Support resolution report |
|---|---|---|
| ![Notes](docs/screenshots/notes-highlights.png) | ![Suggestions](docs/screenshots/ai-suggestions.png) | ![Support report](docs/screenshots/support-report.png) |

| Single sign-on | Admin settings |
|---|---|
| ![Login](docs/screenshots/sso-login.png) | ![Settings](docs/screenshots/settings.png) |

A complete example of the generated document, sent for approval, is in [`docs/sample-support-report.md`](docs/sample-support-report.md).

## Quick start

You need **Python 3.10+** and **Node.js 18+**.

```bash
git clone https://github.com/bhuwanthada/insight-vault.git
cd insight-vault
npm run setup      # creates .venv, installs dependencies, and creates your .env from .env.example (once)
npm start          # starts the API and the UI, then opens http://localhost:3000
```

On Windows, after `npm run setup`, you can double-click `start.bat` instead. The first start builds the UI, which takes about a minute.

By default there is **no login and no LLM**, which suits one person on one machine. Edit `.env` to change that.

### Try it with the sample data

1. **Transcript:** click **+ Add note**, load `samples/interview-priya.txt`, then select text to highlight and tag it.
2. **Support report:** open **Support report**, click **Load sample tickets**, then change the time window.
3. **Document:** click **Generate document**, **Send for approval (mock)**, enter a reviewer name and click **Approve**.

## Configuration: the `.env` file

Copy of `.env.example`, created for you by `npm run setup`. It is git-ignored, so your keys never get committed. Real environment variables override it, and a wrong value stops startup with a message naming every problem.

**Every setting is documented inside [`.env.example`](.env.example).** The groups:

| Group | What you can customize |
|---|---|
| **LLM** | Provider, model, key, base URL, fallback models, a different model per task, temperature, tokens, timeout, retries, votes, concurrency, circuit breaker, daily token cap, personal-data masking, prompt files, output language |
| **Rules and reports** | Issue categories and fix patterns (YAML), report layout (Markdown template), rule confidence thresholds, the "last X days" choices and default |
| **Sign-in** | Mode (none, API keys, SSO), providers, who may sign in, roles, self-approval rule, session length |
| **Server and security** | Host, port, threads, JSON or text logs, log level, data file, upload limit, CORS origins, proxy trust, rate limits, audit retention |
| **Look and feel** | App name, accent color, default theme, which features are shown, date locale |

Administrators can see the active settings (secrets hidden) in the **Settings** tab.

## Choosing an LLM provider

Set `LLM_PROVIDER` and `LLM_MODEL` in `.env`:

| `LLM_PROVIDER` | Use it for | Extra install |
|---|---|---|
| `openrouter` | OpenRouter directly over HTTP. Any model on their list. | none |
| `openai_compatible` | Any OpenAI-style server: Ollama, vLLM, LM Studio, a company gateway. Needs `LLM_BASE_URL`. | none |
| `litellm` | Any model LiteLLM supports (100+ providers) | `npm run setup:litellm` |
| `none` | LLM off, rules only (the default) | none |

```bash
# .env: OpenRouter
LLM_PROVIDER=openrouter
LLM_MODEL=anthropic/claude-sonnet-4.5
LLM_API_KEY=your-openrouter-key
```

```bash
# .env: a local model through Ollama
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3.1:8b
```

More options:

- **Fallbacks:** `LLM_FALLBACKS=litellm:gpt-4o-mini,openai_compatible:llama3.1:8b` tries the next model when one fails. You can mix providers.
- **A model per task:** set `LLM_MODEL_CLASSIFY` to a cheap model and `LLM_MODEL_INSIGHTS` and `LLM_MODEL_SUMMARY` to a stronger one.
- **Spending guard:** `LLM_DAILY_TOKEN_CAP=200000` stops calls once the day's tokens are used.
- **Privacy:** `LLM_PII_MASKING=true` replaces e-mail addresses, phone numbers and card-like numbers before text leaves your machine.
- **Your own prompts:** put `classify.txt`, `insights.txt` or `summary.txt` in a folder and set `PROMPT_DIR`.

Admins can press **Test LLM connection** in the Settings tab. Your quotes and tickets are sent to the provider you choose, so pick one that fits your privacy needs.

## Single sign-on and roles

Set `AUTH_MODE=sso` in `.env`. Sign-in uses OpenID Connect (authorization code flow with PKCE), and sessions are signed, HTTP-only cookies.

**Access is denied by default.** Signing in with a Google account does not grant access: the e-mail address must be in `ALLOWED_EMAILS` or its domain in `ALLOWED_DOMAINS`. Settings are read at startup. After you edit `.env` and restart, role and access changes apply to people who are already signed in, with no need for them to sign in again.

| Role | Can do |
|---|---|
| `viewer` | Read everything |
| `editor` | Also add and change notes, highlights, insights, tickets and documents |
| `approver` | Also approve or reject documents |
| `admin` | Also see the Settings tab, audit log and test the LLM connection |

Assign roles with `ADMIN_EMAILS`, `APPROVER_EMAILS` and `EDITOR_EMAILS`; everyone else gets `DEFAULT_ROLE`. People cannot approve documents they created unless `ALLOW_SELF_APPROVAL=true`.

### Registering your sign-in provider

You must create an app with each provider you want and copy its client ID and secret into `.env`. For every provider, register this **redirect URI**: `<PUBLIC_URL>/api/auth/callback/<provider>`, for example `http://localhost:3000/api/auth/callback/google`.

| Provider | Where to register | `.env` keys | Notes |
|---|---|---|---|
| Google | Google Cloud console, "OAuth client ID", type Web application | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | |
| LinkedIn | LinkedIn developer portal. Add the **Sign In with LinkedIn using OpenID Connect** product | `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` | |
| Microsoft | Entra ID, "App registrations" | `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_TENANT` | The tenant must be **your own** tenant id or domain. `common`, `organizations` and `consumers` are rejected because they would let other tenants claim your e-mail addresses |
| Any OpenID Connect provider (Okta, Auth0, Keycloak, ...) | Your provider | `OIDC_NAME`, `OIDC_METADATA_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | The redirect URI ends in `/oidc` |

Example `.env`:

```bash
AUTH_MODE=sso
SECRET_KEY=<32+ random characters>    # python -c "import secrets; print(secrets.token_urlsafe(32))"
PUBLIC_URL=http://localhost:3000
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
ALLOWED_DOMAINS=yourcompany.com
ADMIN_EMAILS=you@yourcompany.com
COOKIE_SECURE=true                    # when served over https
```

GitHub sign-in is not included: it uses plain OAuth rather than OpenID Connect, which needs a different integration.

**Scripts and automation** can use `AUTH_MODE=apikey` (or add keys in SSO mode): `API_KEYS=ci:editor:<key>` and send `Authorization: Bearer <key>`.

## Architecture

```text
Browser ──► Next.js + React (frontend/, port 3000)
               │  /api/* proxied
               ▼
            Flask API served by Waitress (backend/, port 5001)
               ├── Sign-in, roles, rate limits, audit log
               ├── JSON file store      data/db.json (atomic writes, lock-protected)
               ├── Deterministic rules  keyword classifier, fix extractor, tag grouping (config/rules.yaml)
               └── LLM providers        OpenRouter, OpenAI-compatible, or LiteLLM (optional)
```

| Folder | What it contains |
|---|---|
| `backend/` | Python API, sign-in, analysis, LLM providers, caching and concurrency helpers |
| `frontend/` | Next.js app (React components in `src/components`) |
| `config/` | Editable issue rules (`rules.yaml`) and report layout (`report_template.md`) |
| `tests/`, `frontend/tests/`, `e2e/` | Python, React and real-browser tests |
| `samples/`, `docs/` | Fictional transcript and tickets; screenshots and the sample approval document |

## How it stays reliable

**Deterministic first, probabilistic when needed.** Support tickets are diagnosed by keyword rules. A clear match is decided instantly, for free, and the same way every time. Only ambiguous tickets go to the LLM. They are sampled several times and the majority answer wins. The share of agreeing votes is the confidence. The LLM's answer must be one of your configured categories, or it is ignored.

**Insights are validated.** LLM-suggested insights must cite quote ids that exist. Invented ids are dropped. If nothing valid remains, the app groups quotes that share a tag.

**Caching.** Trustworthy LLM results are cached (LRU with expiry), keyed by a hash of the input, the model and the prompt version. Fallback results are never cached, so a recovered LLM is used again straight away.

**Race conditions.**
- *Duplicate work:* identical requests that arrive together share one LLM call (single-flight).
- *Lost updates:* reads and writes of the data file share one lock and files are replaced atomically. A test runs four writers and three readers at once and checks that no update is lost.
- *Approvals:* a document's status changes by compare-and-set under the lock. If eight people approve and reject at the same moment, exactly one wins and the rest receive `409 Conflict`.

**Failure handling.** Each LLM call has a timeout, a concurrency limit, retries with backoff, a token budget, and a circuit breaker per model. Models are tried in order, and when all fail the app degrades to rules and says so in the UI.

**Operations.** `/api/health` (alive), `/api/ready` (data store usable), `/api/metrics` (Prometheus text), structured JSON logs with a request id on every line, rate limits, security headers, a CORS allow-list, and a cross-site request check for cookie sessions.

## The support report

Pick a time window and the report shows, for tickets resolved in that period:

- the **issue categories diagnosed**, with ticket counts, share and average time to resolve
- **how each was fixed**: the actions agents took (for example "Reset password / credentials") and the tools they used
- the busiest agents and how many diagnoses were low-confidence

Categories and fix patterns live in [`config/rules.yaml`](config/rules.yaml); copy it and set `RULES_FILE` to use your own. The document layout lives in [`config/report_template.md`](config/report_template.md) (`REPORT_TEMPLATE`). Documents move through `draft`, `pending approval`, `approved` or `rejected`. The approval is a **mock**: it records the request and decision, with the real person's name when sign-in is on, and sends nothing.

To ingest your own tickets, `POST` JSON to `/api/tickets` with `subject`, `resolution`, `createdAt`, `resolvedAt` (ISO-8601) and optionally `agent`, `tools_used` and `description`.

## API summary

| Method and path | Role | Purpose |
|---|---|---|
| `GET /api/health`, `/api/ready`, `/api/config/public` | none | Liveness, readiness, branding and feature flags |
| `GET /api/auth/providers`, `/api/auth/me`, `/api/auth/login/<provider>`, `POST /api/auth/logout` | none | Sign-in |
| `GET /api/state`, `/api/search?q=`, `/api/export`, `/api/notes/<id>`, `/api/report?days=` | viewer | Read data and reports |
| `POST /api/notes`, `/api/highlights`, `/api/insights`, `/api/insights/suggest`, `/api/tickets`, `/api/reports` and their `DELETE`/`PATCH` | editor | Change data |
| `POST /api/reports/<id>/decision` | approver | Approve or reject a document |
| `GET /api/config`, `/api/audit`, `POST /api/llm/test` | admin | Settings, audit trail, LLM connection test |
| `GET /api/llm/status`, `/api/usage`, `/api/metrics` | viewer | LLM status, token usage, metrics |

## Docker

```bash
npm run setup          # creates .env
docker compose up --build
```

Opens at <http://localhost:3000>. The API is reachable only from the web container, and data lives on a volume. Use `WITH_LITELLM=true docker compose build` to include LiteLLM. When serving over https, set `COOKIE_SECURE=true` and `PUBLIC_URL` to your address.

## Tests

```bash
npm test            # backend and frontend unit tests, both enforcing 100% coverage
npm run test:e2e    # real browser tests against the running stack (needs Chrome or Edge)
```

| Suite | Tests | Coverage |
|---|---|---|
| Backend (pytest) | 175 | 100% of statements and branches (enforced) |
| Frontend (Vitest) | 75 | 100% of statements, branches, functions and lines (enforced) |
| End-to-end (Playwright) | 30 | Full user journeys; the sign-in tests run the real OpenID Connect handshake against a local fake provider |

The unit tests never call a real LLM or identity provider: they are replaced by scripted fakes. A test also checks that `.env.example` documents every setting the code reads. The end-to-end suite runs with no LLM configured, so it exercises the rules fallback. GitHub Actions runs the unit tests on every push.

## Limitations

- Sign-in has been verified against a standards-compliant fake provider, not against live Google, LinkedIn or Microsoft accounts. Do a first real sign-in yourself before relying on it.
- Built-in sentiment and summaries use simple word counting, not a language model.
- There is no audio or video transcription. Paste text or load a transcript file.
- Storage is one JSON file on one machine, which suits a team on one server but not multiple server copies. Rate limits and caches are per process.
- The approval workflow is a mock and sends no email or chat message.
- Run it behind https in any shared deployment (set `COOKIE_SECURE=true`).
- The Docker files have been syntax-checked but not yet built end to end. Please report any problem.

## License

[MIT](LICENSE) © 2026 Bhuwan Thada
