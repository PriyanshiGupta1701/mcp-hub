import os
import time
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

app = App(token=os.environ["SLACK_TOKEN"])
HOLMES_URL = os.environ.get("HOLMES_URL", "http://holmes:5050/api/chat")

MAX_ATTEMPTS = 6
RETRY_WAIT_SEC = 60

@app.event("app_mention")
def handle_mention(event, say):
    text = event.get("text", "")
    parts = text.split(" ", 1)
    question = parts[1].strip() if len(parts) > 1 else text.strip()

    say(text="On it — investigating...", thread_ts=event["ts"])

    answer = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.post(HOLMES_URL, json={"ask": question}, timeout=1200)

            if resp.status_code == 429:
                if attempt < MAX_ATTEMPTS - 1:
                    say(text=f"Rate limited — retrying in {RETRY_WAIT_SEC}s... (attempt {attempt + 2}/{MAX_ATTEMPTS})", thread_ts=event["ts"])
                    time.sleep(RETRY_WAIT_SEC)
                    continue
                answer = f"Holmes is still rate-limited after {MAX_ATTEMPTS} attempts. Try again in a few minutes."

            elif resp.status_code == 500:
                try:
                    error_data = resp.json()
                    error_detail = (
                        error_data.get("detail")
                        or error_data.get("message")
                        or str(error_data)
                    )
                except Exception:
                    error_detail = resp.text or ""

                # analysis=None means Gemini finished tools but gave no summary — retry
                if "Input should be a valid string" in error_detail or \
                   "analysis" in error_detail and "NoneType" in error_detail:
                    if attempt < MAX_ATTEMPTS - 1:
                        say(
                            text=f"Model didn't return a summary, retrying in {RETRY_WAIT_SEC}s... (attempt {attempt + 2}/{MAX_ATTEMPTS})",
                            thread_ts=event["ts"]
                        )
                        time.sleep(RETRY_WAIT_SEC)
                        continue
                    answer = "Holmes completed the investigation but the model did not produce a summary after several attempts. Try rephrasing your question or asking again."

                elif "list index out of range" in error_detail:
                    if attempt < MAX_ATTEMPTS - 1:
                        time.sleep(RETRY_WAIT_SEC)
                        continue
                    answer = "Holmes encountered an empty model response repeatedly. Please try again."

                else:
                    answer = f"Holmes returned an error: {error_detail[:500]}"

            else:
                resp.raise_for_status()
                data = resp.json()
                analysis = data.get("analysis")
                if analysis:
                    answer = analysis
                else:
                    other_fields = {k: v for k, v in data.items() if v is not None}
                    if other_fields:
                        answer = "\n".join(f"*{k}*: {v}" for k, v in other_fields.items())
                    else:
                        answer = f"Holmes returned an empty response. Raw: {resp.text[:500]}"
            break

        except requests.exceptions.ReadTimeout:
            if attempt < MAX_ATTEMPTS - 1:
                say(text=f"Still working... (attempt {attempt + 2}/{MAX_ATTEMPTS})", thread_ts=event["ts"])
                continue
            answer = "Holmes timed out repeatedly. Try a simpler or more specific question."
            break
        except Exception as e:
            answer = f"Error calling Holmes: {e}"
            break

    if answer:
        say(text=answer, thread_ts=event["ts"])

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
