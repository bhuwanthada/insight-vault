# Deploying on Azure

Two routes: **Azure Container Apps** (managed containers) or **AKS** (use `deploy/kubernetes/insight-vault.yaml`).
The outline below is for Container Apps. See the [overview](README.md) for the settings every deployment needs.

> Not yet deployed on a live Azure subscription. Treat the first run as a dry run.

## 1. Managed services
1. **Azure Database for PostgreSQL, Flexible Server** (14 or newer). Create database `insightvault` and a user. Use private access in the same VNet.
2. **Azure Cache for Redis**. Use `rediss://` (port 6380) in `REDIS_URL`, and the access key in the URL.
3. **Key Vault**: store `DATABASE_URL`, `REDIS_URL`, `SECRETS_KEY`, `SECRET_KEY`, client secrets and your LLM key. Container Apps can
   reference Key Vault secrets and expose them as environment variables.
4. **Azure Container Registry** for the images.

## 2. Images
```bash
az acr create --name REGISTRY --resource-group RG --sku Basic
az acr build --registry REGISTRY --file Dockerfile.api --image insight-vault-api:1.0.0 .
az acr build --registry REGISTRY --file Dockerfile.web --build-arg BACKEND_URL=http://insight-api --image insight-vault-web:1.0.0 .
```
Inside one Container Apps environment, apps reach each other by name, so `http://insight-api` works as the API address.

## 3. Container Apps (one environment, in your VNet)
| App | Image | Command | Ingress | Replicas |
|---|---|---|---|---|
| `insight-api` | api | (default) | internal, port 5001 | 2 or more |
| `insight-worker` | api | `python -m backend.worker` | none | 1 or more |
| `insight-web` | web | (default) | external, port 3000 | 2 or more |

* Probes for `insight-api`: liveness `/api/health`, readiness `/api/ready`.
* Environment: `PUBLIC_URL` (the app's https address or your domain), `COOKIE_SECURE=true`, `TRUST_PROXY=1`, `LOG_FORMAT=json`,
  `DEPLOYMENT_MODE`, `AUTH_MODE`, plus the Key Vault secrets.

## 4. Front door
Container Apps provides an https address with a managed certificate. For your own domain add a custom domain, or put **Azure Front Door**
or **Application Gateway** in front of `insight-web`.

## 5. Logs and scaling
Logs flow to Log Analytics (set `LOG_FORMAT=json`). Add HTTP or CPU scale rules for `insight-api` and `insight-web`.
