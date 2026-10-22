#!/usr/bin/env python3
"""Unstepped Cloud on this Mac: sign-in, check-ins, the trial, the lock, and app updates. Stdlib only.

Each Mac signs in with an email and a one-time code from its company's admin (Home > Settings > Account). The server
gives it a token tied to this Mac's hardware (a copied token is refused on any other Mac and flagged to the admin).
Home checks in every few minutes; Claude sessions check in when they start (the plugin's hook).

States: signed-in | trial (7 days from this Mac's first run, no shared styles) | locked (trial over, signed out,
logged out by the admin, offline too long, or an app update is required) | off (no server configured: a working copy
that never locks, like the maintainer's own before the service is set up).

Signing out, or being logged out, removes the shared styles this Mac downloaded (they live in
~/Library/Application Support/Unstepped/shared/). The person's own styles and their video projects stay.

    python3 ~/unstepped-review/cloud.py status                    where this Mac stands (no network)
    python3 ~/unstepped-review/cloud.py check [--quiet]           check in now; prints a line only when something needs doing
    python3 ~/unstepped-review/cloud.py signin <email> <code>
    python3 ~/unstepped-review/cloud.py setup <setup code> <email> "<company>" ["<name>"]   once, for the company's owner
    python3 ~/unstepped-review/cloud.py signout
    python3 ~/unstepped-review/cloud.py update                    install the newest app version from the server
    python3 ~/unstepped-review/cloud.py server <url>              set the server (the installer does this)
    python3 ~/unstepped-review/cloud.py publish <version> ["notes"]   (the service's owner) zip this app and publish it
"""
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "home"
FILE = STATE / "cloud.json"
MANAGED = Path.home() / "Library" / "Application Support" / "Unstepped" / "shared"
CHECK_EVERY = 180          # seconds between check-ins (Home's background thread, Claude's start-up hook)
LOCAL_TRIAL_DAYS = 7       # when the server can't be reached on a first run
KEEP = ("home", "backups", "test", "venv", "models", "state", "__pycache__")   # never replaced by an app update


def _now():
    return time.time()


def _iso(t=None):
    return datetime.datetime.utcfromtimestamp(t or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def load():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(st):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    tmp.replace(FILE)


def version():
    try:
        return (ROOT / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


def server():
    return (os.environ.get("UNSTEPPED_CLOUD") or load().get("server") or "").rstrip("/")


def hw():
    """This Mac, as the server knows it: a hash of the hardware UUID (the UUID itself never leaves the Mac)."""
    if os.environ.get("UNSTEPPED_HW"):   # tests: pretend to be another Mac
        return hashlib.sha256(os.environ["UNSTEPPED_HW"].encode()).hexdigest()
    try:
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True, timeout=10).stdout
        u = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out).group(1)
    except (OSError, AttributeError, subprocess.TimeoutExpired):
        u = f"{os.uname().nodename}-{Path.home()}"
    return hashlib.sha256(f"unstepped|{u}".encode()).hexdigest()


def mac_name():
    if os.environ.get("UNSTEPPED_MAC"):
        return os.environ["UNSTEPPED_MAC"]
    try:
        return subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5).stdout.strip() or "Mac"
    except OSError:
        return "Mac"


class CloudError(RuntimeError):
    def __init__(self, status, error, message="", extra=None):
        super().__init__(message or error)
        self.status, self.error, self.message, self.extra = status, error, message or error, extra or {}


def call(method, path, body=None, auth=True, data=None, stream_to=None, ctype=None, timeout=30):
    """A request to the server. JSON in and out unless data= (raw upload) or stream_to= (a download to a file)."""
    srv = server()
    if not srv:
        raise CloudError(0, "no-server", "Unstepped Cloud isn't set up on this Mac.")
    st = load()
    h = {"x-unstepped-hw": hw(), "x-unstepped-version": version(), "x-unstepped-mac": mac_name()[:80], "user-agent": f"unstepped/{version()}"}
    if auth and st.get("token"):
        h["authorization"] = f"Bearer {st['token']}"
    payload = None
    if body is not None:
        payload, h["content-type"] = json.dumps(body).encode(), "application/json"
    elif data is not None:
        payload, h["content-type"] = data, ctype or "application/octet-stream"
        if hasattr(data, "read"):
            h["content-length"] = str(os.fstat(data.fileno()).st_size)
    req = urllib.request.Request(srv + path, data=payload, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read() or b"{}")
        except ValueError:
            j = {}
        raise CloudError(e.code, j.get("error") or f"http-{e.code}", j.get("message") or "", j)
    except (urllib.error.URLError, OSError) as e:
        raise CloudError(0, "offline", f"Can't reach Unstepped Cloud ({getattr(e, 'reason', e)}).")
    with r:
        if stream_to:
            Path(stream_to).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(stream_to) + ".part")
            with open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            tmp.replace(stream_to)
            return dict(r.headers)
        raw = r.read()
        if (r.headers.get("content-type") or "").startswith("application/json"):
            return json.loads(raw or b"{}")
        return raw


# ---------------------------------------------------------------- where this Mac stands
def state():
    """{"state", "reason", "days_left", "member", "org", ...} from what this Mac last heard. No network."""
    st = load()
    if not server():
        return {"state": "off", "version": version()}
    out = {"version": version(), "server": server(), "mac": mac_name(), "latest": st.get("latest")}
    org = st.get("org") or {}
    if st.get("token"):
        out.update(member=st.get("member"), org=org)
        if st.get("ended"):
            return {**out, "state": "locked", "reason": st["ended"]}
        if org.get("min_version") and _older(version(), org["min_version"]):
            return {**out, "state": "locked", "reason": "update-needed"}
        last = st.get("last_ok") or 0
        grace = (org.get("offline_days") or 7) * 86400
        if _now() - last > grace:
            return {**out, "state": "locked", "reason": "offline"}
        return {**out, "state": "signed-in", "offline_since": st.get("offline_since")}
    start = _ts((st.get("trial") or {}).get("started")) or st.get("first_run") or _now()
    days = (st.get("trial") or {}).get("days", LOCAL_TRIAL_DAYS)
    left = days - (_now() - start) / 86400
    if st.get("ended"):   # signed out or logged out: no new trial on this Mac
        return {**out, "state": "locked", "reason": st["ended"]}
    if left <= 0:
        return {**out, "state": "locked", "reason": "trial-over", "days_left": 0}
    return {**out, "state": "trial", "days_left": round(left, 1)}


def _older(a, b):
    v = lambda s: [int(x) if x.isdigit() else 0 for x in str(s).split(".")][:3] + [0, 0, 0]
    return v(a)[:3] < v(b)[:3]


def locked():
    s = state()
    return s if s["state"] == "locked" else None


LOCK_TEXT = {"trial-over": "The 7-day trial is over. Sign in to keep using Unstepped Studio.",
             "signed-out": "This Mac is signed out of Unstepped.", "logged-out": "An admin logged this Mac out of Unstepped.",
             "removed": "This account was removed from the team.", "wrong-mac": "This sign-in belongs to another Mac.",
             "offline": "This Mac hasn't reached Unstepped Cloud for too long. Connect to the internet and check again.",
             "update-needed": "This version of Unstepped Studio is too old. Update it to keep working.",
             "suspended": "This company's Unstepped account is paused."}


def lock_message(s=None):
    s = s or state()
    return LOCK_TEXT.get(s.get("reason"), "Unstepped Studio is locked.") + " Open Unstepped Home > Settings > Account to sign in."


def guard(tool="this tool"):
    """For the command-line tools (review.py, style.py, shared.py): stop when the Mac is locked."""
    s = state()
    if s["state"] == "locked":
        sys.exit(f"Unstepped is locked: {lock_message(s)}")
    if s["state"] != "off" and _now() - (load().get("last_check") or 0) > 1800:
        try:
            check()
        except Exception:
            pass
        s = state()
        if s["state"] == "locked":
            sys.exit(f"Unstepped is locked: {lock_message(s)}")


# ---------------------------------------------------------------- checking in
def check(force=False):
    """Check in with the server (at most every few minutes unless force). Applies a logout (wipes the shared styles)
    and records the trial. Never raises for a network problem: an offline Mac keeps working until its grace runs out."""
    st = load()
    if not server():
        return state()
    if not force and _now() - (st.get("last_check") or 0) < CHECK_EVERY:
        return state()
    st.setdefault("first_run", _now())
    st["last_check"] = _now()
    save(st)
    try:
        if st.get("token") and not st.get("ended"):
            r = call("GET", "/v1/me")
            st = load()
            st.update(member=r["member"], org=r["org"], last_ok=_now(), latest=r.get("latest"), offline_since=None)
            save(st)
        elif not st.get("token"):
            r = call("POST", "/v1/hello", {"hw": hw(), "name": mac_name(), "version": version()}, auth=False)
            st = load()
            st.update(trial=r["trial"], latest=r.get("latest"), last_ok=_now())
            save(st)
    except CloudError as e:
        st = load()
        if e.status == 0:
            st.setdefault("offline_since", _now())
            save(st)
        elif e.error in ("signed-out", "not-signed-in"):
            end(e.extra.get("reason") or "signed-out")   # logged-out / removed / signed-out
        elif e.error in ("wrong-mac", "suspended"):
            end(e.error)
    s = state()
    if s["state"] == "locked" and s.get("reason") in ("offline", "trial-over"):
        wipe()
    return s


def end(reason):
    """Signed out (by this Mac or the admin): forget the token, remove the shared styles, lock."""
    st = load()
    st.pop("token", None)
    st["ended"] = reason or "signed-out"
    st["ended_at"] = _iso()
    save(st)
    wipe()


# ---------------------------------------------------------------- signing in and out
def _signed_in(r):
    st = load()
    st.update(token=r["token"], device_id=r["device_id"], member=r["member"], org=r["org"], last_ok=_now(), last_check=_now(),
              signed_in=_iso(), offline_since=None)
    for k in ("ended", "ended_at"):
        st.pop(k, None)
    save(st)
    return state()


def signin(email, code):
    r = call("POST", "/v1/signin", {"email": email.strip(), "code": code.strip(), "hw": hw(), "mac_name": mac_name(),
                                    "os": "macOS " + (subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True).stdout.strip() or ""),
                                    "version": version()}, auth=False)
    return _signed_in(r)


def setup(setup_code, email, org, name=""):
    r = call("POST", "/v1/setup", {"setup_code": setup_code, "email": email.strip(), "org": org, "name": name, "hw": hw(),
                                   "mac_name": mac_name(), "version": version()}, auth=False)
    return _signed_in(r)


def signout():
    try:
        call("POST", "/v1/signout")
    except CloudError:
        pass
    end("signed-out")
    return state()


def event(kind, **detail):
    try:
        if load().get("token"):
            call("POST", "/v1/event", {"kind": kind, **detail})
    except CloudError:
        pass


def dashboard_link():
    return call("POST", "/v1/admin/link", {})["url"]


# ---------------------------------------------------------------- the shared styles this Mac downloaded
def wipe():
    """Remove every shared style this Mac downloaded: its managed folder, Claude's link to its skill, Home's entry.
    The person's own styles and their video folders stay."""
    sys.path.insert(0, str(ROOT))
    import style as stylelib
    reg = stylelib.jread(stylelib.REGISTRY, {}) or {"styles": {}}
    shared = stylelib.jread(STATE / "shared.json", {}) or {}
    gone = []
    for sk, e in list(reg.get("styles", {}).items()):
        if not e.get("managed"):
            continue
        link = Path.home() / ".claude" / "skills" / sk
        if link.is_symlink():
            link.unlink()
        shutil.rmtree(MANAGED / sk, ignore_errors=True)
        reg["styles"].pop(sk)
        shared.pop(sk, None)
        gone.append(sk)
    if gone:
        stylelib.jwrite(stylelib.REGISTRY, reg)
        stylelib.jwrite(STATE / "shared.json", shared)
    if MANAGED.is_dir():   # anything left over from an older install
        for d in MANAGED.iterdir():
            if d.name not in reg.get("styles", {}):
                shutil.rmtree(d, ignore_errors=True)
    return gone


# ---------------------------------------------------------------- app updates
def update(force=False):
    """The newest app version from the server, checked against its sha256, installed over this one.
    Keeps home/ (account, styles list, settings), backups/, test/, venv/, models/ and state/."""
    s = state()
    info = call("GET", "/v1/app/latest", auth=s["state"] == "signed-in")
    if not force and not _older(version(), info["version"]):
        return {"updated": False, "version": version()}
    tmp = STATE / "update.zip"
    call("GET", "/v1/app/download", auth=s["state"] == "signed-in", stream_to=tmp, timeout=300)
    data = tmp.read_bytes()
    if hashlib.sha256(data).hexdigest() != info["sha256"]:
        tmp.unlink(missing_ok=True)
        raise CloudError(0, "bad-download", "The download didn't match its fingerprint; nothing was changed.")
    install_zip(data, ROOT)
    tmp.unlink(missing_ok=True)
    return {"updated": True, "version": info["version"]}


def install_zip(data, dest):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        if "VERSION" not in names or "home.py" not in names:
            raise CloudError(0, "bad-package", "That isn't an Unstepped Studio package.")
        for n in names:
            parts = n.split("/")
            if ".." in parts or n.startswith("/") or parts[0] in KEEP:
                continue
            out = Path(dest) / n
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(str(out) + ".new", "wb") as f:
                shutil.copyfileobj(src, f)
            os.replace(str(out) + ".new", out)


APP_FILES = ("home.py", "review.py", "style.py", "shared.py", "cloud.py", "lib", "LICENSE-cooked", "NOTICE-cooked")


def publish(ver, notes=""):
    """Zip this app (the files every Mac runs, never home/ or anything personal) as version ver and publish it.
    Every signed-in Mac installs it on its next check-in; trial Macs get it when they set up."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", ver):
        raise CloudError(0, "bad-version", "The version looks like 2.1.0")
    me = os.environ.get("USER") or ""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("VERSION", ver + "\n")
        for name in APP_FILES:
            p = ROOT / name
            for f in ([p] if p.is_file() else sorted(p.rglob("*")) if p.is_dir() else []):
                if f.is_file() and "__pycache__" not in f.parts and f.name != ".DS_Store":
                    data = f.read_bytes()
                    if me and me.encode() in data and f.suffix in (".py", ".html", ".md", ".js"):
                        raise CloudError(0, "personal-path", f"{f.relative_to(ROOT)} contains a path with your user name: fix it first.")
                    z.writestr(f.relative_to(ROOT).as_posix(), data)
    data = buf.getvalue()
    r = call("PUT", "/v1/admin/release?" + urllib.parse.urlencode({"version": ver, "notes": notes[:400]}), data=data, ctype="application/zip", timeout=300)
    (ROOT / "VERSION").write_text(ver + "\n")
    return r


def main():
    a = sys.argv[1:] or ["status"]
    cmd = a[0]
    try:
        if cmd == "status":
            print(json.dumps(state(), indent=1))
        elif cmd == "check":
            s = check(force=True)
            quiet = "--quiet" in a
            if s["state"] == "locked":
                print(f"Unstepped is locked: {lock_message(s)}")
            elif s["state"] == "trial" and (not quiet or s.get("days_left", 7) <= 3):
                print(f"Unstepped Studio: trial, {s['days_left']:g} days left. Sign in on Home > Settings > Account.")
            elif s["state"] == "signed-in" and s.get("latest") and _older(version(), s["latest"]):
                r = update()
                if r.get("updated"):
                    print(f"Unstepped Studio updated to {r['version']} (restart Home to load it).")
            elif not quiet:
                print(json.dumps(s, indent=1))
        elif cmd == "signin" and len(a) >= 3:
            print(json.dumps(signin(a[1], a[2]), indent=1))
        elif cmd == "setup" and len(a) >= 4:
            print(json.dumps(setup(a[1], a[2], a[3], a[4] if len(a) > 4 else ""), indent=1))
        elif cmd == "signout":
            print(json.dumps(signout(), indent=1))
        elif cmd == "update":
            print(json.dumps(update("--force" in a)))
        elif cmd == "publish" and len(a) >= 2:
            print(json.dumps(publish(a[1], a[2] if len(a) > 2 else "")))
        elif cmd == "server" and len(a) == 2:
            st = load()
            st["server"] = a[1].rstrip("/")
            save(st)
            print(json.dumps(check(force=True), indent=1))
        else:
            print(__doc__)
    except CloudError as e:
        if "--quiet" in a:
            return
        sys.exit(e.message)


if __name__ == "__main__":
    main()
