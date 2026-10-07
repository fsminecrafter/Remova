#!/usr/bin/env python3
"""wifi-keyboard: type and move a mouse on this machine from a browser.

Input is injected through /dev/uinput, so it reaches whatever the kernel
console (tty1) or desktop session has focus. Needs root.
"""
import asyncio, ctypes, hashlib, hmac, json, os, pwd, secrets, shutil, time
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import quote

from aiohttp import web, WSMsgType
from evdev import UInput, ecodes as e

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text())


def find_files_user():
    """Account whose home folder is the default file root: whoever ran this, never root.
    Run directly -> the current user. Run with sudo -> SUDO_USER. Run as the systemd
    service -> the user setup.sh recorded as "files_user" in config.json."""
    names = []
    if os.geteuid() != 0:
        names.append(pwd.getpwuid(os.geteuid()).pw_name)
    else:
        names.append(os.environ.get("SUDO_USER"))
        uid = os.environ.get("PKEXEC_UID", "")
        if uid.isdigit():
            try:
                names.append(pwd.getpwuid(int(uid)).pw_name)
            except KeyError:
                pass
        names.append(CFG.get("files_user"))
    for name in names:
        try:
            user = pwd.getpwnam(name) if name else None
        except KeyError:
            continue
        if user and user.pw_uid != 0 and Path(user.pw_dir).is_dir():
            return user
    return None


FILES_USER = None if CFG.get("files_root") else find_files_user()
# File explorer root. Default: ~ of the user who ran it (never root's home). Set
# "files_root" in config.json to expose another folder; with no non-root user to
# fall back on (a real root login) it uses ./files.
if CFG.get("files_root"):
    FILE_ROOT = Path(CFG["files_root"]).expanduser()
elif FILES_USER:
    FILE_ROOT = Path(FILES_USER.pw_dir)
else:
    FILE_ROOT = BASE / "files"
SESSION_TTL = 12 * 3600
sessions: dict[str, float] = {}
fails: dict[str, list[float]] = {}

# ---------- key tables (KeyboardEvent.code -> evdev) ----------
KEYS = {}
for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    KEYS["Key" + c] = getattr(e, "KEY_" + c)
for d in "0123456789":
    KEYS["Digit" + d] = getattr(e, "KEY_" + d)
for n in range(1, 13):
    KEYS[f"F{n}"] = getattr(e, f"KEY_F{n}")
KEYS.update({
    "Enter": e.KEY_ENTER, "Escape": e.KEY_ESC, "Backspace": e.KEY_BACKSPACE,
    "Tab": e.KEY_TAB, "Space": e.KEY_SPACE, "Minus": e.KEY_MINUS,
    "Equal": e.KEY_EQUAL, "BracketLeft": e.KEY_LEFTBRACE,
    "BracketRight": e.KEY_RIGHTBRACE, "Backslash": e.KEY_BACKSLASH,
    "Semicolon": e.KEY_SEMICOLON, "Quote": e.KEY_APOSTROPHE,
    "Backquote": e.KEY_GRAVE, "Comma": e.KEY_COMMA, "Period": e.KEY_DOT,
    "Slash": e.KEY_SLASH, "IntlBackslash": e.KEY_102ND,
    "CapsLock": e.KEY_CAPSLOCK,
    "ShiftLeft": e.KEY_LEFTSHIFT, "ShiftRight": e.KEY_RIGHTSHIFT,
    "ControlLeft": e.KEY_LEFTCTRL, "ControlRight": e.KEY_RIGHTCTRL,
    "AltLeft": e.KEY_LEFTALT, "AltRight": e.KEY_RIGHTALT,
    "MetaLeft": e.KEY_LEFTMETA, "MetaRight": e.KEY_RIGHTMETA,
    "ArrowUp": e.KEY_UP, "ArrowDown": e.KEY_DOWN,
    "ArrowLeft": e.KEY_LEFT, "ArrowRight": e.KEY_RIGHT,
    "Home": e.KEY_HOME, "End": e.KEY_END, "PageUp": e.KEY_PAGEUP,
    "PageDown": e.KEY_PAGEDOWN, "Insert": e.KEY_INSERT, "Delete": e.KEY_DELETE,
})

# Characters for the phone soft keyboard (US layout assumed)
CHARS = {}
for c in "abcdefghijklmnopqrstuvwxyz":
    CHARS[c] = (getattr(e, "KEY_" + c.upper()), False)
    CHARS[c.upper()] = (getattr(e, "KEY_" + c.upper()), True)
for d, s in zip("1234567890", "!@#$%^&*()"):
    CHARS[d] = (getattr(e, "KEY_" + d), False)
    CHARS[s] = (getattr(e, "KEY_" + d), True)
for a, b, k in [("-", "_", e.KEY_MINUS), ("=", "+", e.KEY_EQUAL),
                ("[", "{", e.KEY_LEFTBRACE), ("]", "}", e.KEY_RIGHTBRACE),
                ("\\", "|", e.KEY_BACKSLASH), (";", ":", e.KEY_SEMICOLON),
                ("'", '"', e.KEY_APOSTROPHE), ("`", "~", e.KEY_GRAVE),
                (",", "<", e.KEY_COMMA), (".", ">", e.KEY_DOT),
                ("/", "?", e.KEY_SLASH)]:
    CHARS[a] = (k, False)
    CHARS[b] = (k, True)
CHARS[" "] = (e.KEY_SPACE, False)
CHARS["\n"] = (e.KEY_ENTER, False)

BUTTONS = [e.BTN_LEFT, e.BTN_MIDDLE, e.BTN_RIGHT]

# Key repeat. The kernel ignores a second "press" for a key that is already down, so
# holding a key only repeats if we emit repeat events (value 2) ourselves. The Linux
# console (tty1) needs them; X11/Wayland ignore them and repeat on their own.
# Tune with "repeat_delay_ms" / "repeat_period_ms" in config.json.
REPEAT_DELAY = CFG.get("repeat_delay_ms", 350) / 1000
REPEAT_PERIOD = CFG.get("repeat_period_ms", 35) / 1000
NO_REPEAT = {e.KEY_LEFTSHIFT, e.KEY_RIGHTSHIFT, e.KEY_LEFTCTRL, e.KEY_RIGHTCTRL,
             e.KEY_LEFTALT, e.KEY_RIGHTALT, e.KEY_LEFTMETA, e.KEY_RIGHTMETA, e.KEY_CAPSLOCK}

kbd = UInput({e.EV_KEY: list(set(KEYS.values()))}, name="wifi-keyboard")
mouse = UInput({e.EV_KEY: BUTTONS, e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL]},
               name="wifi-mouse")


def drop_file_privileges(user):
    """The server runs as root for /dev/uinput, but the file explorer should act as the
    user whose home it shows: files they create belong to them and root-only files stay
    out of reach. Changing only the filesystem uid keeps the open uinput devices working."""
    libc = ctypes.CDLL(None, use_errno=True)
    os.initgroups(user.pw_name, user.pw_gid)
    libc.setfsgid(user.pw_gid)
    libc.setfsuid(user.pw_uid)
    if libc.setfsuid(-1) != user.pw_uid:  # returns the current fsuid
        raise RuntimeError("could not switch file access to " + user.pw_name)


if FILES_USER and os.geteuid() == 0:
    drop_file_privileges(FILES_USER)
FILE_ROOT.mkdir(parents=True, exist_ok=True)


async def autorepeat(code):
    await asyncio.sleep(REPEAT_DELAY)
    while True:
        kbd.write(e.EV_KEY, code, 2)
        kbd.syn()
        await asyncio.sleep(REPEAT_PERIOD)


def tap(code, shift=False):
    if shift:
        kbd.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
    kbd.write(e.EV_KEY, code, 1)
    kbd.write(e.EV_KEY, code, 0)
    if shift:
        kbd.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
    kbd.syn()


# ---------- auth ----------
def check_password(pw: str) -> bool:
    got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(CFG["salt"]),
                         n=CFG.get("n", 2**14), r=8, p=1, dklen=32)
    return hmac.compare_digest(got.hex(), CFG["hash"])


def authed(req) -> bool:
    tok = req.cookies.get("wk")
    exp = sessions.get(tok or "")
    if exp and exp > time.time():
        return True
    sessions.pop(tok or "", None)
    return False


async def login(req):
    ip = req.remote or "?"
    now = time.time()
    recent = [t for t in fails.get(ip, []) if now - t < 300]
    fails[ip] = recent
    if len(recent) >= 5:
        return web.json_response({"error": "Too many attempts. Wait a few minutes."}, status=429)
    try:
        pw = (await req.json()).get("password", "")
    except Exception:
        pw = ""
    if not check_password(str(pw)):
        fails[ip].append(now)
        return web.json_response({"error": "Wrong password."}, status=401)
    fails.pop(ip, None)
    tok = secrets.token_urlsafe(32)
    sessions[tok] = now + SESSION_TTL
    res = web.json_response({"ok": True})
    res.set_cookie("wk", tok, httponly=True, samesite="Strict", max_age=SESSION_TTL)
    return res


async def logout(req):
    sessions.pop(req.cookies.get("wk", ""), None)
    return web.json_response({"ok": True})


async def me(req):
    return web.json_response({"ok": authed(req)})


# ---------- authenticated file explorer ----------
def file_path(value, allow_root=True):
    if not isinstance(value, str) or "\\" in value or "\0" in value:
        raise ValueError("Invalid path.")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in (".", "..") for part in relative.parts):
        raise ValueError("Invalid path.")
    if not value and not allow_root:
        raise ValueError("A file path is required.")
    target = FILE_ROOT.joinpath(*relative.parts)
    current = FILE_ROOT
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symbolic links are not supported.")
    try:
        target.resolve(strict=False).relative_to(FILE_ROOT.resolve())
    except ValueError:
        raise ValueError("Path is outside the file area.") from None
    return target


def file_entry(path):
    info = path.stat()
    return {"name": path.name, "path": path.relative_to(FILE_ROOT).as_posix(),
            "directory": path.is_dir(), "size": info.st_size,
            "modified": int(info.st_mtime)}


def files_error(message, status=400):
    return web.json_response({"error": message}, status=status)


def check_files_auth(req):
    """Return an error response if not logged in, otherwise None."""
    return None if authed(req) else files_error("Authentication required.", 401)


async def files_list(req):
    denied = check_files_auth(req)
    if denied:
        return denied
    try:
        folder = file_path(req.query.get("path", ""))
        if not folder.is_dir():
            return files_error("Folder not found.", 404)
        entries = [file_entry(path) for path in folder.iterdir() if not path.is_symlink()]
        entries.sort(key=lambda item: (not item["directory"], item["name"].casefold()))
        return web.json_response({"path": PurePosixPath(req.query.get("path", "")).as_posix()
                                  if req.query.get("path") else "", "root": str(FILE_ROOT.resolve()),
                                  "entries": entries})
    except (ValueError, OSError) as exc:
        return files_error(str(exc))


async def files_search(req):
    denied = check_files_auth(req)
    if denied:
        return denied
    query = req.query.get("q", "").strip().casefold()
    if not query:
        return web.json_response({"entries": []})
    matches = []
    for folder, directories, filenames in __import__("os").walk(FILE_ROOT, followlinks=False):
        directories[:] = [name for name in directories if not (Path(folder) / name).is_symlink()]
        for name in directories + filenames:
            path = Path(folder) / name
            if name.casefold().find(query) >= 0 and not path.is_symlink():
                try:
                    matches.append(file_entry(path))
                except OSError:
                    continue
                if len(matches) >= 500:
                    break
        if len(matches) >= 500:
            break
    return web.json_response({"entries": matches})


async def files_content(req):
    denied = check_files_auth(req)
    if denied:
        return denied
    try:
        target = file_path(req.query.get("path", ""), allow_root=False)
        if req.method == "GET":
            if not target.is_file():
                return files_error("File not found.", 404)
            if target.stat().st_size > 2 * 1024 * 1024:
                return files_error("Files over 2 MB cannot be opened in the editor.", 413)
            return web.json_response({"path": target.relative_to(FILE_ROOT).as_posix(),
                                      "content": target.read_text(encoding="utf-8")})
        data = await req.json()
        content = data.get("content")
        if not isinstance(content, str):
            return files_error("Text content is required.")
        target.write_text(content, encoding="utf-8")
        return web.json_response({"ok": True})
    except UnicodeDecodeError:
        return files_error("This file is not UTF-8 text.", 415)
    except (ValueError, OSError) as exc:
        return files_error(str(exc))


async def files_download(req):
    denied = check_files_auth(req)
    if denied:
        return denied
    try:
        target = file_path(req.query.get("path", ""), allow_root=False)
        if not target.is_file():
            return files_error("File not found.", 404)
        return web.FileResponse(target, headers={"Content-Disposition":
                                                  f"attachment; filename*=UTF-8''{quote(target.name)}"})
    except (ValueError, OSError) as exc:
        return files_error(str(exc), 404 if isinstance(exc, FileNotFoundError) else 400)


async def files_upload(req):
    denied = check_files_auth(req)
    if denied:
        return denied
    try:
        reader = await req.multipart()
        path_part = await reader.next()
        if path_part is None or path_part.name != "path":
            return files_error("Upload folder is required.")
        folder = file_path(await path_part.text())
        if not folder.is_dir():
            return files_error("Folder not found.", 404)
        saved = []
        total = 0
        while (part := await reader.next()) is not None:
            if not part.filename:
                continue
            name = part.filename.replace("\\", "/").split("/")[-1]
            if name in ("", ".", ".."):
                return files_error("Invalid upload filename.")
            target = file_path((folder.relative_to(FILE_ROOT) / name).as_posix(), allow_root=False)
            try:
                with target.open("xb") as output:
                    while chunk := await part.read_chunk(65536):
                        total += len(chunk)
                        if total > 100 * 1024 * 1024:
                            raise ValueError("Uploads are limited to 100 MB per request.")
                        output.write(chunk)
                saved.append(name)
            except FileExistsError:
                return files_error(f"{name} already exists.", 409)
        return web.json_response({"uploaded": saved})
    except (ValueError, OSError) as exc:
        return files_error(str(exc))


async def files_action(req):
    denied = check_files_auth(req)
    if denied:
        return denied
    try:
        data = await req.json()
        action = data.get("action")
        source = file_path(data.get("path", ""), allow_root=action in ("mkdir", "mkfile"))
        if not source.exists():
            return files_error("Item not found.", 404)
        if action == "delete":
            shutil.rmtree(source) if source.is_dir() else source.unlink()
        elif action == "rename":
            name = data.get("name", "")
            if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
                return files_error("Enter a valid name.")
            destination = file_path((source.parent.relative_to(FILE_ROOT) / name).as_posix(), allow_root=False)
            if destination.exists():
                return files_error("An item with that name already exists.", 409)
            source.rename(destination)
        elif action in ("move", "copy"):
            folder = file_path(data.get("destination", ""))
            if not folder.is_dir():
                return files_error("Destination folder not found.", 404)
            destination = folder / source.name
            file_path(destination.relative_to(FILE_ROOT).as_posix(), allow_root=False)
            if destination.exists():
                return files_error("An item with that name already exists.", 409)
            if source.is_dir() and (folder == source or source in folder.parents):
                return files_error("A folder cannot be moved or copied into itself.")
            if action == "move":
                shutil.move(str(source), str(destination))
            elif source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copy2(source, destination)
        elif action == "mkdir":
            folder = file_path(data.get("path", ""))
            name = data.get("name", "")
            if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
                return files_error("Enter a valid folder name.")
            destination = file_path((folder.relative_to(FILE_ROOT) / name).as_posix(), allow_root=False)
            destination.mkdir()
        elif action == "mkfile":
            folder = file_path(data.get("path", ""))
            name = data.get("name", "")
            if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
                return files_error("Enter a valid file name.")
            destination = file_path((folder.relative_to(FILE_ROOT) / name).as_posix(), allow_root=False)
            destination.touch(exist_ok=False)
        else:
            return files_error("Unknown file operation.")
        return web.json_response({"ok": True})
    except FileExistsError:
        return files_error("An item with that name already exists.", 409)
    except (ValueError, OSError) as exc:
        return files_error(str(exc))


# ---------- input socket ----------
async def ws_handler(req):
    if not authed(req):
        raise web.HTTPUnauthorized()
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    down = set()
    repeats = {}

    def stop_repeat(code=None):
        for c in ([code] if code is not None else list(repeats)):
            task = repeats.pop(c, None)
            if task:
                task.cancel()

    def release_all():
        stop_repeat()
        for k in list(down):
            kbd.write(e.EV_KEY, k, 0)
        down.clear()
        kbd.syn()
        for b in BUTTONS:
            mouse.write(e.EV_KEY, b, 0)
        mouse.syn()

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
                t = m["t"]
                if t == "k":  # key
                    code = KEYS.get(m["c"])
                    if code is None:
                        continue
                    if m["d"]:
                        down.add(code)
                    else:
                        down.discard(code)
                        stop_repeat(code)
                    kbd.write(e.EV_KEY, code, 1 if m["d"] else 0)
                    kbd.syn()
                    if m["d"] and code not in NO_REPEAT and code not in repeats:
                        repeats[code] = asyncio.create_task(autorepeat(code))
                elif t == "s":  # text from soft keyboard
                    for ch in str(m["s"])[:200]:
                        if ch in CHARS:
                            tap(*CHARS[ch])
                elif t == "m":  # relative move
                    mouse.write(e.EV_REL, e.REL_X, int(m["x"]))
                    mouse.write(e.EV_REL, e.REL_Y, int(m["y"]))
                    mouse.syn()
                elif t == "b":  # button
                    mouse.write(e.EV_KEY, BUTTONS[int(m["b"]) % 3], 1 if m["d"] else 0)
                    mouse.syn()
                elif t == "w":  # wheel
                    mouse.write(e.EV_REL, e.REL_WHEEL, -1 if m["y"] > 0 else 1)
                    mouse.syn()
                elif t == "r":
                    release_all()
            except (KeyError, ValueError, TypeError):
                continue
    finally:
        release_all()
    return ws


async def index(_):
    return web.FileResponse(BASE / "index.html")


app = web.Application()
app.add_routes([
    web.get("/", index),
    web.post("/api/login", login),
    web.post("/api/logout", logout),
    web.get("/api/me", me),
    web.get("/api/files", files_list),
    web.get("/api/files/search", files_search),
    web.get("/api/files/content", files_content),
    web.put("/api/files/content", files_content),
    web.get("/api/files/download", files_download),
    web.post("/api/files/upload", files_upload),
    web.post("/api/files/action", files_action),
    web.get("/ws", ws_handler),
])

if __name__ == "__main__":
    web.run_app(app, host=CFG.get("host", "0.0.0.0"), port=CFG.get("port", 8765))
