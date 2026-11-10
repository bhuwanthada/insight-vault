"""Regenerate samples/support_tickets.json (fictional tickets; dates are stored as days-ago offsets)."""
import json
import os

# (subject, description, resolution, agent, tools, daysAgo, hoursToResolve)
TICKETS = [
    ("Cannot log in after password change", "User says login fails after changing password; locked out of the account.", "Reset the password and cleared cache; confirmed login works.", "Asha", ["admin console", "audit log"], 2, 1.5),
    ("SSO login loop", "Authentication redirects back to the login page; SSO and MFA prompt never completes.", "Reset MFA credentials and updated the SSO configuration settings.", "Ravi", ["admin console", "idp dashboard"], 5, 3.0),
    ("Locked out of workspace", "Several staff are locked out; access denied after permission change.", "Reset the password for the admin and restored permission group.", "Asha", ["admin console"], 9, 2.0),
    ("2FA codes rejected", "Authentication codes are rejected, cannot log in from new phone.", "Reset 2FA and verified login.", "Meera", ["admin console", "audit log"], 20, 1.0),
    ("Dashboard is very slow", "Dashboard loading is slow with high latency; pages timed out during peak.", "Restarted the reporting service and cleared the cache; latency back to normal.", "Ravi", ["grafana", "ssh"], 3, 4.0),
    ("Export times out", "CSV export times out and the page hangs; performance is poor.", "Applied a hotfix to the export job and restarted the worker.", "Meera", ["grafana", "log viewer"], 11, 6.0),
    ("App lag on large projects", "Severe lag and slow loading when opening large projects.", "Escalated to engineering; patch released that fixed the query performance.", "Ravi", ["grafana", "jira"], 26, 30.0),
    ("Charged twice for subscription", "Customer was charged twice; billing shows duplicate invoice and payment.", "Issued a refund for the duplicate charge and credited the next invoice.", "Priya", ["billing portal", "stripe dashboard"], 4, 2.5),
    ("Invoice shows wrong VAT", "Invoice has wrong tax on billing statement; payment blocked.", "Corrected the invoice and issued a credit note.", "Priya", ["billing portal"], 14, 5.0),
    ("Refund not received", "Refund for cancelled subscription never arrived; payment still pending.", "Re-issued the refund through the payment provider.", "Priya", ["stripe dashboard", "billing portal"], 33, 8.0),
    ("Credit card declined on renewal", "Credit card payment declined at subscription renewal; billing page errors.", "Updated payment method and retried the charge; refund of late fee issued.", "Asha", ["billing portal"], 47, 3.0),
    ("Contacts missing after import", "CSV import dropped rows; missing data and duplicate contacts after migration.", "Re-synced the data from the source file and removed duplicates.", "Meera", ["import tool", "database console"], 6, 5.5),
    ("Calendar sync stuck", "Sync has stopped; data not updating between calendar and app.", "Re-synced the account and restarted the sync worker.", "Ravi", ["admin console", "log viewer"], 16, 2.0),
    ("Duplicate records after migration", "Migration created duplicate records, sync conflicts everywhere.", "Rolled back the migration and re-ran the import with dedupe enabled.", "Meera", ["database console", "import tool"], 41, 12.0),
    ("App crashes on startup", "Application crash on launch with an error and blank screen.", "Reinstalled the application and cleared the cache; crash no longer reproduces.", "Asha", ["remote desktop", "log viewer"], 1, 1.0),
    ("500 error saving report", "Saving a report returns a 500 error with a stack trace.", "Rolled back the latest release and applied a hotfix.", "Ravi", ["log viewer", "ssh", "jira"], 8, 7.0),
    ("Editor freezes", "The editor freezes then shows an error exception.", "Applied a patch and asked customer to reinstall the desktop client.", "Meera", ["remote desktop"], 22, 4.5),
    ("Web app blank screen", "Blank screen after login, console shows exception and error.", "Cleared cache and restarted the web service.", "Asha", ["log viewer"], 58, 2.0),
    ("Webhook not firing", "Webhook integration stopped delivering events; API returns errors.", "Regenerated the API token and updated the webhook configuration.", "Ravi", ["api explorer", "log viewer"], 7, 3.5),
    ("API rate limit hit", "Customer integration gets rate limit responses from the API.", "Raised the rate limit and rotated the API key.", "Priya", ["api explorer", "admin console"], 18, 2.5),
    ("OAuth token expired for integration", "OAuth integration fails; token expired and API calls are rejected.", "Regenerated the token and allow-listed the callback domain.", "Ravi", ["api explorer"], 29, 3.0),
    ("Need help with initial setup", "New customer needs to configure settings and finish onboarding setup.", "Updated the configuration settings and walked through setup.", "Meera", ["screen share"], 10, 1.5),
    ("Install fails on managed laptops", "Install is blocked by company policy; configuration of the environment needed.", "Updated settings and allow-listed the installer; install succeeded.", "Asha", ["remote desktop", "screen share"], 37, 6.0),
    ("Not receiving email notifications", "Notification emails are not receiving; likely spam folder.", "Allow-listed the sender domain and re-enabled notification settings.", "Priya", ["admin console", "mail logs"], 12, 1.0),
    ("SMS alerts delayed", "SMS notification arrive hours late; notify rules misfire.", "Restarted the notification service and updated the configuration.", "Ravi", ["log viewer", "ssh"], 24, 3.0),
    ("Weekly digest email missing", "Customer not receiving the weekly digest email.", "Updated the notification settings and allow-listed the domain.", "Priya", ["mail logs"], 63, 2.0),
    ("Things feel off since Tuesday", "Customer says something is different and not working like before.", "Escalated to engineering; they rolled back a feature flag.", "Asha", ["jira"], 13, 9.0),
    ("Strange behaviour with shared board", "The shared board behaves oddly for one team only.", "Restarted the service after reviewing the logs.", "Meera", ["log viewer"], 31, 5.0),
    ("Login slow and then error", "Login is slow and then shows an error; authentication timed out.", "Restarted the auth service and cleared the cache.", "Ravi", ["grafana", "log viewer"], 70, 3.0),
    ("Billing page crash", "Billing page crash with an error after payment update.", "Applied a patch and issued a refund for the failed charge.", "Priya", ["billing portal", "log viewer"], 85, 6.0),
]

rows = [
    {"id": f"S-{i:03d}", "subject": s, "description": d, "resolution": r, "agent": a, "tools_used": t, "daysAgo": days, "hoursToResolve": h}
    for i, (s, d, r, a, t, days, h) in enumerate(TICKETS, 1)
]
target = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "support_tickets.json")
with open(target, "w", encoding="utf-8") as handle:
    json.dump(rows, handle, indent=2)
print(len(rows), "sample tickets written to", target)
