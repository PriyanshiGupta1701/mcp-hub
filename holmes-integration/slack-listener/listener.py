import os
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

app = App(token=os.environ["SLACK_BOT_TOKEN"])

HOLMES_URL = os.environ.get("HOLMES_URL", "http://holmes:5050/api/chat")

@app.event("app_mention")
def handle_mention(event, say):
    text = event.get("text", "")
    # Strip the bot mention tag from the start
    parts = text.split(" ", 1)
    question = parts[1].strip() if len(parts) > 1 else text.strip()

    say(text="On it — investigating...", thread_ts=event["ts"])

    try:
        resp = requests.post(HOLMES_URL, json={"ask": question}, timeout=120)
        if resp.status_code == 500:
            answer = "Holmes completed the investigation but returned no summary. Check Holmes logs for details."
        else:
            resp.raise_for_status()
            data = resp.json()
            answer = data.get("analysis") or "Holmes finished but returned no text response."
    except Exception as e:
        answer = f"Error calling Holmes: {e}"

    say(text=answer, thread_ts=event["ts"])

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
