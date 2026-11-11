"""The deployment files must stay consistent with the product: valid YAML, real settings, real commands."""
import re
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTED = set(dotenv_values(ROOT / ".env.example"))
# names that appear in the guides but are not application settings (placeholders, cloud or tool variables)
NOT_SETTINGS = {"BACKEND_URL", "REGISTRY", "ACCOUNT", "REGION", "PROJECT", "RG", "AWS", "XXXX", "NODE_ENV", "LLM", "SSO", "API", "TLS", "ECS", "EKS",
                "GKE", "AKS", "VPC", "SQL", "IP", "HTTP", "HTTPS", "JSON", "URL", "RDS", "ACM", "ALB", "CPU", "GCE", "VNet", "VNET", "AWS", "TLS"}


def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def manifests():
    return [d for d in yaml.safe_load_all((ROOT / "deploy" / "kubernetes" / "insight-vault.yaml").read_text(encoding="utf-8")) if d]


def test_compose_defines_the_whole_stack_and_wires_it_together():
    services = compose()["services"]
    assert set(services) == {"db", "redis", "api", "worker", "web"}
    for name in ("api", "worker"):
        env = services[name]["environment"]
        assert env["DATABASE_URL"].startswith("postgresql://insight:") and "@db:5432/" in env["DATABASE_URL"]
        assert env["REDIS_URL"] == "redis://redis:6379/0" and env["TRUST_PROXY"] == "1"
        assert services[name]["env_file"] == ".env"
    assert services["worker"]["command"] == ["python", "-m", "backend.worker"] and services["worker"]["healthcheck"] == {"disable": True}
    assert services["api"]["depends_on"]["db"]["condition"] == "service_healthy" and services["web"]["ports"] == ["3000:3000"]
    assert "ports" not in services["api"] and "ports" not in services["db"]  # only the web tier is reachable from outside
    assert "POSTGRES_PASSWORD" in DOCUMENTED and "?set POSTGRES_PASSWORD" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_kubernetes_manifests_are_complete_and_probe_the_right_endpoints():
    docs = manifests()
    kinds = [d["kind"] for d in docs]
    assert kinds == ["Namespace", "ConfigMap", "Deployment", "Service", "Deployment", "Deployment", "Service", "Ingress", "PodDisruptionBudget"]
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    api = deployments["api"]["spec"]["template"]["spec"]["containers"][0]
    assert api["readinessProbe"]["httpGet"]["path"] == "/api/ready" and api["livenessProbe"]["httpGet"]["path"] == "/api/health"
    assert deployments["worker"]["spec"]["template"]["spec"]["containers"][0]["command"] == ["python", "-m", "backend.worker"]
    for d in docs:
        assert d["apiVersion"] and d["metadata"]["name"]
        if d["kind"] == "Deployment":
            for c in d["spec"]["template"]["spec"]["containers"]:
                assert c["resources"]["requests"] and c["resources"]["limits"], d["metadata"]["name"]


def test_the_configmap_only_uses_documented_settings():
    config = next(d for d in manifests() if d["kind"] == "ConfigMap")["data"]
    assert set(config) <= DOCUMENTED, set(config) - DOCUMENTED
    from backend.config import Settings

    assert Settings.from_env({k: v for k, v in config.items() if k not in ("DEPLOYMENT_MODE", "AUTH_MODE")}).cookie_secure is True


@pytest.mark.parametrize("path", sorted((ROOT / "docs" / "deploy").glob("*.md")) + [ROOT / "deploy" / "kubernetes" / "insight-vault.yaml"], ids=lambda p: p.name)
def test_deployment_docs_only_mention_real_settings(path):
    names = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})(?:=[^`]*)?`", path.read_text(encoding="utf-8")))
    unknown = {n for n in names if n not in DOCUMENTED and n not in NOT_SETTINGS}
    assert not unknown, f"{path.name} mentions settings that do not exist: {sorted(unknown)}"


def test_the_guides_name_the_real_commands_and_images():
    text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "docs" / "deploy").glob("*.md"))
    assert "python -m backend.worker" in text and "Dockerfile.api" in text and "Dockerfile.web" in text and "/api/ready" in text
    assert "not yet deployed" in text.lower() or "not been deployed" in text.lower()  # the guides must keep saying they are unverified
    assert (ROOT / "Dockerfile.api").is_file() and (ROOT / "Dockerfile.web").is_file()
