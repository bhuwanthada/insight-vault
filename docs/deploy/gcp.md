# Deploying on Google Cloud

Two routes: **Cloud Run** (fully managed containers) or **GKE** (use `deploy/kubernetes/insight-vault.yaml`).
The outline below is for Cloud Run. See the [overview](README.md) for the settings every deployment needs.

> Not yet deployed on a live Google Cloud project. Treat the first run as a dry run.

## 1. Managed services
1. **Cloud SQL for PostgreSQL** (14 or newer). Create database `insightvault` and a user. Prefer a private IP.
2. **Memorystore for Redis** on the same VPC.
3. **Serverless VPC Access connector** so Cloud Run can reach the private Cloud SQL and Memorystore addresses.
4. **Secret Manager**: one secret per sensitive setting (`DATABASE_URL`, `REDIS_URL`, `SECRETS_KEY`, `SECRET_KEY`, client secrets,
   LLM key). Cloud Run can expose each as an environment variable.

## 2. Images
```bash
gcloud artifacts repositories create insight-vault --repository-format=docker --location=REGION
docker build -f Dockerfile.api -t REGION-docker.pkg.dev/PROJECT/insight-vault/api:1.0.0 .
docker build -f Dockerfile.web --build-arg BACKEND_URL=https://insight-api-XXXX.a.run.app -t REGION-docker.pkg.dev/PROJECT/insight-vault/web:1.0.0 .
docker push both
```
The web image needs the API's address at build time. Deploy the API first, then build the web image with its URL.

## 3. Cloud Run services
| Service | Image | Notes |
|---|---|---|
| `insight-api` | api | port 5001, `--min-instances=1`, VPC connector, secrets from Secret Manager, readiness `/api/ready` |
| `insight-web` | web | port 3000, public (`--allow-unauthenticated`), the only public service |
| `insight-worker` | api, command `python -m backend.worker` | Cloud Run **worker pool or a job**, or run it on a small GCE/GKE workload: it needs to run continuously |

* Set `CPU always allocated` on the worker so it is not throttled between requests.
* Environment: `PUBLIC_URL` (the web service's https address or your own domain), `COOKIE_SECURE=true`, `TRUST_PROXY=1`,
  `LOG_FORMAT=json`, `DEPLOYMENT_MODE`, `AUTH_MODE`.
* Restrict the API to internal ingress plus the web service's identity if you want it private.

## 4. Front door
Map your domain to `insight-web` (Cloud Run domain mapping) or put a global HTTPS load balancer with a Google-managed certificate in front.

## 5. Logs and scaling
Logs appear in Cloud Logging (set `LOG_FORMAT=json`). Cloud Run autoscaling handles `api` and `web`; the shared Redis keeps rate limits and caches consistent across instances.
