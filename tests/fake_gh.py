import base64, json, re, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

def b64(s): return base64.b64encode(s.encode()).decode()

STATE = {}
def reset():
    STATE.clear()
    STATE.update(
        branches={"main": "sha-main"},
        files={"main": {"README.md": "# Proj\nHello wrold\nHello wrold again\nBye\n", "src/app.py": "print('hi')\n"}},
        commits=[], prs=[], reviews=[], calls=[],
        open_prs=[{"number": 7, "title": "Fix typo", "html_url": "https://github.com/me/proj/pull/7", "user": {"login": "me"},
                   "head": {"sha": "deadbeef0000", "ref": "agent/x"}, "created_at": "2026-09-18T00:00:00Z"}],
        checks={})
reset()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send_json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def handle_any(self, method):
        u = urlparse(self.path); q = parse_qs(u.query); p = unquote(u.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        STATE["calls"].append((method, p))
        S = STATE
        if p == "/user": return self.send_json(200, {"login": "me"})
        if p == "/user/repos":
            return self.send_json(200, [{"full_name": "me/proj", "private": True, "description": "demo", "default_branch": "main", "pushed_at": "2026-09-01T00:00:00Z"}])
        m = re.fullmatch(r"/repos/me/proj", p)
        if m: return self.send_json(200, {"default_branch": "main"})
        m = re.fullmatch(r"/repos/me/proj/git/ref/heads/(.+)", p)
        if m:
            b = m.group(1)
            return self.send_json(200, {"object": {"sha": S["branches"][b]}}) if b in S["branches"] else self.send_json(404, {"message": "Not Found"})
        if p == "/repos/me/proj/git/refs" and method == "POST":
            b = body["ref"].removeprefix("refs/heads/"); S["branches"][b] = "sha-" + b
            S["files"][b] = dict(S["files"]["main"]); return self.send_json(201, {"ref": body["ref"]})
        m = re.fullmatch(r"/repos/me/proj/contents/?(.*)", p)
        if m:
            path = m.group(1); ref = (q.get("ref") or ["main"])[0]
            files = S["files"].get(ref)
            if files is None: return self.send_json(404, {"message": "No commit found for the ref"})
            if method == "PUT":
                assert body["branch"] in S["branches"], "branch must exist"
                old = files.get(path)
                if old is not None: assert body.get("sha") == "blob-" + path, f"stale sha {body.get('sha')}"
                S["files"][body["branch"]][path] = base64.b64decode(body["content"]).decode()
                S["commits"].append((body["branch"], path, body["message"]))
                return self.send_json(200, {"commit": {"sha": "c0ffee1234567"}})
            if path == "": return self.send_json(200, [{"name": k.split('/')[0], "path": k.split('/')[0], "type": "dir" if '/' in k else "file", "size": len(v)} for k, v in files.items()])
            if path in files:
                c = files[path]; return self.send_json(200, {"type": "file", "encoding": "base64", "content": b64(c), "sha": "blob-" + path, "size": len(c)})
            return self.send_json(404, {"message": "Not Found"})
        m = re.fullmatch(r"/repos/me/proj/compare/(.+)\.\.\.(.+)", p)
        if m:
            head = m.group(2)
            changed = [k for k, v in S["files"].get(head, {}).items() if S["files"]["main"].get(k) != v]
            return self.send_json(200, {"ahead_by": len(changed), "behind_by": 0, "files": [{"filename": k, "status": "modified", "additions": 1, "deletions": 1, "patch": "@@ -1 +1 @@\n-old\n+new"} for k in changed]})
        if p == "/repos/me/proj/pulls" and method == "GET":
            return self.send_json(200, S["open_prs"])
        m = re.fullmatch(r"/repos/me/proj/commits/(.+)/check-runs", p)
        if m: return self.send_json(200, {"check_runs": S["checks"].get(m.group(1), [])})
        m = re.fullmatch(r"/repos/me/proj/commits/(.+)/status", p)
        if m: return self.send_json(200, {"state": "pending", "total_count": 0, "statuses": [], "sha": m.group(1)})
        if p == "/repos/me/proj/pulls" and method == "POST":
            S["prs"].append(body); return self.send_json(201, {"number": 7, "html_url": "https://github.com/me/proj/pull/7"})
        if p == "/repos/me/proj/pulls/7/reviews" and method == "POST":
            S["reviews"].append(body); return self.send_json(200, {"html_url": "https://github.com/me/proj/pull/7#review-1"})
        if p == "/repos/me/proj/commits":
            return self.send_json(200, [{"sha": "abcdef123456", "commit": {"author": {"date": "2026-09-01T10:00:00Z", "name": "Me"}, "message": "Initial commit\n\nbody"}, "author": {"login": "me"}}])
        if p == "/repos/me/proj/issues" and method == "GET":
            extra = list(S.get("issues", {}).values())
            return self.send_json(200, extra + [{"number": 1, "title": "Bug", "user": {"login": "x"}, "labels": [{"name": "bug"}]}, {"number": 2, "title": "A PR", "user": {"login": "x"}, "labels": [], "pull_request": {}}])
        if p == "/repos/me/proj/issues" and method == "POST":
            n = max([1, 2] + list(S.get("issues", {}).keys())) + 1
            issue = {"number": n, "title": body["title"], "body": body.get("body", ""), "state": "open", "user": {"login": "bot"}, "labels": [lb for lb in body.get("labels", [])], "html_url": f"https://github.com/me/proj/issues/{n}"}
            S.setdefault("issues", {})[n] = issue
            return self.send_json(201, issue)
        m = re.fullmatch(r"/repos/me/proj/issues/(\d+)", p)
        if m and method == "GET":
            n = int(m.group(1))
            if n in S.get("issues", {}): return self.send_json(200, S["issues"][n])
            if n == 1: return self.send_json(200, {"number": 1, "title": "Bug", "state": "open", "user": {"login": "x"}, "labels": [{"name": "bug"}], "body": "desc here"})
            return self.send_json(404, {"message": "Not Found"})
        m = re.fullmatch(r"/repos/me/proj/issues/(\d+)/comments", p)
        if m and method == "GET":
            n = int(m.group(1))
            return self.send_json(200, S.get("issue_comments", {}).get(n, []))
        if p == "/repos/me/proj/pulls/7":
            return self.send_json(200, {"number": 7, "title": "T", "state": "open", "user": {"login": "me"}, "head": {"ref": "agent/x", "sha": "deadbeef0000"}, "base": {"ref": "main"}, "commits": 1, "changed_files": 1, "additions": 1, "deletions": 1, "body": "desc"})
        if p == "/repos/me/proj/pulls/7/files":
            return self.send_json(200, [{"filename": "README.md", "status": "modified", "additions": 1, "deletions": 1, "patch": "@@ -1 +1 @@\n-a\n+b"}])
        if p == "/repos/me/private/contents":
            return self.send_json(403, {"message": "Resource not accessible by personal access token"})
        return self.send_json(404, {"message": "Not Found " + p})
    def do_GET(self): self.handle_any("GET")
    def do_PUT(self): self.handle_any("PUT")
    def do_POST(self): self.handle_any("POST")

def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
