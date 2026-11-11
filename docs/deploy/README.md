# Deploying Insight Vault

The product is cloud-neutral: two container images (API and web), a PostgreSQL database, an optional Redis,
and a background worker. Nothing in the code depends on a particular cloud. Each cloud only changes *which
managed service* provides the database, the cache, the secrets and the load balancer.

| Need | Neutral choice | AWS | Google Cloud | Azure |
|---|---|---|---|---|
| Run the containers | Docker or Kubernetes | [ECS Fargate or EKS](aws.md) | [Cloud Run or GKE](gcp.md) | [Container Apps or AKS](azure.md) |
| Database | PostgreSQL 14+ | RDS for PostgreSQL | Cloud SQL for PostgreSQL | Azure Database for PostgreSQL |
| Shared cache and limits | Redis 6+ | ElastiCache | Memorystore | Azure Cache for Redis |
| Secrets | environment variables | Secrets Manager | Secret Manager | Key Vault |
| TLS and routing | any HTTPS front end | ALB + ACM | HTTPS load balancer | Front Door or App Gateway |

## What runs where

| Service | Image | Command | Scale |
|---|---|---|---|
| `api` | `Dockerfile.api` | `python -m backend` | 2 or more copies behind a load balancer |
| `worker` | `Dockerfile.api` | `python -m backend.worker` | 1 or more copies (each connector is leased while it syncs) |
| `web` | `Dockerfile.web` | `npm start` | 2 or more copies |

The `web` image proxies `/api/*` to the API. The API address is fixed when you **build** the web image
(`--build-arg BACKEND_URL=http://api:5001`), so build it once per environment.

## Settings every deployment needs

| Setting | Why |
|---|---|
| `DATABASE_URL` | PostgreSQL, for example `postgresql://user:password@host:5432/insightvault` |
| `REDIS_URL` | Needed as soon as you run more than one API copy, so the cache, rate limits and the "one LLM call at a time" lock are shared |
| `SECRETS_KEY` | Encrypts stored credentials. Create one with `python -m backend.admin secrets-key` |
| `PUBLIC_URL`, `COOKIE_SECURE=true` | The https address people use; secure session cookies |
| `TRUST_PROXY=1` | Take the client address from the load balancer's `X-Forwarded-For` (set the number of proxies in front) |
| `LOG_FORMAT=json` | Structured logs for your cloud's log service |
| `AUTH_MODE=sso` and a provider | See the README |
| `DEPLOYMENT_MODE` | `single_org` for one company, `multi_tenant` for SaaS (then also `ORG_KEY_PUBLIC_KEY_FILE` and `PLATFORM_ADMIN_KEYS`) |

The API creates and upgrades the database schema when it starts (guarded by a database lock, so several
copies can start at once). Health checks: `/api/health` (is the process alive) and `/api/ready` (can it
reach the database and Redis).

## Honest status

These guides and manifests were written against each cloud's documented services. They have been checked for
syntax and for matching this product's settings, but **they have not been deployed to a live AWS, Google Cloud
or Azure account**. Treat the first deployment in each cloud as a dry run, and expect to adjust names, networking
and permissions for your account.

## Local and single-machine options

* `docker compose up --build` runs everything, including PostgreSQL and Redis, on one machine (see the top of `docker-compose.yml`).
* `deploy/kubernetes/insight-vault.yaml` is a plain Kubernetes manifest that works on any cluster.
