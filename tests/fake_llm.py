"""Scriptable fake OpenRouter for tests."""
import json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = []          # list of actions, consumed in order; when empty -> default text reply
SEEN = []            # (model, has_tools, tool_choice)

def _completion(model, message, finish="stop"):
    return {"id": "x", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj, headers=None):
        raw = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model = body["model"]
        SEEN.append((model, bool(body.get("tools")), body.get("tool_choice"), body.get("max_tokens"), body.get("stop")))
        act = SCRIPT.pop(0) if SCRIPT else ("text", "default reply")
        kind = act[0]
        if kind == "status":
            return self._send(act[1], {"error": {"message": act[2] if len(act) > 2 else "boom", "code": act[1]}})
        if kind == "429_minute":
            return self._send(429, {"error": {"message": "Rate limit exceeded: free-models-per-min", "code": 429}}, {"Retry-After": str(act[1])})
        if kind == "429_day":
            return self._send(429, {"error": {"message": "Rate limit exceeded: free-models-per-day. Add credits", "code": 429}})
        if kind == "429_upstream":
            return self._send(429, {"error": {"message": "Provider returned error: temporarily rate-limited upstream", "code": 429}})
        if kind == "200_error":
            return self._send(200, {"error": {"message": "Provider disconnected", "code": 502}})
        if kind == "empty":
            return self._send(200, _completion(model, {"role": "assistant", "content": None}))
        if kind == "hang":
            time.sleep(act[1]); 
            try: return self._send(200, _completion(model, {"role": "assistant", "content": "late"}))
            except Exception: return
        if kind == "text":
            return self._send(200, _completion(model, {"role": "assistant", "content": act[1]}))
        if kind == "tool":
            tc = {"id": "call_1", "type": "function", "function": {"name": act[1], "arguments": json.dumps(act[2])}}
            return self._send(200, _completion(model, {"role": "assistant", "content": None, "tool_calls": [tc]}, "tool_calls"))
        if kind == "json_text":   # tool call printed as text
            return self._send(200, _completion(model, {"role": "assistant", "content": json.dumps({"name": act[1], "arguments": act[2]})}))
        self._send(500, {"error": {"message": "unknown script"}})

def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"
