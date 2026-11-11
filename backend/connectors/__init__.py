"""Ticketing-tool connectors and CSV import."""
from .base import Connector, ConnectorAuthError, ConnectorError, HttpClient
from .freshdesk import Freshdesk
from .jira import JiraServiceManagement
from .salesforce import SalesforceServiceCloud
from .servicenow import ServiceNow
from .zendesk import Zendesk

KINDS = {c.KIND: c for c in (Zendesk, Freshdesk, JiraServiceManagement, ServiceNow, SalesforceServiceCloud)}

__all__ = ["KINDS", "Connector", "ConnectorAuthError", "ConnectorError", "HttpClient"]
