"""Salesforce Service Cloud: SOQL over the REST API. Closed cases; the newest case comment is the resolution."""
import re

from .base import Connector, ConnectorAuthError, ConnectorError, checked_host, clean_text, days_ago, make_ticket, parse_time, split_tools

_CUSTOM_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]*__c")
_VERSION = re.compile(r"v\d{2}\.\d")
HOST_SUFFIXES = (".salesforce.com", ".force.com")


class SalesforceServiceCloud(Connector):
    KIND = "salesforce"
    LABEL = "Salesforce Service Cloud"
    CONFIG_FIELDS = (("instance_url", "Instance host (for example acme.my.salesforce.com)", True),
                     ("api_version", "API version (default v60.0)", False))
    SECRET_FIELDS = (("client_id", "Connected app client id", False), ("client_secret", "Connected app client secret", False),
                     ("access_token", "Access token (instead of client id and secret)", False))

    @classmethod
    def missing(cls, config, secrets):
        problems = [n for n, _, req in cls.CONFIG_FIELDS if req and not str(config.get(n, "")).strip()]
        if not secrets.get("access_token") and not (secrets.get("client_id") and secrets.get("client_secret")):
            problems.append("access_token or client_id and client_secret")
        return problems

    def validate(self):
        self.host = checked_host(self.config.get("instance_url"), HOST_SUFFIXES, "the Salesforce host", self.extra_hosts)
        version = str(self.config.get("api_version") or "v60.0").strip()
        if not _VERSION.fullmatch(version):
            raise ConnectorError("api_version must look like v60.0")
        self.version, self.base = version, f"https://{self.host}"
        self.resolution_field = str(self.mapping.get("resolution_field") or "").strip()
        if self.resolution_field and not _CUSTOM_FIELD.fullmatch(self.resolution_field):
            raise ConnectorError("resolution_field must be a custom field API name such as Resolution__c")
        self.tools_field = str(self.mapping.get("tools_field") or "").strip()
        if self.tools_field and not _CUSTOM_FIELD.fullmatch(self.tools_field):
            raise ConnectorError("tools_field must be a custom field API name such as Tools__c")
        self._token = self.secrets.get("access_token") or ""

    def _login(self):
        data = self.http.request("POST", f"{self.base}/services/oauth2/token", data={
            "grant_type": "client_credentials", "client_id": self.secrets["client_id"], "client_secret": self.secrets["client_secret"]})
        self._token = data["access_token"]

    def _get(self, path, **params):
        """GET with the bearer token; a client-credentials token is refreshed once if it has expired."""
        if not self._token:
            self._login()
        try:
            return self.http.request("GET", self.base + path, params=params, headers={"Authorization": f"Bearer {self._token}"})
        except ConnectorAuthError:
            if self.secrets.get("access_token"):
                raise  # a fixed token cannot be refreshed
            self._login()
            return self.http.request("GET", self.base + path, params=params, headers={"Authorization": f"Bearer {self._token}"})

    def _soql(self, since):
        extra = "".join(f", {f}" for f in (self.resolution_field, self.tools_field) if f)
        return ("SELECT Id, CaseNumber, Subject, Description, CreatedDate, ClosedDate, LastModifiedDate, Owner.Name" + extra +
                ", (SELECT CommentBody FROM CaseComments ORDER BY CreatedDate DESC LIMIT 1) FROM Case "
                f"WHERE IsClosed = true AND LastModifiedDate > {since:%Y-%m-%dT%H:%M:%SZ} ORDER BY LastModifiedDate ASC")

    def pages(self, cursor):
        since = parse_time(cursor) if cursor else days_ago(self.now, self.initial_days)
        data = self._get(f"/services/data/{self.version}/query", q=self._soql(since))
        while True:
            tickets, newest = [], since
            for r in data.get("records", []):
                comments = (r.get("CaseComments") or {}).get("records") or []
                resolution = r.get(self.resolution_field) if self.resolution_field else (comments[0].get("CommentBody") if comments else "")
                built = make_ticket(
                    "sf", r.get("CaseNumber") or r["Id"], subject=r.get("Subject"), created=r.get("CreatedDate"),
                    resolved=r.get("ClosedDate") or r.get("LastModifiedDate"), description=r.get("Description"),
                    resolution=clean_text(resolution), agent=(r.get("Owner") or {}).get("Name", ""),
                    tools=split_tools(r.get(self.tools_field)) if self.tools_field else [])
                if built:
                    tickets.append(built)
                if r.get("LastModifiedDate"):
                    newest = max(newest, parse_time(r["LastModifiedDate"]))
            yield tickets, newest.isoformat()
            next_url = data.get("nextRecordsUrl")
            if data.get("done", True) or not next_url:
                return
            if not next_url.startswith("/services/data/"):
                raise ConnectorError("Salesforce returned an unexpected next-page address")
            data = self._get(next_url)

    def test(self):
        self._get(f"/services/data/{self.version}/limits")
        return f"Connected to {self.host}"
