"""Live LLM check: python -m backend.smoke  (npm run llm:check)

Reads your .env, then exercises the configured model for real: connection, how accurately it classifies
labeled support tickets, whether it returns valid JSON, whether suggested insights cite real quotes, and
whether it writes a usable summary. Prints latency and token use. Your API key is never printed.

Exit code: 0 all good, 1 a check failed, 2 no LLM is configured.
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone

from .classify import Classifier
from .config import ConfigError, Settings
from .insights_ai import InsightSuggester
from .llm import LLMClient, LLMUnavailable, UsageTracker
from .privacy import mask_pii
from .prompts import Prompts
from .resilience import SingleFlight, TTLCache
from .rules import load_rules
from .store import JsonStore

# (subject, description, resolution, the category a careful human would pick). Deliberately plain language,
# so the model is judged on understanding, not on keywords.
LABELED = [
    ("Locked out after phone upgrade", "New phone, authenticator app has no codes, cannot get into my account.", "Reset the multi-factor setup.", "Login & access"),
    ("Page takes forever to open", "The reports page hangs for a minute before anything shows.", "Restarted the reporting service.", "Performance"),
    ("Charged twice this month", "I see two identical charges on my card statement.", "Refunded the duplicate charge.", "Billing & payments"),
    ("Imported contacts are missing", "I uploaded my spreadsheet but half the people are not there.", "Re-ran the import with the correct file.", "Data sync & import"),
    ("App closes immediately", "It opens for a second and then disappears with an error message.", "Reinstalled the application.", "Crashes & errors"),
    ("Webhook stopped delivering", "Our integration no longer receives events from you.", "Regenerated the API token.", "Integrations & API"),
    ("How do I set up single sign-on", "We are new and want to configure SSO for the team.", "Walked through the setup steps.", "Configuration & setup"),
    ("Never get the weekly digest", "Everyone on my team says the summary emails do not arrive.", "Allow-listed the sender domain.", "Notifications & email"),
    ("Cannot sign in with my work account", "The login page rejects my password even after resetting it.", "Cleared the account lock.", "Login & access"),
    ("Dashboard is sluggish at peak hours", "Charts load slowly every morning around nine.", "Applied a performance patch.", "Performance"),
]
MIN_ACCURACY, MIN_VALID_JSON = 0.6, 0.9


def _percentile(values, pct):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def _timed(fn):
    start = time.monotonic()
    result = fn()
    return result, (time.monotonic() - start) * 1000


def run(settings, out, samples=len(LABELED), providers=None):
    """Run every check. Returns {'ok': bool, 'checks': {...}, ...}; prints a readable report to ``out``."""
    say = lambda text="": print(text, file=out)
    store = JsonStore(os.path.join(tempfile.mkdtemp(prefix="iv-smoke-"), "usage.json"))
    llm = LLMClient(settings, providers=providers, usage=UsageTracker(store, settings.daily_token_cap))
    chain = ", ".join(t.label for t in llm.targets("classify"))
    say(f"Model chain for classification: {chain}")
    say(f"API key: {'set' if settings.llm_api_key else 'NOT SET (the provider may read its own environment variable)'}")
    say(f"Personal-data masking: {'on' if settings.pii_masking else 'off'}; JSON mode: {'on' if settings.llm_json_mode else 'off'}")
    checks = {}

    # 1. connection
    ping = llm.ping()
    checks["connection"] = ping["ok"]
    say(f"\n[1] Connection: {'OK, ' + str(ping['latencyMs']) + ' ms' if ping['ok'] else 'FAILED: ' + ping['error']}")
    if not ping["ok"]:
        return {"ok": False, "checks": checks, "stats": dict(llm.stats)}

    # 2. classification accuracy and JSON reliability
    rules, prompts = load_rules(settings.rules_file), Prompts(settings.prompt_dir, settings.output_language)
    classifier = Classifier(llm, TTLCache(), SingleFlight(), settings, rules, prompts)
    hits, latencies, valid, asked, wrong = 0, [], 0, 0, []
    for subject, description, resolution, expected in LABELED[:samples]:
        text = f"{subject} {description} {resolution}".lower()
        ballots, ms = _timed(lambda: classifier._vote(text))
        latencies.append(ms)
        valid, asked = valid + len(ballots), asked + settings.votes
        winner = max(set(ballots), key=ballots.count) if ballots else None
        hits += winner == expected
        if winner != expected:
            wrong.append((subject, expected, winner))
    accuracy, validity = hits / max(1, min(samples, len(LABELED))), valid / max(1, asked)
    checks["classification"], checks["json_reliability"] = accuracy >= MIN_ACCURACY, validity >= MIN_VALID_JSON
    say(f"\n[2] Classification: {hits}/{min(samples, len(LABELED))} correct ({accuracy:.0%}); needs {MIN_ACCURACY:.0%}")
    for subject, expected, got in wrong:
        say(f"      missed: '{subject}' expected {expected}, got {got}")
    say(f"[3] Valid JSON answers: {valid}/{asked} ({validity:.0%}); needs {MIN_VALID_JSON:.0%}")
    say(f"    Latency per ticket ({settings.votes} calls): median {statistics.median(latencies):.0f} ms, slowest {_percentile(latencies, 95):.0f} ms")

    # 4. insights must cite real quotes
    highlights = [{"id": f"h{i}", "noteId": "n1", "text": f"{s}. {r}", "tags": [e.split()[0].lower()]}
                  for i, (s, _d, r, e) in enumerate(LABELED[:samples])]
    suggestion = InsightSuggester(llm, TTLCache(), SingleFlight(), prompts).suggest(highlights)
    checks["insights"] = suggestion["method"] == "llm" and bool(suggestion["suggestions"])
    say(f"\n[4] Insights: {'OK' if checks['insights'] else 'FAILED'}: {len(suggestion['suggestions'])} suggestion(s) via {suggestion['method']}"
        + (f" ({suggestion['fallbackReason']})" if suggestion.get("fallbackReason") else ""))
    for item in suggestion["suggestions"][:3]:
        say(f"      - {item['title']} ({round(item['confidence'] * 100)}% of cited quotes real, {len(item['highlightIds'])} quotes)")

    # 5. executive summary
    facts = {"days": 30, "totalTickets": samples, "categories": sorted({e for *_x, e in LABELED[:samples]})}
    try:
        summary = str(llm.complete_json("summary", prompts.get("summary"), json.dumps(facts)).get("summary", "")).strip()
    except LLMUnavailable as exc:
        summary = ""
        say(f"\n[5] Summary: FAILED: {exc}")
    checks["summary"] = bool(summary)
    if summary:
        say(f"\n[5] Summary: OK: {summary[:200]}")

    # 6. privacy filter (no model call)
    masked = mask_pii("Contact jane.doe@example.com or +1 555 123 4567")
    say(f"\n[6] Masking sample: {masked}")

    ok = all(checks.values())
    say(f"\nTokens used by this check: {llm.stats['tokens']}   calls: {llm.stats['calls']}   failed calls: {llm.stats['failures']}")
    say("RESULT: " + ("all checks passed" if ok else "some checks FAILED: " + ", ".join(k for k, v in checks.items() if not v)))
    return {"ok": ok, "checks": checks, "accuracy": accuracy, "validity": validity, "stats": dict(llm.stats)}


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m backend.smoke", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--samples", type=int, default=len(LABELED), help=f"how many labeled tickets to use (1-{len(LABELED)}); fewer means fewer calls")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv(os.environ.get("ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not settings.llm_enabled:
        print("No LLM is configured. In your .env set, for example:\n  LLM_PROVIDER=groq\n  LLM_MODEL=<a model id from your Groq console>\n"
              "  LLM_API_KEY=<your key>\nthen run this again.", file=out)
        return 2
    started = datetime.now(timezone.utc)
    report = run(settings, out, max(1, min(args.samples, len(LABELED))))
    print(f"(took {(datetime.now(timezone.utc) - started).total_seconds():.1f} s)", file=out)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
