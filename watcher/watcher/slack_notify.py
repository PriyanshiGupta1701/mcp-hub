"""
slack_notify.py
----------------
Slack client and the single helper used to post (or thread-reply) alerts.
"""

from slack_sdk import WebClient

from .config import SLACK_TOKEN, SLACK_CHANNEL

slack = WebClient(token=SLACK_TOKEN)


def notify_slack(text, thread_ts=None):
    resp = slack.chat_postMessage(channel=SLACK_CHANNEL, text=text, thread_ts=thread_ts)
    return resp["ts"]
