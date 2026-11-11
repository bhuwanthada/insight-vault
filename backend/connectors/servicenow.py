"""ServiceNow: Table API (incidents by default). Resolved (6) and closed (7) records; close_notes is the resolution."""
import re

from .base import Connector, ConnectorError, checked_host, days_ago, make_ticket, parse_time, split_tools

_INSTANCE = re.compile(r"[a-z0-9][a-z0-9-]*")
_TABLE = re.compile(r"[a-z][a-z0-9_]*")
PAGE = 200
FIELDS = "sys_id,number,short_description,description,close_notes,close_code,opened_at,resolved_at,closed_at,sys_updated_on,assigned_to"


class ServiceNow(Connector):
    KIND = "servicenow"
    LABEL = "ServiceNow"
    CONFIG_FIELDS = (("instance", "Instance name (the part before .service-now.com) or full host", True), ("username", "User name", True),
                     ("table", "Table (default: incident)", False))
    SECRET_FIELDS = (("password", "Password", True),)

    def validate(self):
        raw = str(self.config.get("instance", "")).strip().lower()
        if "." not in raw and not _INSTANCE.fullmatch(raw):
            raise ConnectorError("instance may only contain letters, digits and hyphens")
        # a bare instance name means <name>.service-now.com; a full host must be on an allowed domain
        self.host = checked_host(raw if "." in raw else f"{raw}.service-now.com", (".service-now.com",), "the ServiceNow host", self.extra_hosts)
        table = str(self.config.get("table") or "incident").strip()
        if not _TABLE.fullmatch(table):
            raise ConnectorError("table must be a ServiceNow table name such as incident")
        self.table = table
        self.auth = (self.config["username"], self.secrets["password"])
        self.base = f"https://{self.host}/api/now/table"
        self._names = {}

    def _get(self, path, **params):
        return self.http.request("GET", self.base + path, params=params, auth=self.auth, headers={"Accept": "application/json"})

    def _agent(self, sys_id):
        if not sys_id:
            return ""
        if sys_id not in self._names:
            self._names[sys_id] = self._get(f"/sys_user/{sys_id}", sysparm_fields="name").get("result", {}).get("name", "")
        return self._names[sys_id]

    def pages(self, cursor):
        since = parse_time(cursor) if cursor else days_ago(self.now, self.initial_days)
        offset = 0
        while True:
            query = f"stateIN6,7^sys_updated_on>={since:%Y-%m-%d %H:%M:%S}^ORDERBYsys_updated_on"
            rows = self._get(f"/{self.table}", sysparm_query=query, sysparm_fields=FIELDS, sysparm_limit=PAGE,
                             sysparm_offset=offset, sysparm_exclude_reference_link="true").get("result", [])
            tickets, newest = [], since
            for r in rows:
                notes = (r.get("close_notes") or "").strip()
                code = (r.get("close_code") or "").strip()
                field = self.mapping.get("tools_field")
                built = make_ticket(
                    "snow", r.get("number") or r["sys_id"], subject=r.get("short_description"), created=r.get("opened_at"),
                    resolved=r.get("resolved_at") or r.get("closed_at") or r.get("sys_updated_on"), description=r.get("description"),
                    resolution=f"{notes} ({code})" if notes and code else notes or code, agent=self._agent(r.get("assigned_to")),
                    tools=split_tools(r.get(field)) if field else [])
                if built:
                    tickets.append(built)
                if r.get("sys_updated_on"):
                    newest = max(newest, parse_time(r["sys_updated_on"]))
            yield tickets, newest.isoformat()
            if len(rows) < PAGE:
                return
            offset += PAGE

    def test(self):
        self._get(f"/{self.table}", sysparm_limit=1, sysparm_fields="sys_id")
        return f"Connected to {self.host}"
