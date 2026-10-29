#!/usr/bin/env python3
"""Unstepped Cloud on this Mac: sign-in, check-ins, the lock, and app updates. Stdlib only.

Each Mac signs in with an email and a one-time code from its company's admin (Home > Settings > Account). The server
gives it a token tied to this Mac's hardware (a copied token is refused on any other Mac and flagged to the admin).
Home checks in every few minutes; Claude sessions check in when they start (the plugin's hook).

States: signed-in | locked (not signed in yet, signed out, logged out by the admin, offline too long, or an app update
is required) | off (no server configured: a working copy that never locks, like the service owner's own). There is no
trial: a new Mac sees only the sign-in screen, and the app is downloaded only after signing in (the installer's gate).
A sign-in lasts until someone signs out: restarts and shutdowns keep it. Offline too long only pauses Home until the Mac
reaches the server again; it never removes anything.

Signing out, or being logged out, removes the shared styles this Mac downloaded (they live in
~/Library/Application Support/Unstepped/shared/). The person's own styles and their video projects stay.

    python3 ~/unstepped-review/cloud.py status                    where this Mac stands (no network)
    python3 ~/unstepped-review/cloud.py check [--quiet]           check in now; prints a line only when something needs doing
    python3 ~/unstepped-review/cloud.py signin <email> <code>
    python3 ~/unstepped-review/cloud.py setup <setup code> <email> "<company>" ["<name>"]   once, for the company's owner
    python3 ~/unstepped-review/cloud.py signout
    python3 ~/unstepped-review/cloud.py update                    install the newest app version from the server
    python3 ~/unstepped-review/cloud.py server <url>              set the server (the installer does this)
    python3 ~/unstepped-review/cloud.py gate [--no-open]          the sign-in page a new Mac sees first (the installer opens it)
    python3 ~/unstepped-review/cloud.py publish <version> ["notes"]   (the service's owner) zip this app and publish it
"""
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
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
KEEP = ("home", "backups", "test", "venv", "models", "state", "__pycache__")   # never replaced by an app update
UPDATING = {}   # {"to", "at"} while a new version is being installed: Home's page shows its updating screen


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


def _uuid():
    """This Mac's hardware UUID, or None when it can't be read right now (a busy Mac, too many open files)."""
    for cmd, pat in ((["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], r'"IOPlatformUUID"\s*=\s*"([^"]+)"'),
                     (["/usr/sbin/system_profiler", "SPHardwareDataType"], r"Hardware UUID:\s*(\S+)")):
        for _ in range(2):
            try:
                m = re.search(pat, subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout)
                if m:
                    return m.group(1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            time.sleep(0.3)
    return None


_HW = {}


def hw():
    """This Mac, as the server knows it: a hash of the hardware UUID (the UUID itself never leaves the Mac). When the
    UUID can't be read for a moment it's the one this Mac signed in with, never a made-up one: a made-up one reads as a
    copied sign-in and locks the Mac out (Rizwan's Mac, the minute 2.2.3 installed)."""
    if os.environ.get("UNSTEPPED_HW"):   # tests: pretend to be another Mac
        return hashlib.sha256(os.environ["UNSTEPPED_HW"].encode()).hexdigest()
    if load().get("hw_legacy"):   # the server knows this Mac by the ID an older version made up (see _adopt_legacy)
        return _legacy_hw()
    if "hw" not in _HW:
        u = _uuid()
        if not u:
            if load().get("hw"):
                return load()["hw"]
            raise CloudError(0, "no-hw", "Couldn't read this Mac's hardware ID just now. Try again in a moment.")
        _HW["hw"] = hashlib.sha256(f"unstepped|{u}".encode()).hexdigest()
    return _HW["hw"]


def _legacy_hw():
    """The ID versions before 2.2.4 sent when they couldn't read the hardware UUID, made from this Mac's name and home
    folder. A Mac that signed in at such a moment is known to the server by it."""
    return hashlib.sha256(f"unstepped|{os.uname().nodename}-{Path.home()}".encode()).hexdigest()


def _adopt_legacy():
    """The server said wrong-mac. Before locking this Mac, ask once with the ID an older version would have made up
    here: if the server knows this Mac by that one (it signed in while 2.2.3 couldn't read the UUID), keep using it, and
    this Mac stays signed in. A sign-in copied to another Mac makes a different one there, so it still locks."""
    old = _legacy_hw()
    if os.environ.get("UNSTEPPED_HW") or load().get("hw_legacy") or old == _HW.get("hw"):
        return False
    try:
        call("GET", "/v1/me", headers={"x-unstepped-hw": old})
    except CloudError:
        return False
    st = load()
    st.update(hw_legacy=True, hw=old)
    save(st)
    _HW["hw"] = old
    return True


def mac_name():
    if os.environ.get("UNSTEPPED_MAC"):
        return os.environ["UNSTEPPED_MAC"]
    if "name" not in _HW:
        try:
            n = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            n = ""
        if not n:
            return load().get("mac_name") or "Mac"
        _HW["name"] = n
    return _HW["name"]


# A Mac (or its router) can remember a brand-new server address as "not found" for up to 30 minutes. When the normal
# lookup fails for the Unstepped server (or any workers.dev address), ask Cloudflare's public DNS directly. The
# connection still uses the real name, so the secure connection checks it's really this server.
_real_getaddrinfo = socket.getaddrinfo
_DOH = {}


def _direct_ip(host):
    hit = _DOH.get(host)
    if hit and hit[1] > _now():
        return hit[0]
    try:
        out = subprocess.run(["dig", "+short", "+time=3", "@1.1.1.1", host, "A"], capture_output=True, text=True, timeout=8).stdout.split()
        ip = next((x for x in out if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", x)), None)
    except (OSError, subprocess.TimeoutExpired):
        ip = None
    if ip:
        _DOH[host] = (ip, _now() + 300)
    return ip


def _getaddrinfo(host, *a, **k):
    try:
        return _real_getaddrinfo(host, *a, **k)
    except socket.gaierror:
        srv = (server().split("//", 1) + [""])[1].split("/")[0].split(":")[0]
        if isinstance(host, str) and (host == srv or host.endswith(".workers.dev")):
            ip = _direct_ip(host)
            if ip:
                return _real_getaddrinfo(ip, *a, **k)
        raise


socket.getaddrinfo = _getaddrinfo


def _plain(text):
    """Header-safe text: a Mac called "Umair’s MacBook Pro" has a curly apostrophe, which headers can't carry."""
    text = str(text).replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


class CloudError(RuntimeError):
    def __init__(self, status, error, message="", extra=None):
        super().__init__(message or error)
        self.status, self.error, self.message, self.extra = status, error, message or error, extra or {}


class Stopped(Exception):
    """A download stopped part way on purpose (paused or cancelled on Home): the half-written file is gone."""


def launch():
    """How Home runs on this Mac, for the team dashboard: the login item inside Unstepped Home.app ("app", the only way
    macOS can give it drive access), inside Unstepped Home.app but started by start_home() because the login item
    wouldn't load ("app-fallback": macOS may count its access against whatever started it), the login item on bare
    Python ("python", from before 2.2.5), or started by a Claude session or a terminal ("session"). Only Home's own
    process says (None for any other command), so a session's style.py or review.py never stands in for it."""
    if Path(sys.argv[0]).name != "home.py":
        return None
    agent = os.environ.get("XPC_SERVICE_NAME") == AGENT
    if os.environ.get("UNSTEPPED_HOME_APP"):
        return "app" if agent else "app-fallback"
    return "python" if agent else "session"


def call(method, path, body=None, auth=True, data=None, stream_to=None, ctype=None, timeout=30, headers=None, with_headers=False,
         stop=None, tick=None):
    """A request to the server. JSON in and out unless data= (raw upload) or stream_to= (a download to a file). While
    streaming, stop() returning true ends it (Stopped) and tick(bytes so far) follows it."""
    srv = server()
    if not srv:
        raise CloudError(0, "no-server", "Unstepped Cloud isn't set up on this Mac.")
    st = load()
    h = {"x-unstepped-hw": hw(), "x-unstepped-version": version(), "x-unstepped-mac": _plain(mac_name())[:80], "user-agent": f"unstepped/{version()}"}
    if launch():
        h["x-unstepped-launch"] = launch()
    if auth and st.get("token"):
        h["authorization"] = f"Bearer {st['token']}"
    h.update(headers or {})
    payload = None
    if body is not None:
        payload, h["content-type"] = json.dumps(body).encode(), "application/json"
    elif data is not None:
        payload, h["content-type"] = data, ctype or "application/octet-stream"
        if hasattr(data, "read") and "content-length" not in h:   # a part of a file or bytes in memory say their own length
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
            got = 0
            with open(tmp, "wb") as f:
                while True:
                    if stop and stop():
                        f.close()
                        tmp.unlink(missing_ok=True)
                        raise Stopped()
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    got += len(b)
                    if tick:
                        tick(got)
            tmp.replace(stream_to)
            return dict(r.headers)
        raw = r.read()
        out = json.loads(raw or b"{}") if (r.headers.get("content-type") or "").startswith("application/json") else raw
        return (out, dict(r.headers)) if with_headers else out


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
    return {**out, "state": "locked", "reason": st.get("ended") or "not-signed-in"}


def _older(a, b):
    v = lambda s: [int(x) if x.isdigit() else 0 for x in str(s).split(".")][:3] + [0, 0, 0]
    return v(a)[:3] < v(b)[:3]


def locked():
    s = state()
    return s if s["state"] == "locked" else None


LOCK_TEXT = {"not-signed-in": "Sign in with your work email and the code from your admin.",
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
    if s["state"] != "off":
        try:
            if home_behind():
                kick_home()
        except Exception:
            pass


def home_behind():
    """The version Home runs when it's older than the app on disk and Home can't notice by itself (a Home from before
    2.2.7: an update a command-line tool installed never restarted it, so it served old code under the new page), else None."""
    try:
        url = (STATE / ".server").read_text(encoding="utf-8").strip()
        v = json.loads(urllib.request.urlopen(url.replace("/?t=", "/api/version?t="), timeout=1).read())
    except (OSError, ValueError):
        return None
    return v.get("version") if isinstance(v, dict) and v.get("version") != version() and not v.get("watch") else None


def kick_home():
    """Start the login item's Home again (on the code on disk). A Home started elsewhere gives way to it. At most once in
    two minutes; never from a test copy."""
    f = STATE / ".home-kicked"
    try:
        if _now() - f.stat().st_mtime < 120:
            return False
    except OSError:
        pass
    if not agent_ours():
        return False
    f.touch()
    if agent_job():
        code, said = _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{AGENT}")
        _hlog(f"kick: Home ran older code than {version()}: kickstart -k {code} {said}")
    else:   # not loaded (booted out by hand): loaded again, inside Unstepped Home.app
        agent_plist()
        start_handover()
    return True


# ---------------------------------------------------------------- checking in
def check(force=False):
    """Check in with the server (at most every few minutes unless force). Applies a logout (wipes the shared styles)
    and installs new versions. Never raises for a network problem: an offline Mac keeps working until its grace runs out."""
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
            if "hw" in _HW:   # the server just accepted it: what to send when the UUID can't be read for a moment
                st["hw"] = _HW["hw"]
            if "name" in _HW:
                st["mac_name"] = _HW["name"]
            save(st)
        elif not st.get("token"):
            r = call("POST", "/v1/hello", {"hw": hw(), "name": mac_name(), "version": version()}, auth=False)
            st = load()
            st.update(latest=r.get("latest"), last_ok=_now())
            save(st)
    except CloudError as e:
        st = load()
        if e.status == 0:
            st.setdefault("offline_since", _now())
            save(st)
        elif e.error in ("signed-out", "not-signed-in"):
            end(e.extra.get("reason") or "signed-out")   # logged-out / removed / signed-out
        elif e.error == "wrong-mac" and _adopt_legacy():
            pass   # known to the server by the ID an older version made up here: still signed in
        elif e.error in ("wrong-mac", "suspended"):
            end(e.error)
    s = state()
    if s["state"] == "signed-in" and s.get("latest") and _older(version(), s["latest"]) and not os.environ.get("UNSTEPPED_NO_AUTOUPDATE"):
        try:   # automatic updates: always on (the company decides the version, not the Mac)
            r = update()
            if r.get("updated"):
                st = load()
                st["updated_to"], st["updated_at"] = r["version"], _iso()
                save(st)
                s = {**state(), "just_updated": r["version"]}
        except CloudError:
            pass
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
              signed_in=_iso(), offline_since=None, hw=hw(), mac_name=mac_name())
    for k in ("ended", "ended_at"):
        st.pop(k, None)
    save(st)
    return state()


def signin(email, code):
    st = load()
    if st.pop("hw_legacy", None):   # signing in again: the Mac is registered by its real hardware ID from now on
        st.pop("hw", None)
        save(st)
        _HW.pop("hw", None)
    r = call("POST", "/v1/signin", {"email": email.strip(), "code": code.strip(), "hw": hw(), "mac_name": mac_name(),
                                    "os": "macOS " + (subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True).stdout.strip() or ""),
                                    "version": version()}, auth=False)
    return _signed_in(r)


def request_access(email, name):
    """A new Mac asks to join (its sign-in page): the server emails the company's owner a code for this person, and
    the owner passes it on. Returns {org, emailed}."""
    return call("POST", "/v1/request", {"email": email.strip(), "name": name.strip(), "hw": hw(), "mac_name": mac_name(),
                                        "version": version()}, auth=False)


def setup(setup_code, email, org, name=""):
    r = call("POST", "/v1/setup", {"setup_code": setup_code, "email": email.strip(), "org": org, "name": name, "hw": hw(),
                                   "mac_name": mac_name(), "version": version()}, auth=False)
    return _signed_in(r)


def recover(recovery_code, email):
    """The owner on a new Mac, after deploy.recover set a one-time recovery code on the server (proof of the Cloudflare account)."""
    r = call("POST", "/v1/recover", {"recovery_code": recovery_code, "email": email.strip(), "hw": hw(), "mac_name": mac_name(),
                                     "version": version()}, auth=False)
    return _signed_in(r)


def releases():
    """The app's versions on the server, newest first: [{version, created, notes, bytes}]."""
    return call("GET", "/v1/app/releases", auth=state()["state"] == "signed-in")


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


REPORTS = STATE / "reports-queue.json"   # reports written while offline: sent at the next check-in


def report(title, details="", kind="bug", area=None, style=None, video=None):
    """Something went wrong on this Mac (or the user said so): to the owner's Reports list on the dashboard. Offline,
    it waits in home/reports-queue.json and goes at the next check-in. Returns {"ok", "id"} or {"queued": True}."""
    r = {"title": str(title or "").strip()[:200], "details": str(details or "")[:6000], "kind": kind if kind in ("bug", "idea", "flag") else "bug",
         "area": area, "style": style, "video": video, "version": version(), "at": _now()}
    if not r["title"]:
        raise CloudError(0, "bad-request", "Say what went wrong in a line.")
    try:
        if not load().get("token"):
            raise CloudError(0, "offline", "not signed in")
        return call("POST", "/v1/report", r)
    except CloudError as e:
        if e.status and e.status != 429:
            raise
        q = json.loads(REPORTS.read_text(encoding="utf-8")) if REPORTS.is_file() else []
        q.append(r)
        REPORTS.write_text(json.dumps(q[-200:], indent=1), encoding="utf-8")
        return {"queued": True, "waiting": len(q)}


def flush_reports():
    """The reports written while offline, sent now (Home's check-in)."""
    if not REPORTS.is_file():
        return 0
    try:
        q = json.loads(REPORTS.read_text(encoding="utf-8"))
    except ValueError:
        q = []
    left, sent = [], 0
    for r in q:
        try:
            call("POST", "/v1/report", r)
            sent += 1
        except CloudError as e:
            if not e.status or e.status == 429:
                left.append(r)
    if left:
        REPORTS.write_text(json.dumps(left, indent=1), encoding="utf-8")
    else:
        REPORTS.unlink(missing_ok=True)
    return sent


def dashboard_link():
    return call("POST", "/v1/admin/link", {})["url"]


# ---------------------------------------------------------------- the shared styles this Mac downloaded
def wipe():
    """Remove every shared style this Mac downloaded: its managed folder, Claude's link to its skill, Home's entry.
    The person's own styles and their video folders stay."""
    rj = lambda f: json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}   # no app needed: a Mac at the sign-in page has only this file
    try:
        reg = rj(STATE / "styles.json") or {"styles": {}}
        shared = rj(STATE / "shared.json") or {}
    except ValueError:
        reg, shared = {"styles": {}}, {}
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
        for f, d in ((STATE / "styles.json", reg), (STATE / "shared.json", shared)):
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
            tmp.replace(f)
    rec = rj(STATE / "skills.json") if (STATE / "skills.json").is_file() else {}
    for name, e in (rec.get("installed") or {}).items():   # the company's video skills and creator profile go too
        if e.get("path"):
            shutil.rmtree(e["path"], ignore_errors=True)
            gone.append(f"skill {name}")
    if rec.get("installed"):
        (STATE / "skills.json").unlink(missing_ok=True)
    md = Path.home() / ".claude" / "CLAUDE.md"   # and the standing instruction that pointed Claude at them
    if md.is_file():
        t = md.read_text(encoding="utf-8")
        t2 = re.sub(r"\n*<!-- unstepped-studio:start.*?<!-- unstepped-studio:end -->\n?", "\n", t, flags=re.S).strip()
        if t2 != t.strip():
            md.write_text(t2 + "\n" if t2 else "", encoding="utf-8")
    sf = Path.home() / ".claude" / "settings.json"   # and the hook that kept it inside the Claude app
    try:
        sd = json.loads(sf.read_text(encoding="utf-8")) if sf.is_file() else None
        pre = ((sd or {}).get("hooks") or {}).get("PreToolUse")
        if isinstance(pre, list):
            keep = [e for e in pre if "unstepped-review/lib/browserguard.py" not in json.dumps(e)]
            if keep != pre:
                sd["hooks"]["PreToolUse"] = keep
                if not keep:
                    sd["hooks"].pop("PreToolUse")
                if not sd["hooks"]:
                    sd.pop("hooks")
                sf.write_text(json.dumps(sd, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        pass
    try:   # and the start-up hook that told each session what's new in Unstepped (lib/current.py)
        sd = json.loads(sf.read_text(encoding="utf-8")) if sf.is_file() else None
        ss = ((sd or {}).get("hooks") or {}).get("SessionStart")
        if isinstance(ss, list):
            keep = [e for e in ss if "unstepped-review/lib/current.py" not in json.dumps(e)]
            if keep != ss:
                sd["hooks"]["SessionStart"] = keep
                if not keep:
                    sd["hooks"].pop("SessionStart")
                if not sd["hooks"]:
                    sd.pop("hooks")
                sf.write_text(json.dumps(sd, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        pass
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
    UPDATING.update(to=info["version"], at=_now())   # stays set: Home restarts on the new version right after
    try:
        install_zip(data, ROOT)
    except Exception:
        UPDATING.clear()
        raise
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


# ---------------------------------------------------------------- the installer's sign-in page (no app before sign-in)
GATE_PORT = 47899
AGENT = "com.unstepped.home"
GATE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Join Unstepped Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet"><style>
:root{--page:#12161D;--panel:#171C25;--line:rgba(255,255,255,.09);--ink:#E9E6DF;--body:rgba(233,230,223,.8);--muted:rgba(233,230,223,.56);--brand:#2EB98C;--bad:#E5484D;--field:#0F1318;
  --sans:'IBM Plex Sans',system-ui,-apple-system,sans-serif;--mono:'IBM Plex Mono',ui-monospace,Menlo,monospace}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--page:#F4F3EF;--panel:#FFFFFF;--line:rgba(18,22,29,.12);--ink:#14181F;--body:#2E3440;--muted:#5E6573;--field:#FAFAF8}}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--page);color:var(--body);font:15px/1.5 var(--sans);-webkit-font-smoothing:antialiased;padding:16px}
.box{width:min(420px,100%);background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:28px;display:flex;flex-direction:column;gap:12px}form{display:flex;flex-direction:column;gap:12px}
.logo{font-weight:600;font-size:22px;color:var(--ink);letter-spacing:-.02em}.logo i{color:var(--brand);font-style:normal}h1{margin:0;font-size:22px;font-weight:600;letter-spacing:-.015em;color:var(--ink)}p{margin:0;color:var(--muted);font-size:13.5px}p b{color:var(--ink)}
input{font:inherit;height:42px;padding:0 12px;border-radius:10px;border:1px solid var(--line);background:var(--field);color:var(--ink)}input:focus{outline:none;border-color:var(--brand)}input.code{font-family:var(--mono);letter-spacing:.08em}
button{font:inherit;font-weight:600;height:42px;border:0;border-radius:10px;background:var(--brand);color:#06140E;cursor:pointer;transition:transform .12s cubic-bezier(.23,1,.32,1),filter .15s}button:hover{filter:brightness(1.07)}button:active{transform:scale(.97)}button:disabled{opacity:.6;cursor:wait}
.links{display:flex;justify-content:space-between;gap:12px;font-size:13px}.links a{color:var(--muted)}.links a:hover{color:var(--ink)}
.step{display:flex;align-items:center;gap:7px;font:500 11px/14px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}.step::before{content:'';width:7px;height:7px;border-radius:50%;background:var(--brand);box-shadow:0 0 0 3px rgba(46,185,140,.2)}#msg{min-height:20px;font-size:13px}#msg.bad{color:var(--bad)}</style></head><body>
<div class="box"><div class="logo"><i>/</i> unstepped</div>
<form id="fa"><div class="step">Step 1 of 2</div><h1>Join Unstepped Studio</h1>
<p>Enter your name and work email. Your admin gets an email with a sign-in code for you, and you enter it on the next step.</p>
<input id="name" placeholder="Your name" autocomplete="name" required><input id="aemail" type="email" placeholder="Work email" autocomplete="email" required>
<button id="ago">Request access</button><div class="links"><a href="#" id="tocode">I already have a code</a></div></form>
<form id="fc" hidden><div class="step" id="cstep">Step 2 of 2</div><h1>Enter your code</h1><p id="sent"></p>
<input id="email" type="email" placeholder="Work email" autocomplete="email" required><input id="code" class="code" placeholder="Code: K7QF-2MXP-9R4T" autocomplete="one-time-code" required>
<button id="go">Sign in</button><div class="links"><a href="#" id="toask">Use a different email</a><a href="#" id="again" hidden>Ask again</a></div></form>
<div id="msg"></div></div>
<script>
const $=id=>document.getElementById(id),T=new URLSearchParams(location.search).get('t')||'';let A=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const msg=(t,bad)=>{$('msg').textContent=t;$('msg').className=bad?'bad':''};
const show=p=>{$('fa').hidden=p!=='ask';$('fc').hidden=p!=='code';msg('')};
const post=(p,b)=>fetch(p+'?t='+T,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b)});
function asked(a){A=a;$('email').value=a.email;$('cstep').hidden=false;$('toask').textContent='Use a different email';
  $('sent').innerHTML=a.emailed?`We've asked ${esc(a.org)}'s admin to let you in as <b>${esc(a.email)}</b>. When they send you your code, enter it here.`
    :`Your request is waiting on ${esc(a.org)}'s team dashboard. When your admin sends you your code, enter it here.`;$('again').hidden=false;show('code');$('code').focus()}
async function ask(b){const r=await post('/request',b),j=await r.json().catch(()=>({}));if(!r.ok){msg(j.error||'Not sent',true);return false}asked(j);return true}
async function poll(){const r=await (await fetch('/status?t='+T)).json();msg(r.message,r.phase==='failed');
  if(r.phase==='done'&&r.url){location.href=r.url;return}if(r.phase==='failed'){$('go').disabled=false;return}setTimeout(poll,800)}
$('fa').onsubmit=async e=>{e.preventDefault();$('ago').disabled=true;msg('Sending your request…');await ask({name:$('name').value,email:$('aemail').value});$('ago').disabled=false};
$('fc').onsubmit=async e=>{e.preventDefault();$('go').disabled=true;msg('Signing in…');
  const r=await post('/signin',{email:$('email').value,code:$('code').value});
  const j=await r.json();if(!r.ok){msg(j.error||'Not signed in',true);$('go').disabled=false;return}poll()};
$('tocode').onclick=e=>{e.preventDefault();$('email').value=$('email').value||$('aemail').value;$('cstep').hidden=true;$('again').hidden=true;
  $('sent').textContent='Enter your work email and the one-time code your admin sent you.';$('toask').textContent='Request access instead';show('code')};
$('toask').onclick=e=>{e.preventDefault();show('ask')};
$('again').onclick=async e=>{e.preventDefault();if(!A)return;msg('Asking again…');if(await ask({name:A.name,email:A.email}))msg('Asked again.')};
fetch('/status?t='+T).then(r=>r.json()).then(r=>{if(r.ask&&r.ask.email)asked(r.ask);if(r.phase==='running'||r.phase==='done')poll()});
</script></body></html>"""


HOME_APP = STATE / "Unstepped Home.app"
LAUNCHER_C = r"""/* Unstepped Home's launcher. macOS decides file access (Documents, Desktop, drives, Full Disk Access) by the app
   that started a process: Home runs as this app's child, so it is "Unstepped Home" in Privacy & Security. */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>
extern char **environ;
static pid_t child;
static void pass(int s) { if (child > 0) kill(child, s); }
int main(int argc, char **argv) {
    if (argc < 2) return 64;
    signal(SIGTERM, pass); signal(SIGINT, pass); signal(SIGHUP, pass);
    if (posix_spawn(&child, argv[1], NULL, NULL, argv + 1, environ) != 0) return 127;
    int st;
    while (waitpid(child, &st, 0) < 0) if (errno != EINTR) return 1;
    return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
}
"""
_WHY = "Unstepped Home lists, opens and moves the styles and videos you keep"
APP_INFO = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.unstepped.home</string>
  <key>CFBundleName</key><string>Unstepped Home</string>
  <key>CFBundleDisplayName</key><string>Unstepped Home</string>
  <key>CFBundleExecutable</key><string>unstepped-home</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>NSDocumentsFolderUsageDescription</key><string>{_WHY} in Documents.</string>
  <key>NSDesktopFolderUsageDescription</key><string>{_WHY} on the Desktop.</string>
  <key>NSDownloadsFolderUsageDescription</key><string>{_WHY} in Downloads.</string>
  <key>NSRemovableVolumesUsageDescription</key><string>{_WHY} on this drive.</string>
  <key>NSNetworkVolumesUsageDescription</key><string>{_WHY} on this network drive.</string>
</dict></plist>
"""


LAUNCHER_LOG = STATE / "launcher.json"   # why Unstepped Home.app couldn't be built here (Settings shows it)
CLANG, CODESIGN = "/usr/bin/clang", "/usr/bin/codesign"


def home_app():
    """Unstepped Home.app's launcher (its path), built once with this Mac's own compiler and kept across updates: a
    rebuild would change its signature and lose what the person allowed. Started straight from /usr/bin/python3, macOS
    checked Home against the xcode-select shim's identity while Python.app's code ran, so no Full Disk Access grant
    could match (Zain, 2026-10-26). None without a compiler: Home then starts as before, and the step that failed, its
    return code and what it said are kept in home/launcher.json and reported (before 2.2.7 they were thrown away)."""
    exe = HOME_APP / "Contents" / "MacOS" / "unstepped-home"
    if exe.is_file():
        LAUNCHER_LOG.unlink(missing_ok=True)
        return exe
    tmp = HOME_APP.with_name(HOME_APP.name + ".new")
    src = STATE / "unstepped-home.c"
    step = "write"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        (tmp / "Contents" / "MacOS").mkdir(parents=True)
        src.write_text(LAUNCHER_C)
        step = "compile"
        built = subprocess.run([CLANG, "-O2", "-o", str(tmp / "Contents" / "MacOS" / "unstepped-home"), str(src)],
                               capture_output=True, timeout=180)
        if built.returncode:
            return _launcher_failed(step, built.returncode, built.stderr or built.stdout)
        (tmp / "Contents" / "Info.plist").write_text(APP_INFO)
        step = "sign"
        signed = subprocess.run([CODESIGN, "-s", "-", "--force", str(tmp)], capture_output=True, timeout=60)
        if signed.returncode:
            return _launcher_failed(step, signed.returncode, signed.stderr or signed.stdout)
        step = "install"
        shutil.rmtree(HOME_APP, ignore_errors=True)
        tmp.rename(HOME_APP)
        LAUNCHER_LOG.unlink(missing_ok=True)
        return exe
    except (OSError, subprocess.SubprocessError) as e:
        return _launcher_failed(step, None, f"{type(e).__name__}: {e}")
    finally:
        src.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _launcher_failed(step, code, said):
    """Unstepped Home.app couldn't be built: kept in home/launcher.json for Settings, and reported to the dashboard
    once per version (offline, it waits with the other reports). Returns None, like home_app."""
    said = (said.decode("utf-8", "replace") if isinstance(said, bytes) else str(said or "")).strip()
    rec = {"step": step, "returncode": code, "stderr": said[-2000:], "version": version(), "at": _iso(),
           "reported": (launcher_problem() or {}).get("reported")}
    if rec["reported"] != version():
        try:
            report(f"Unstepped Home.app couldn't be built on this Mac ({step} failed)",
                   f"Step: {step}\nReturn code: {code}\n{said[-3000:] or '(nothing said)'}\nHome runs straight from Python, which "
                   f"macOS can't be given access for: external drives and protected folders stay closed to it.", "bug", "home")
            rec["reported"] = version()
        except Exception:
            pass
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        LAUNCHER_LOG.write_text(json.dumps(rec, indent=1))
    except OSError:
        pass
    print(f"Unstepped Home.app: the {step} step failed ({code}): {said[-300:]}", file=sys.stderr)
    return None


def launcher_problem():
    """Why Unstepped Home.app couldn't be built on this Mac ({"step", "returncode", "stderr", "version", "at"}), or None."""
    if (HOME_APP / "Contents" / "MacOS" / "unstepped-home").is_file():
        return None
    try:
        return json.loads(LAUNCHER_LOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def agent_plist():
    """Write the login item: Home inside Unstepped Home.app when this Mac has it."""
    from xml.sax.saxutils import escape
    exe = home_app()
    args = ([str(exe)] if exe else []) + ["/usr/bin/python3", str(ROOT / "home.py"), "serve", "--no-open"]
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", **({"UNSTEPPED_HOME_APP": str(HOME_APP)} if exe else {})}
    plist = agent_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{AGENT}</string>
  <key>ProgramArguments</key><array>{"".join(f"<string>{escape(a)}</string>" for a in args)}</array>
  <key>EnvironmentVariables</key><dict>{"".join(f"<key>{k}</key><string>{escape(v)}</string>" for k, v in env.items())}</dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{escape(str(STATE / "home.log"))}</string><key>StandardErrorPath</key><string>{escape(str(STATE / "home.log"))}</string>
</dict></plist>
""")
    return plist


LAUNCHCTL = os.environ.get("UNSTEPPED_LAUNCHCTL") or "/bin/launchctl"   # (a self-check points it at a pretend launchd)
HANDOVER_LOG = STATE / "handover.log"


def agent_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT}.plist"


def _launchctl(*args):
    try:
        r = subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stderr or r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def _hlog(text):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(HANDOVER_LOG, "a", encoding="utf-8") as f:
            f.write(f"{_iso()} {text}\n")
    except OSError:
        pass


def agent_job():
    """The login item as launchd has it loaded: {"path", "program", "state", "pid"}, or None when it isn't loaded."""
    try:
        r = subprocess.run([LAUNCHCTL, "print", f"gui/{os.getuid()}/{AGENT}"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode:
        return None
    get = lambda k: (re.search(rf"^\s*{k} = (.+)$", r.stdout, re.M) or [None, None])[1]
    pid = (get("pid") or "").strip()
    return {"path": get("path"), "program": get("program"), "state": get("state"), "pid": int(pid) if pid.isdigit() else None}


def agent_file():
    """The login item's file on disk ({"args", "env"}), or None. Read with plutil: plistlib needs expat, which some
    Macs' Python lacks."""
    try:
        r = subprocess.run(["/usr/bin/plutil", "-convert", "json", "-o", "-", str(agent_path())], capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return {"args": d.get("ProgramArguments") or [], "env": d.get("EnvironmentVariables") or {}} if isinstance(d, dict) else None


def agent_current():
    """The login item's file starts this copy of Home inside Unstepped Home.app."""
    f, exe = agent_file(), HOME_APP / "Contents" / "MacOS" / "unstepped-home"
    return bool(f) and f["args"][:1] == [str(exe)] and str(ROOT / "home.py") in f["args"] and f["env"].get("UNSTEPPED_HOME_APP") == str(HOME_APP)


def agent_ours():
    """This copy of the app is the one the login item is for: ~/unstepped-review, or the one its file names. A test copy
    (or anyone's second copy) never rewrites or reloads it."""
    if os.environ.get("UNSTEPPED_NO_AGENT"):
        return False
    if ROOT.resolve() == (Path.home() / "unstepped-review").resolve():
        return True
    f = agent_file()
    return bool(f) and str(ROOT / "home.py") in f["args"]


def handover():
    """Reload the login item so Home runs inside Unstepped Home.app: booted out, gone, loaded again from its file
    (bootstrap is tried again: launchd refuses while the old job is still going away), started. Each step goes in
    home/handover.log. Runs in a helper outside the job (`cloud.py handover`), since booting out the job ends the Home
    in it. A job loaded from another file (another copy's) is left alone."""
    dom, plist = f"gui/{os.getuid()}", agent_path()
    j = agent_job()
    if j and j.get("path") and Path(j["path"]).resolve() != plist.resolve():
        _hlog(f"handover: the loaded login item is {j['path']}, not {plist}: left alone")
        return False
    if j:
        _hlog("bootout: %s %s" % _launchctl("bootout", f"{dom}/{AGENT}"))
        for _ in range(40):   # up to 20 s for the old job to go
            if not agent_job():
                break
            time.sleep(0.5)
        else:
            _hlog("bootout: the old login item is still loaded after 20 s")
    for i in range(5):
        code, said = _launchctl("bootstrap", dom, str(plist))
        _hlog(f"bootstrap ({i + 1}): {code} {said}")
        if code == 0:
            break
        time.sleep(1 + i)
    else:
        return False
    j = agent_job()
    if j and not j.get("pid"):
        _hlog("kickstart: %s %s" % _launchctl("kickstart", f"{dom}/{AGENT}"))
        j = agent_job()
    _hlog(f"loaded: {j}")
    return True


def start_handover():
    """handover() in a helper of its own session, so it outlives the job it reloads."""
    _hlog(f"handover asked by pid {os.getpid()} ({os.environ.get('XPC_SERVICE_NAME') or 'not the login item'})")
    subprocess.Popen([sys.executable, str(ROOT / "cloud.py"), "handover"], start_new_session=True, cwd=str(ROOT),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install_agent():
    """Home starts by itself whenever this person logs in (a LaunchAgent); a crash restarts it. A bootstrap launchd
    refuses (the old job still going away) is tried again; a failure is kept in home/handover.log."""
    if os.environ.get("UNSTEPPED_NO_AGENT"):
        return False
    plist = agent_plist()
    dom = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{dom}/{AGENT}")
    for i in range(4):
        code, said = _launchctl("bootstrap", dom, str(plist))
        if code == 0:
            return True
        time.sleep(1 + i)
    _hlog(f"install: bootstrap failed ({code}): {said}")
    return False


def start_home():
    """Home on this Mac (through its LaunchAgent when there is one); returns its URL once it answers. When the login
    item can't be loaded, Home still starts inside Unstepped Home.app (the login item takes over from it later)."""
    if not install_agent():
        exe = None if os.environ.get("UNSTEPPED_NO_AGENT") else home_app()
        env = {**os.environ, "UNSTEPPED_NO_AGENT": "1", **({"UNSTEPPED_HOME_APP": str(HOME_APP)} if exe else {})}
        with open(STATE / "home.log", "ab") as log:
            subprocess.Popen(([str(exe)] if exe else []) + ["/usr/bin/python3", str(ROOT / "home.py"), "serve", "--no-open"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(ROOT), env=env)
    for _ in range(80):
        time.sleep(0.25)
        try:
            url = (STATE / ".server").read_text().strip()
            urllib.request.urlopen(url.split("?")[0] + "api/state?" + url.split("?", 1)[1], timeout=2).read()
            return url
        except (OSError, IndexError):
            continue
    return None


def gate(open_browser=True, port=GATE_PORT):
    """The sign-in page a new Mac sees first: Request access (name and work email; the company's owner is emailed a
    code for them), then the code. Nothing of the app is on the Mac until the sign-in succeeds: then it downloads the
    app, sets Home to start at login, starts it and hands over (the page goes to Home)."""
    import secrets as _s
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit
    tok = _s.token_urlsafe(24)
    job = {"phase": "idle", "message": ""}
    asked = {}   # this page's request to join, so a reload still shows the code step

    def work(email, code):
        try:
            job.update(phase="running", message="Signing in…")
            if state()["state"] != "signed-in":
                signin(email, code)
            job.update(message="Installing Unstepped Studio…")
            if not (ROOT / "home.py").exists() or _older(version(), load().get("latest") or "0.0.0"):
                update(force=True)
            job.update(message="Starting Home…")
            url = start_home()
            if not url:
                raise CloudError(0, "home", "Home didn't start: see ~/unstepped-review/home/home.log")
            job.update(phase="done", message="You're in.", url=url)
            threading.Timer(15, srv.shutdown).start()
        except CloudError as e:
            job.update(phase="failed", message=e.message)
        except Exception as e:   # never leave the page waiting
            job.update(phase="failed", message=f"{type(e).__name__}: {e}")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def ok(self):
            return parse_qs(urlsplit(self.path).query).get("t", [""])[0] == tok

        def do_GET(self):
            if not self.ok():
                return self.send(403, {"error": "open the address the installer printed"})
            p = urlsplit(self.path).path
            if p == "/":
                return self.send(200, GATE_HTML.encode(), "text/html; charset=utf-8")
            if p == "/status":
                return self.send(200, {**job, "ask": asked})
            return self.send(404, {})

        def do_POST(self):
            p = urlsplit(self.path).path
            if not self.ok() or p not in ("/signin", "/request"):
                return self.send(403, {"error": "not allowed"})
            if job["phase"] == "running":
                return self.send(409, {"error": "Already signing in."})
            b = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            if p == "/request":
                name, email = str(b.get("name") or "").strip(), str(b.get("email") or "").strip()
                if not name or not re.match(r"^[^\s@]+@[^\s@]+\.[a-z]{2,}$", email, re.I):
                    return self.send(400, {"error": "Enter your name and your work email."})
                try:
                    r = request_access(email, name)
                except CloudError as e:
                    return self.send(400, {"error": e.message or "The request didn't go through. Check the internet connection and try again."})
                asked.clear()
                asked.update(name=name, email=email, org=r.get("org") or "your company", emailed=bool(r.get("emailed")))
                return self.send(200, asked)
            if not re.match(r"^[^\s@]+@[^\s@]+\.[a-z]{2,}$", b.get("email") or "", re.I) or len(re.sub(r"[^A-Za-z0-9]", "", b.get("code") or "")) < 8:
                return self.send(400, {"error": "Enter your work email and the code from your admin."})
            threading.Thread(target=work, args=(b["email"], b["code"]), daemon=True).start()
            return self.send(200, {"ok": True})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}/?t={tok}"
    print(url, flush=True)
    if open_browser:
        subprocess.run(["open", url])
    srv.serve_forever()


APP_FILES = ("home.py", "review.py", "style.py", "templates.py", "shared.py", "cloud.py", "deploy.py", "macsetup.py", "studio.py", "rules.py", "split.py", "lib", "LICENSE-cooked", "NOTICE-cooked")


def publish(ver, notes=""):
    """Zip this app (the files every Mac runs, never home/ or anything personal) as version ver and publish it.
    Every signed-in Mac installs it on its next check-in; a new Mac gets it when it signs in."""
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
            elif s.get("just_updated"):
                print(f"Unstepped Studio updated to {s['just_updated']}. Home restarts on it by itself.")
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
        elif cmd == "gate":
            gate(open_browser="--open" in a)   # the installing session shows it in the Claude app's built-in browser
        elif cmd == "start":
            print(start_home() or "Home didn't start")
        elif cmd == "handover":   # (Home's helper: reload the login item, inside Unstepped Home.app)
            sys.exit(0 if handover() else 1)
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
