"""Support-resolution report: what issues were diagnosed in the last N days and how agents fixed them."""
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from string import Template

from .config import ConfigError

DEFAULT_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "report_template.md")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _top(counter, n=5):
    return [{"name": name, "count": count} for name, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def build_report(tickets, days, now, classify, extract_fixes):
    start = now - timedelta(days=days)
    window = [
        t for t in tickets if t.get("status") == "resolved" and start <= parse_time(t["resolvedAt"]) <= now
    ]
    groups = defaultdict(list)
    overall_actions, overall_tools, agents = Counter(), Counter(), defaultdict(Counter)
    low_confidence = 0
    for ticket in window:
        diagnosis = classify(ticket)
        fixes = extract_fixes(ticket)
        low_confidence += diagnosis["method"] in ("rules-low-confidence", "unclassified")
        hours = (parse_time(ticket["resolvedAt"]) - parse_time(ticket["createdAt"])).total_seconds() / 3600
        groups[diagnosis["category"]].append((ticket, diagnosis, fixes, hours))
        overall_actions.update(fixes["actions"])
        overall_tools.update(fixes["tools"])
        agents[ticket.get("agent", "unknown")][diagnosis["category"]] += 1

    categories = []
    for category, rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        actions, tools = Counter(), Counter()
        for _, _, fixes, _ in rows:
            actions.update(fixes["actions"])
            tools.update(fixes["tools"])
        categories.append(
            {
                "category": category,
                "count": len(rows),
                "share": round(len(rows) / len(window), 2),
                "avgResolutionHours": round(sum(r[3] for r in rows) / len(rows), 1),
                "avgConfidence": round(sum(r[1]["confidence"] for r in rows) / len(rows), 2),
                "topActions": _top(actions),
                "topTools": _top(tools),
                "exampleTickets": [r[0]["id"] for r in rows[:3]],
            }
        )
    by_agent = [
        {"agent": agent, "count": sum(c.values()), "topCategory": sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]}
        for agent, c in sorted(agents.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    ]
    report = {
        "days": days,
        "from": start.date().isoformat(),
        "to": now.date().isoformat(),
        "totalTickets": len(window),
        "lowConfidence": low_confidence,
        "categories": categories,
        "topActions": _top(overall_actions, 8),
        "topTools": _top(overall_tools, 8),
        "byAgent": by_agent,
    }
    report["summary"] = deterministic_summary(report)
    return report


def deterministic_summary(report):
    if not report["totalTickets"]:
        return f"No tickets were resolved in the last {report['days']} days."
    top = report["categories"][0]
    action = top["topActions"][0]["name"].lower() if top["topActions"] else "manual investigation"
    return (
        f"{report['totalTickets']} tickets were resolved in the last {report['days']} days. "
        f"The most common issue was {top['category']} ({round(top['share'] * 100)}% of tickets), "
        f"most often fixed by: {action}."
    )


def load_template(path=""):
    path = path or DEFAULT_TEMPLATE
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise ConfigError(f"Cannot read report template {path}: {exc}") from exc


def render_markdown(title, report, summary, template):
    """Fill the report template. Placeholders: $title $period $total $low_confidence $summary $categories $agents."""
    blocks = []
    if not report["categories"]:
        blocks.append("_No resolved tickets in this period._\n")
    for cat in report["categories"]:
        noun = "ticket" if cat["count"] == 1 else "tickets"
        blocks.append("\n".join([
            f"### {cat['category']} ({cat['count']} {noun}, {round(cat['share'] * 100)}%)",
            "",
            f"- Average time to resolve: {cat['avgResolutionHours']} h",
            f"- Average diagnosis confidence: {cat['avgConfidence']}",
            "- Fixes applied: " + (", ".join(f"{a['name']} ({a['count']})" for a in cat["topActions"]) or "none recorded"),
            "- Tools used: " + (", ".join(f"{t['name']} ({t['count']})" for t in cat["topTools"]) or "none recorded"),
            "- Example tickets: " + ", ".join(cat["exampleTickets"]),
            "",
        ]))
    agents = ["| Agent | Tickets | Most common issue |", "|---|---|---|"]
    agents += [f"| {a['agent']} | {a['count']} | {a['topCategory']} |" for a in report["byAgent"]]
    return Template(template).safe_substitute(
        title=title,
        period=f"{report['from']} to {report['to']} (last {report['days']} days)",
        total=report["totalTickets"],
        low_confidence=report["lowConfidence"],
        summary=summary,
        categories="\n".join(blocks).rstrip(),
        agents="\n".join(agents),
    )
