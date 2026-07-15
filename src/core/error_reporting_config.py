"""Fixed production endpoint configuration for anonymous error reporting."""

ERROR_REPORT_RELAY_ORIGIN = (
    "https://tunnelforge-issue-relay.ppkimsanh.workers.dev"
)
ERROR_REPORT_RELAY_URL = f"{ERROR_REPORT_RELAY_ORIGIN}/v1/reports"
