"""CSV import: the universal route for any ticketing tool that can export a spreadsheet."""
import csv
import hashlib
import io
from datetime import datetime, timezone

from .base import ConnectorError, make_ticket, parse_time, split_tools

MAX_ROWS = 50_000
SYNONYMS = {
    "id": ["id", "ticket_id", "ticket_number", "number", "key", "case_number", "reference"],
    "subject": ["subject", "title", "summary", "short_description", "issue"],
    "description": ["description", "details", "body", "problem"],
    "resolution": ["resolution", "solution", "resolution_notes", "close_notes", "fix", "how_fixed", "resolved_by_action"],
    "agent": ["agent", "assignee", "assigned_to", "owner", "resolved_by", "handler"],
    "tools": ["tools", "tools_used", "tool", "systems_used"],
    "created": ["created", "created_at", "created_date", "opened", "opened_at", "date_created", "createdat"],
    "resolved": ["resolved", "resolved_at", "resolved_date", "closed", "closed_at", "closed_date", "date_resolved", "resolvedat"],
}
REQUIRED = ("subject", "created", "resolved")


def _norm(header):
    return str(header or "").strip().lower().replace(" ", "_").replace("-", "_")


def _delimiter(first_line):
    return max(",;\t|", key=first_line.count)


def _column_for(field, headers, mapping):
    wanted = _norm(mapping.get(field)) if mapping.get(field) else None
    if wanted:
        if wanted not in headers:
            raise ConnectorError(f"the column '{mapping[field]}' for {field} was not found; columns are: {', '.join(headers.values())}")
        return wanted
    return next((c for c in SYNONYMS[field] if c in headers), None)


def _when(text, date_format):
    if date_format:
        parsed = datetime.strptime(text.strip(), date_format)
        return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).isoformat()
    return parse_time(text).isoformat()


def parse_csv(text, mapping=None, date_format=None):
    """Turn CSV text into tickets. Returns (tickets, skipped) where skipped is [{'row': n, 'reason': why}].

    Columns are found by common names (subject/title/summary, created/opened, resolved/closed, ...) or by an
    explicit mapping {field: column}. Raises ConnectorError if a needed column cannot be found."""
    mapping = mapping or {}
    lines = text.strip().splitlines()
    if len(lines) < 2:
        raise ConnectorError("the file needs a header row and at least one ticket")
    reader = csv.DictReader(io.StringIO(text.strip()), delimiter=_delimiter(lines[0]))
    headers = {_norm(h): h for h in reader.fieldnames or [] if h}
    columns = {field: _column_for(field, headers, mapping) for field in SYNONYMS}
    missing = [f for f in REQUIRED if not columns[f]]
    if missing:
        raise ConnectorError(f"could not find a column for: {', '.join(missing)}. Columns are: {', '.join(headers.values())}. "
                             "Map them explicitly to continue")
    value = lambda row, field: (row.get(headers[columns[field]]) or "").strip() if columns[field] else ""
    tickets, skipped = [], []
    for number, row in enumerate(reader, start=2):
        if number - 1 > MAX_ROWS:
            raise ConnectorError(f"too many rows: the limit is {MAX_ROWS} per import")
        try:
            created, resolved = _when(value(row, "created"), date_format), _when(value(row, "resolved"), date_format)
        except ValueError:
            skipped.append({"row": number, "reason": "unreadable date"})
            continue
        ticket_id = value(row, "id") or hashlib.sha1(f"{value(row, 'subject')}|{created}|{resolved}".encode()).hexdigest()[:12]
        built = make_ticket("csv", ticket_id, subject=value(row, "subject"), created=created, resolved=resolved,
                            description=value(row, "description"), resolution=value(row, "resolution"),
                            agent=value(row, "agent"), tools=split_tools(value(row, "tools")))
        if built is None:
            skipped.append({"row": number, "reason": "missing subject"})
        else:
            tickets.append(built)
    return tickets, skipped
