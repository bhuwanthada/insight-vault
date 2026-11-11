"""The set of long-lived objects one organization's requests need (LLM client, caches, classifier, ...)."""
from dataclasses import dataclass

from .classify import Classifier
from .config import Settings
from .insights_ai import InsightSuggester
from .llm import LLMClient, UsageTracker
from .observability import Metrics
from .prompts import Prompts
from .report import load_template
from .resilience import SingleFlight, TTLCache
from .shared import Shared
from .rules import load_rules, load_rules_text


@dataclass
class Services:
    settings: Settings
    llm: LLMClient
    cache: TTLCache
    flight: SingleFlight
    classifier: Classifier
    suggester: InsightSuggester
    rules: object
    prompts: Prompts
    template: str
    usage: UsageTracker
    metrics: Metrics


def build_services(settings, store, llm=None, rules_text=None, template_text=None, shared=None):
    """Build the services for one organization. ``rules_text`` / ``template_text`` are that
    organization's own overrides (stored in its settings); otherwise the configured files are used."""
    rules = load_rules_text(rules_text, "your organization rules") if rules_text else load_rules(settings.rules_file)
    prompts = Prompts(settings.prompt_dir, settings.output_language)
    usage = UsageTracker(store, settings.daily_token_cap)
    llm = llm or LLMClient(settings, usage=usage)
    shared = shared or Shared()  # in-memory unless REDIS_URL shares them across processes
    cache, flight = shared.cache(store.org_id, settings), shared.flight(store.org_id)
    return Services(
        settings, llm, cache, flight,
        Classifier(llm, cache, flight, settings, rules, prompts),
        InsightSuggester(llm, cache, flight, prompts),
        rules, prompts, template_text or load_template(settings.report_template), usage, Metrics(),
    )
