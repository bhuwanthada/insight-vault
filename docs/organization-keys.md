# Organization keys and licenses

One signed key defines how the product runs for one organization. The same key format serves both ways of selling the product:

| | Single company (license) | SaaS (tenant key) |
|---|---|---|
| Key `mode` | `license` | `tenant` |
| Where it goes | `LICENSE_KEY` in the customer's `.env` | pasted into the sign-in page, or sent to `POST /api/org/activate` |
| What it does | Defines that installation: organization, features, quotas, domains, administrators, expiry | Creates (or renews) one organization on your platform and makes its first administrators |
| Also usable as | not needed | an API credential: `Authorization: Bearer <key>` acts as that organization with the key's role |

## What a key contains ("sections")

| Section | Meaning |
|---|---|
| `org` / `sub` | Organization id (lowercase letters, digits, `-`, `_`) and display name |
| `plan` | A label you choose: `starter`, `growth`, `enterprise`, ... |
| `role` | The role granted when the key is used as an API credential |
| `features` | Parts of the product that are on: `notes`, `insights`, `search`, `support`, `ai_suggestions`, `integrations` (`notes` is always required) |
| `quotas` | `tickets` (stored tickets), `llm_tokens_per_day`, `seats` (people), `connectors`. Omitted or `0` means unlimited |
| `domains` | E-mail domains that belong to the organization |
| `admins` | E-mail addresses that become administrators when the organization is activated |
| `mode` | `license` or `tenant` |
| `jti`, `iat`, `exp` | Key id (used to revoke it), issue time, expiry |

A key is **signed, not secret**: anyone holding it can read it. It never contains passwords or API keys. An organization's own LLM key is stored separately, encrypted, in the database.

## You (the vendor) do this once

```bash
python -m backend.admin keygen --out keys/
```

* `keys/org-key-private.pem` signs keys. **Keep it offline and backed up. Never put it on a server.** Anyone with it can mint valid keys.
* `keys/org-key-public.pem` is shipped with every installation. Servers point at it with `ORG_KEY_PUBLIC_KEY_FILE`.

## Issue a key

```bash
# SaaS organization
python -m backend.admin mint --private-key keys/org-key-private.pem --org acme --name "Acme Inc" --plan growth \
    --role editor --admin boss@acme.com --quota tickets=5000 --quota seats=10 --quota connectors=2 --days 365

# A license for a company that runs its own copy
python -m backend.admin mint --private-key keys/org-key-private.pem --org acme-corp --name "Acme Corp" --plan enterprise \
    --mode license --admin it@acme-corp.com --domain acme-corp.com --days 365
```

Look inside any key (add `--public-key` to verify the signature too): `python -m backend.admin inspect <key>`

## What happens next

**SaaS.** Give the customer their key. They open your sign-in page, choose *Activate an organization key*, and paste it. The organization is created and the `admins` listed in the key can sign in. To change a plan, issue a new key for the same organization and activate it again. Roles people were given are never reset.

**License.** The customer sets `LICENSE_KEY` and `ORG_KEY_PUBLIC_KEY_FILE` in `.env`. The key's features, quotas, domains and administrators apply immediately. With `LICENSE_REQUIRED=true` the app refuses to start without a valid license.

## Expiry, revocation, suspension

* **Expired:** the organization (or licensed installation) becomes **read-only**. People can still sign in, read and export their data, but cannot change anything until a renewal key is activated. A key used as an API credential simply stops working at expiry.
* **Revoked:** `POST /api/platform/keys/revoke {"jti": "..."}` (operator key). Takes effect immediately in the process that handled it and within about 15 seconds elsewhere.
* **Suspended:** `POST /api/platform/orgs/<id>/status {"status": "suspended"}`. Everyone in the organization is locked out at once; its data is kept. Send `"active"` to restore it.
* **Deleted:** `DELETE /api/platform/orgs/<id>` with `{"confirm": "<id>"}` removes the organization and everything it owns.

## The operator API (SaaS)

Set `PLATFORM_ADMIN_KEYS=ops:<a long random key>` and send it as `X-Platform-Key`.

| Call | Does |
|---|---|
| `GET /api/platform/orgs` | Organizations with plan, status, key expiry and member count (never their data) |
| `POST /api/platform/orgs/<id>/status` | Suspend or restore |
| `POST /api/platform/keys/revoke` | Revoke a key by its `jti` |
| `DELETE /api/platform/orgs/<id>` | Permanently delete (needs `confirm`) |

## Security notes

* Keys use ES256 signatures and nothing else is accepted, which rules out algorithm-substitution attacks.
* The platform's LLM key is never sent to an endpoint an organization chose: an organization that points the LLM somewhere else must bring its own key.
* Ticketing connectors only contact each vendor's real domains over HTTPS and never follow redirects, so an organization cannot aim the server at internal addresses.
