import os
import json
import time
from flask import Flask, request, Response, send_from_directory
import anthropic

app = Flask(__name__, static_folder="public")

# Model for the roast. claude-sonnet-4-6 is fast and inexpensive for a public
# tool. For a sharper roast (better abductive leaps and timing), switch this to
# claude-opus-4-8. One line, at higher cost and latency.
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096

# Server-side web search. Anthropic runs the search and feeds the results back
# to the model inside the same streaming call, so there is no second round trip
# to manage here. max_uses caps searches per request so one roast cannot run
# away on cost; lower it if you want tighter spend.
TOOLS = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 5,
    }
]

# Single source of truth: the system prompt is the skill file, loaded at start.
# Commit SKILL.md to the repo root alongside this file. The same file is the
# downloadable skill, so the app and the skill can never drift apart.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_PATH = os.path.join(BASE_DIR, "SKILL.md")


def load_system_prompt(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    # Strip YAML front matter (--- ... ---) if present, keep the body.
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


SYSTEM_PROMPT = load_system_prompt(SKILL_PATH)


@app.route("/")
def index():
    return send_from_directory("public", "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("public", filename)


@app.get("/health")
def health():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return {
        "key_set": bool(key),
        "key_preview": key[:12] + "..." if key else "MISSING",
        "prompt_chars": len(SYSTEM_PROMPT),
    }


@app.post("/api/chat")
def chat():
    data = request.get_json()
    messages = data.get("messages", [])

    if not messages:
        return {"error": "Messages required"}, 400

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def sse(payload):
        return f"data: {json.dumps(payload)}\n\n"

    def generate():
        convo = list(messages)

        # The model may return a pause_turn stop reason when it pauses a long
        # turn to run a web search. When that happens, hand its partial reply
        # back so it can pick the turn up where it left off. The outer loop is
        # bounded so a turn can never continue forever.
        for _ in range(6):
            final = None

            for attempt in range(3):
                try:
                    with client.messages.stream(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=[
                            {
                                "type": "text",
                                "text": SYSTEM_PROMPT,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        tools=TOOLS,
                        messages=convo,
                    ) as stream:
                        for text in stream.text_stream:
                            yield sse({"text": text})
                        final = stream.get_final_message()
                    break
                except anthropic.APIStatusError as e:
                    if e.status_code == 529 and attempt < 2:
                        time.sleep(3 * (attempt + 1))
                        continue
                    print(f"Stream error: {e}")
                    msg = (
                        "Claude is overloaded right now. Wait a moment and try again."
                        if e.status_code == 529
                        else f"API error: {e.message}"
                    )
                    yield sse({"error": msg})
                    return
                except Exception as e:
                    print(f"Stream error: {e}")
                    yield sse({"error": "Something went wrong. Please try again."})
                    return

            if final is not None and getattr(final, "stop_reason", None) == "pause_turn":
                convo.append(
                    {
                        "role": "assistant",
                        "content": [
                            block.model_dump() if hasattr(block, "model_dump") else block
                            for block in final.content
                        ],
                    }
                )
                continue

            break

        yield sse({"done": True})

    headers = {
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
    }
    return Response(generate(), mimetype="text/event-stream", headers=headers)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f"Strategy Roast running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
