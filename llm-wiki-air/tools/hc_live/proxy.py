"""Fault-injection proxy for live tests (handoff-conflicts-tests.md section 4).

  .venv/bin/python tools/hc_live/proxy.py <listen_port> <upstream_base> <rules.json>

Run it, then start the pipeline with HTTP_PROXY=http://127.0.0.1:<port> and NO_PROXY= (empty):
the GROWI and chat clients send absolute URLs to it, so growi_url stays unchanged and no
endpoint-change republish happens. rules.json is re-read on every request; rewriting it
resets the counters. Format:

  {"rules": [{"method": "PUT", "path": "/_api/v3/page", "body_contains": "<page id>",
              "nth": 1, "action": "status", "status": 500}]}

path and body_contains are substrings. nth 1 fires on the first match, nth 0 on every
match. Actions: "status" answers with that HTTP status without forwarding, "delay" waits
"seconds" then forwards, "drop" forwards and closes the connection without answering (a
lost response). Every request is printed with the action taken.
"""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx

PORT, UPSTREAM, RULES = int(sys.argv[1]), sys.argv[2].rstrip("/"), sys.argv[3]
seen, lock = {}, threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _rule(self, body: bytes):
        try:
            stamp = os.stat(RULES).st_mtime_ns  # a rewritten rules file starts counting again
            rules = json.load(open(RULES, encoding="utf-8")).get("rules", [])
        except (OSError, ValueError):
            return None
        for i, r in enumerate(rules):
            if r.get("method", self.command) != self.command or r.get("path", "") not in self.path:
                continue
            if r.get("body_contains") and r["body_contains"].encode() not in body:
                continue
            with lock:
                seen[(stamp, i)] = seen.get((stamp, i), 0) + 1
                if int(r.get("nth", 1)) in (0, seen[(stamp, i)]):  # nth 0: every match
                    return r
        return None

    def _handle(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        rule = self._rule(body)
        print(time.strftime("%H:%M:%S"), self.command, self.path[:80], len(body), rule and rule.get("action"), flush=True)
        if rule and rule["action"] == "status":
            data = json.dumps({"error": "injected"}).encode()
            self.send_response(int(rule.get("status", 500)))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if rule and rule["action"] == "delay":
            time.sleep(float(rule.get("seconds", 60)))
        headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "content-length", "connection", "accept-encoding"}}
        with httpx.Client(timeout=900) as client:
            target = self.path if self.path.startswith("http") else UPSTREAM + self.path  # forward-proxy or reverse
            response = client.request(self.command, target, headers=headers, content=body)
        if rule and rule["action"] == "drop":
            self.close_connection = True  # the upstream applied it; the caller never hears back
            return
        self.send_response(response.status_code)
        for k, v in response.headers.items():
            if k.lower() not in {"content-length", "transfer-encoding", "connection", "content-encoding"}:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
