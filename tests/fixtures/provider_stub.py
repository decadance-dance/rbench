"""A local stand-in for the providers' model APIs, for live tests that make no provider call.

Run in the agent container: `python3 provider_stub.py <port> <log>`. Each request is appended to
<log> as one JSON line {"method", "path", "body"}. `POST .../messages` (Anthropic) and
`POST .../responses` (OpenAI Responses) get a minimal streamed reply "OK"; anything else a 404.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def sse(events: list[dict]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def anthropic(model: str) -> bytes:
    usage = {"input_tokens": 1, "output_tokens": 1}
    message = {"id": "msg_stub", "type": "message", "role": "assistant", "model": model}
    message |= {"content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
    return sse([
        {"type": "message_start", "message": message},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ])  # fmt: skip


def responses() -> bytes:
    text = {"type": "output_text", "text": "OK", "annotations": []}
    item = {"type": "message", "role": "assistant", "id": "msg_stub", "content": [text]}
    usage = {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 1,
             "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2}  # fmt: skip
    return sse([
        {"type": "response.created", "response": {"id": "resp_stub"}},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"id": "resp_stub", "usage": usage}},
    ])  # fmt: skip


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            body = raw.decode(errors="replace")
        with open(sys.argv[2], "a", encoding="utf-8") as log:
            log.write(json.dumps({"method": "POST", "path": self.path, "body": body}) + "\n")
        path = self.path.split("?")[0]
        if path.endswith("/messages"):
            self.reply(200, anthropic(body.get("model", "stub")), "text/event-stream")
        elif path.endswith("/responses"):
            self.reply(200, responses(), "text/event-stream")
        else:
            self.reply(404, b'{"error": "not here"}', "application/json")

    def do_GET(self) -> None:
        with open(sys.argv[2], "a", encoding="utf-8") as log:
            log.write(json.dumps({"method": "GET", "path": self.path, "body": None}) + "\n")
        self.reply(404, b'{"error": "not here"}', "application/json")

    def reply(self, status: int, payload: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
