"""
Slack notification module for Aera demand planning pipeline.

Usage (CLI — called by GitHub Actions):
  python slack_report.py --status success
  python slack_report.py --status failure

Usage (module — for custom report functions added later):
  from slack_report import post_to_slack
  post_to_slack("Hello", blocks=[...])
"""

import argparse
import os
from datetime import datetime, timezone, timedelta

import requests

WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
PKT = timezone(timedelta(hours=5))


def post_to_slack(text: str, blocks: list = None) -> bool:
    """POST a message to the configured Slack incoming webhook. Returns True on success."""
    if not WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set — skipping Slack notification.")
        return False
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"✗ Slack request failed: {e}")
        return False
    if resp.status_code == 200:
        print("✓ Slack notification sent.")
        return True
    print(f"✗ Slack notification failed: {resp.status_code} — {resp.text}")
    return False


def _pipeline_blocks(status: str, run_date: str) -> list:
    success = status == "success"
    icon    = "✅" if success else "❌"
    label   = "Succeeded" if success else "FAILED"
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{icon} Daily Pipeline {label}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Date:*\n{run_date}"},
                {"type": "mrkdwn", "text": f"*Status:*\n{icon} {label}"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Aera Demand Planning · EMEA & APAC · BigQuery refreshed"}],
        },
    ]


def notify_pipeline(status: str):
    """Send a pipeline completion notification to Slack."""
    now_pkt  = datetime.now(PKT)
    run_date = now_pkt.strftime("%A, %d %b %Y  %H:%M PKT")
    icon     = "✅" if status == "success" else "❌"
    fallback = f"{icon} Daily pipeline {status} — {run_date}"
    blocks   = _pipeline_blocks(status, run_date)
    post_to_slack(fallback, blocks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--status",
        default="success",
        help="Pipeline exit status (success | failure | cancelled)",
    )
    args = parser.parse_args()
    # GitHub Actions reports "cancelled" — treat it like failure
    status = "success" if args.status == "success" else "failure"
    notify_pipeline(status)
