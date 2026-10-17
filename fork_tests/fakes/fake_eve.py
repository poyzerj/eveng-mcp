"""Minimal fake EVE-NG API: login cookie, sessions that can be expired, nodes,
networks, interfaces. Enough to exercise the link code paths."""
import json, uuid, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSIONS = set()
LAB = {
    "nodes": {
        "1": {"id": 1, "name": "R1"}, "2": {"id": 2, "name": "R2"}, "3": {"id": 3, "name": "R3"},
    },
    "networks": {},
    "ifaces": {n: {"ethernet": [{"name": f"Gi0/{i}", "network_id": 0} for i in range(4)], "serial": []}
               for n in ("1", "2", "3")},
}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj, cookie=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if cookie: self.send_header("Set-Cookie", f"unetlab_session={cookie}; Path=/")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")
    def authed(self):
        c = self.headers.get("Cookie", "")
        m = re.search(r"unetlab_session=([\w-]+)", c)
        return m and m.group(1) in SESSIONS
    def route(self, method):
        p = self.path.split("?")[0]
        if p == "/api/auth/login" and method == "POST":
            self.body(); sid = str(uuid.uuid4()); SESSIONS.add(sid)
            return self.send(200, {"code": 200, "status": "success", "message": "User logged in"}, sid)
        if p == "/test/expire":
            SESSIONS.clear(); return self.send(200, {"ok": True})
        if not self.authed():
            return self.send(412, {"code": 412, "status": "unauthorized", "message": "User is not authenticated or session timed out (90032)."})
        if p == "/api/status":
            return self.send(200, {"code": 200, "status": "success", "data": {"version": "6.2.0-4", "qemu_version": "x"}})
        if p == "/api/list/networks":
            return self.send(200, {"code": 200, "status": "success", "data": {"bridge": "bridge", "pnet0": "Cloud0"}})
        m = re.match(r"/api/labs/(.+?\.unl)(/.*)?$", p)
        if not m: return self.send(404, {"code": 404, "status": "fail", "message": "nope " + method + " " + p})
        sub = m.group(2) or ""
        if sub == "/nodes" and method == "GET":
            return self.send(200, {"code": 200, "status": "success", "data": LAB["nodes"]})
        mm = re.match(r"/nodes/(\d+)/interfaces$", sub)
        if mm and method == "GET":
            return self.send(200, {"code": 200, "status": "success", "data": LAB["ifaces"][mm.group(1)]})
        if mm and method == "PUT":
            for idx, net in self.body().items():
                LAB["ifaces"][mm.group(1)]["ethernet"][int(idx)]["network_id"] = int(net)
            return self.send(201, {"code": 201, "status": "success", "message": "Lab has been saved"})
        if sub == "/networks" and method == "GET":
            return self.send(200, {"code": 200, "status": "success", "data": LAB["networks"]})
        if sub == "/networks" and method == "POST":
            b = self.body(); nid = len(LAB["networks"]) + 1
            LAB["networks"][str(nid)] = {"id": nid, "name": b.get("name") or f"Net{nid}", "type": b.get("type")}
            return self.send(201, {"code": 201, "status": "success", "data": {"id": nid}})
        mn = re.match(r"/nodes/(\d+)$", sub)
        if mn and method == "PUT":
            b = self.body(); b.pop("id", None)
            LAB["nodes"][mn.group(1)].update(b); print("NODE", mn.group(1), b, flush=True)
            return self.send(201, {"code": 201, "status": "success", "message": "Lab has been saved (60023)."})
        mc = re.match(r"/configs/(\d+)$", sub)
        if mc and method == "PUT":
            b = self.body(); LAB.setdefault("configs", {})[mc.group(1)] = b["data"]
            return self.send(201, {"code": 201, "status": "success", "message": "Lab has been saved (60023)."})
        if mc and method == "GET":
            return self.send(200, {"code": 200, "status": "success",
                                   "data": {"id": int(mc.group(1)), "data": LAB.get("configs", {}).get(mc.group(1), "")}})
        mm = re.match(r"/networks/(\d+)$", sub)
        if mm and method == "PUT":
            self.body(); return self.send(201, {"code": 201, "status": "success", "message": "saved"})
        return self.send(404, {"code": 404, "status": "fail", "message": f"unhandled {method} {sub}"})
    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")

if __name__ == "__main__":
    import os
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("FAKE_EVE_PORT", "8099"))), H).serve_forever()
