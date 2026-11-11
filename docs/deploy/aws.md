# Deploying on AWS

Two routes: **ECS on Fargate** (no servers to manage) or **EKS** (use `deploy/kubernetes/insight-vault.yaml`).
The outline below is for ECS. See the [overview](README.md) for the settings every deployment needs.

> Not yet deployed on a live AWS account. Treat the first run as a dry run.

## 1. Managed services
1. **RDS for PostgreSQL** (version 14 or newer). Create a database `insightvault` and a user. Put it in private subnets.
   Allow inbound 5432 only from the API and worker security group.
2. **ElastiCache for Redis** (cluster mode off is fine) in the same private subnets; allow 6379 from the same group.
   Use `rediss://` in `REDIS_URL` if you enable in-transit encryption.
3. **Secrets Manager**: store one secret containing `DATABASE_URL`, `REDIS_URL`, `SECRETS_KEY`, `SECRET_KEY`, the sign-in
   client secrets and your LLM key. ECS can inject each JSON key as an environment variable.

## 2. Images
```bash
aws ecr create-repository --repository-name insight-vault-api
aws ecr create-repository --repository-name insight-vault-web
docker build -f Dockerfile.api -t ACCOUNT.dkr.ecr.REGION.amazonaws.com/insight-vault-api:1.0.0 .
docker build -f Dockerfile.web --build-arg BACKEND_URL=http://api.insight.local:5001 -t ACCOUNT.dkr.ecr.REGION.amazonaws.com/insight-vault-web:1.0.0 .
# aws ecr get-login-password | docker login ... ; docker push both
```

## 3. ECS services (one cluster, Fargate)
| Service | Task image | Command | Port | Count |
|---|---|---|---|---|
| `api` | api image | (default) | 5001 | 2 |
| `worker` | api image | `python -m backend.worker` | none | 1 |
| `web` | web image | (default) | 3000 | 2 |

* Give `api` a Cloud Map / Service Connect name `api.insight.local` so the web tasks can reach `http://api.insight.local:5001`.
* Health checks: `api` target group uses `/api/ready`; `web` uses `/`.
* Task environment: the non-secret settings from the overview table (`PUBLIC_URL`, `COOKIE_SECURE=true`, `TRUST_PROXY=1`,
  `LOG_FORMAT=json`, `DEPLOYMENT_MODE`, `AUTH_MODE`), plus the secrets from step 1.

## 4. Front door
An **Application Load Balancer** with an ACM certificate forwards to the `web` service on 443. Do not expose the `api`
service publicly: the web tier proxies to it. Set `PUBLIC_URL` to the ALB's https name.

## 5. Logs and scaling
* Logs go to CloudWatch (set `LOG_FORMAT=json`).
* Add target-tracking auto scaling on CPU for `api` and `web`. Scale `worker` by count; copies never sync the same connector twice.
