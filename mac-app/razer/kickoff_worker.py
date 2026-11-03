#!/usr/bin/env python3
"""Kickoff worker for the Razer: runs Kickoff's sync engine (musicsync.py) for the Mac, so the hours of
listening happen here and the Mac stays free. Jake's Mac drives are mounted here at /mnt/mac/<DRIVE>
(the Razer's connect_mac_drive.sh), so a job reads the footage and writes the project XML straight
onto the drive.

    POST /api/kickoff/sync   {"args": [...engine args with /Volumes paths...], "settings": {...}}
                             -> {"id": ..., "queued": n}
    GET  /api/kickoff/jobs/<id>?since=N  -> {"state": queued|running|done|failed|cancelled,
                                             "exit": code, "lines": [...], "next": N}
    POST /api/kickoff/jobs/<id>/cancel
    GET  /api/kickoff/ping   -> {"ok": true, "engine": version, "busy": n}

One job at a time, at low priority. Before each job the engine is refreshed from Kickoff's GitHub
main, like the Mac app does. Standard library only; the engine needs numpy, scipy and ffmpeg."""
import json, os, re, subprocess, sys, threading, time, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "musicsync.py")
ENGINE_URL = os.environ.get("KICKOFF_ENGINE_URL", "https://raw.githubusercontent.com/jakenbake1998/Kickoff/main/musicsync.py")
MAC_ROOT, HERE_ROOT = "/Volumes/", os.environ.get("KICKOFF_DRIVES", "/mnt/mac").rstrip("/") + "/"
PORT = int(os.environ.get("KICKOFF_PORT", "8010"))
JOBS, QUEUE, LOCK = {}, [], threading.Lock()


def to_here(s):
    return HERE_ROOT + s[len(MAC_ROOT):] if isinstance(s, str) and s.startswith(MAC_ROOT) else s


def to_mac(line):
    return line.replace(HERE_ROOT, MAC_ROOT)


def mounted(d):
    try:
        return os.path.isdir(d) and bool(os.listdir(d))
    except OSError:
        return False


def refresh_engine():
    if ENGINE_URL == "off":
        return
    try:
        with urllib.request.urlopen(ENGINE_URL, timeout=20) as r:
            new = r.read()
        compile(new, "musicsync.py", "exec")
        old = open(ENGINE, "rb").read() if os.path.exists(ENGINE) else b""
        if new != old:
            with open(ENGINE + ".upd", "wb") as fh:
                fh.write(new)
            os.replace(ENGINE + ".upd", ENGINE)
    except Exception as e:
        print("engine refresh skipped: %s" % e, file=sys.stderr)


def engine_version():
    try:
        m = re.search(rb'^VERSION = "([^"]+)"', open(ENGINE, "rb").read(), re.M)
        return m.group(1).decode() if m else "?"
    except OSError:
        return "missing"


def run(job):
    refresh_engine()
    sf = os.path.join(HERE, "settings-%s.json" % job["id"])
    settings = dict(job.get("settings") or {})
    settings["razer_sync"] = "off"                 # never hand it on again
    with open(sf, "w") as fh:
        json.dump(settings, fh)
    args = [to_here(a) for a in job["args"]]
    cmd = ["nice", "-n", "10", sys.executable, ENGINE, "--settings", sf,
           "--path-map", HERE_ROOT.rstrip("/") + "=" + MAC_ROOT.rstrip("/")] + args
    env = dict(os.environ, KICKOFF_ON_RAZER="1", KICKOFF_DRIVES=HERE_ROOT.rstrip("/"), PYTHONUNBUFFERED="1")
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
    except OSError as e:
        job["lines"].append("error: the Razer couldn't start Kickoff (%s)" % e)
        job["state"], job["exit"] = "failed", 1
        return
    job["proc"] = p
    for line in p.stdout:
        job["lines"].append(to_mac(line.rstrip("\n")))
    p.wait()
    job["exit"] = p.returncode
    job["state"] = "cancelled" if job.get("cancel") else ("done" if p.returncode == 0 else "failed")
    try:
        os.remove(sf)
    except OSError:
        pass


def runner():
    while True:
        job = None
        with LOCK:
            if QUEUE:
                job = QUEUE.pop(0)
        if job is None:
            time.sleep(1)
            continue
        if job.get("cancel"):
            job["state"] = "cancelled"
            continue
        job["state"] = "running"
        run(job)


class H(BaseHTTPRequestHandler):
    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path, _, q = self.path.partition("?")
        if path == "/api/kickoff/ping":
            return self.reply(200, {"ok": True, "engine": engine_version(),
                                    "busy": sum(j["state"] in ("queued", "running") for j in JOBS.values())})
        m = re.match(r"^/api/kickoff/jobs/([\w-]+)$", path)
        if m and m.group(1) in JOBS:
            j = JOBS[m.group(1)]
            since = int((re.search(r"since=(\d+)", q) or [0, 0])[1])
            return self.reply(200, {"state": j["state"], "exit": j.get("exit"), "lines": j["lines"][since:],
                                    "next": len(j["lines"]), "ahead": QUEUE.index(j) if j in QUEUE else 0})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.reply(400, {"error": "bad json"})
        if self.path == "/api/kickoff/sync":
            args = body.get("args")
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                return self.reply(400, {"error": "args must be a list of strings"})
            drives = {a.split("/")[2] for a in args if a.startswith(MAC_ROOT) and len(a.split("/")) > 2}
            missing = sorted(d for d in drives if not mounted(HERE_ROOT + d))
            if missing:
                return self.reply(409, {"error": "%s isn't connected on the Razer" % missing[0]})
            j = dict(id=uuid.uuid4().hex[:12], args=args, settings=body.get("settings") or {}, state="queued", lines=[])
            with LOCK:
                JOBS[j["id"]] = j
                QUEUE.append(j)
            return self.reply(200, {"id": j["id"], "queued": len(QUEUE)})
        m = re.match(r"^/api/kickoff/jobs/([\w-]+)/cancel$", self.path)
        if m and m.group(1) in JOBS:
            j = JOBS[m.group(1)]
            j["cancel"] = True
            if j.get("proc") and j["proc"].poll() is None:
                j["proc"].terminate()
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    refresh_engine()
    threading.Thread(target=runner, daemon=True).start()
    print("Kickoff worker on port %d, engine %s, drives at %s" % (PORT, engine_version(), HERE_ROOT))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
