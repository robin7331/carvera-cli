"""carvera - manage G-code projects on a Carvera HMI (a Linux box / Raspberry Pi at the machine).

Files are stored on the HMI under ~/<gcode_dir>/<project>/ and show up in the Carvera Controller's
file browser under "Local". From there the operator uploads them to the machine's SD card and
runs them at the machine. This tool never moves the machine or starts jobs.

Transport: plain SSH. How the HMI is reached (LAN, VPN, Tailscale, ...) is configured with
`carvera setup` and stored in ~/.config/carvera/config.json.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from importlib import resources

from . import __version__

CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "carvera", "config.json"
)
DEFAULTS = {"host": None, "port": None, "identity_file": None, "gcode_dir": "gcode"}
GCODE_EXT = {".nc", ".gcode", ".gc", ".ngc", ".cnc", ".tap", ".txt"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+()-]{0,79}$")
DIR_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
SKILL_TARGETS = {
    "claude": os.path.expanduser("~/.claude/skills"),
    "agents": os.path.expanduser("~/.agents/skills"),
}

EXIT_USAGE, EXIT_UNREACHABLE, EXIT_NOT_FOUND, EXIT_EXISTS, EXIT_FAIL, EXIT_NOCONF = 2, 3, 4, 5, 1, 6


class CliError(Exception):
    def __init__(self, code, message, hint=None, exit_code=EXIT_FAIL):
        super().__init__(message)
        self.code, self.message, self.hint, self.exit_code = code, message, hint, exit_code


# ---------------------------------------------------------------- config

def load_config(required=True):
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as fh:
            cfg.update(json.load(fh))
    if os.environ.get("CARVERA_HOST"):
        cfg["host"] = os.environ["CARVERA_HOST"]
    if required and not cfg.get("host"):
        raise CliError("not_configured", "carvera is not set up yet (no HMI host configured)",
                       "Run `carvera setup` (interactive) or "
                       "`carvera setup --host <ssh-alias|user@host> [--port N] [--identity KEY]`.",
                       EXIT_NOCONF)
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({k: cfg.get(k) for k in DEFAULTS}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)


def ssh_argv(cfg):
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    if cfg.get("port"):
        argv += ["-p", str(cfg["port"])]
    if cfg.get("identity_file"):
        argv += ["-i", os.path.expanduser(cfg["identity_file"]), "-o", "IdentitiesOnly=yes"]
    return argv + [cfg["host"]]


def ssh(cfg, cmd, stdin_data=None, timeout=30, raw=False):
    """Run a shell command on the HMI. Returns stdout. Raises CliError on failure."""
    try:
        r = subprocess.run(ssh_argv(cfg) + [cmd], input=stdin_data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CliError("unreachable", f"SSH to {cfg['host']} timed out",
                       "Is the HMI powered on and reachable from this computer? (`carvera setup --show`)",
                       EXIT_UNREACHABLE)
    if r.returncode == 255:
        raise CliError("unreachable", f"cannot reach {cfg['host']}: {r.stderr.decode().strip()}",
                       "Check the HMI is on and reachable; re-run `carvera setup` if the address changed.",
                       EXIT_UNREACHABLE)
    if r.returncode != 0:
        raise CliError("remote_error", r.stderr.decode().strip() or f"remote command failed ({r.returncode})")
    return r.stdout if raw else r.stdout.decode()


def check_name(kind, name):
    if not NAME_RE.match(name) or ".." in name:
        raise CliError("bad_name", f"invalid {kind} name: {name!r}",
                       "Use letters, digits, spaces and . _ - + ( ); max 80 chars; no slashes.",
                       EXIT_USAGE)
    return name


def remote_dir(cfg, project=None):
    return f"{cfg['gcode_dir']}/{project}" if project else cfg["gcode_dir"]


LIST_PY = r'''
import json, os, sys, time
base = os.path.expanduser(sys.argv[1])
if not os.path.isdir(base):
    print(json.dumps({"missing": True})); sys.exit(0)
dirs, files = [], []
for e in sorted(os.scandir(base), key=lambda e: e.name.lower()):
    if e.name.startswith("."):
        continue
    st = e.stat()
    item = {"name": e.name, "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))}
    if e.is_dir():
        item["files"] = sum(1 for f in os.scandir(e.path) if f.is_file() and not f.name.startswith("."))
        dirs.append(item)
    else:
        item["size"] = st.st_size
        files.append(item)
print(json.dumps({"dirs": dirs, "files": files}))
'''


def remote_list(cfg, project):
    q = shlex.quote
    data = json.loads(ssh(cfg, f"python3 -I -c {q(LIST_PY)} {q('~/' + remote_dir(cfg, project))}"))
    if data.get("missing"):
        if project:
            raise CliError("not_found", f"project {project!r} does not exist",
                           "List projects with `carvera projects`, create with `carvera new`.",
                           EXIT_NOT_FOUND)
        return {"dirs": [], "files": []}
    return data


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ---------------------------------------------------------------- setup / skill

def prompt(label, default=None):
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def cmd_setup(a):
    cfg = load_config(required=False)
    if a.show:
        return {"config_path": CONFIG_PATH, "config": cfg}, lambda r: (
            f"Config: {r['config_path']}\n" + "\n".join(f"  {k}: {v}" for k, v in r["config"].items()))

    interactive = sys.stdin.isatty() and not a.json and a.host is None
    if interactive:
        print("Carvera HMI connection setup. The HMI is reached over SSH; use an alias from\n"
              "~/.ssh/config, or user@host / user@ip (LAN, VPN or Tailscale name all work).\n")
        cfg["host"] = prompt("SSH target", cfg.get("host"))
        port = prompt("SSH port (empty = default/ssh config)", str(cfg["port"]) if cfg.get("port") else None)
        cfg["port"] = int(port) if port else None
        cfg["identity_file"] = prompt("Private key file (empty = default/ssh config)", cfg.get("identity_file"))
        cfg["gcode_dir"] = prompt("Project folder on the HMI, relative to home", cfg.get("gcode_dir"))
    else:
        for key, val in (("host", a.host), ("port", a.port), ("identity_file", a.identity),
                         ("gcode_dir", a.gcode_dir)):
            if val is not None:
                cfg[key] = val or None
        cfg["gcode_dir"] = cfg.get("gcode_dir") or "gcode"
    if not cfg.get("host"):
        raise CliError("bad_config", "an SSH target is required", "Pass --host <ssh-alias|user@host>.", EXIT_USAGE)
    if not DIR_RE.match(cfg["gcode_dir"]) or ".." in cfg["gcode_dir"]:
        raise CliError("bad_config", f"invalid project folder: {cfg['gcode_dir']!r}",
                       "Use a relative path like `gcode` or `cnc/gcode`.", EXIT_USAGE)

    result = {"config_path": CONFIG_PATH, "config": cfg, "tested": False}
    if not a.no_test:
        q = shlex.quote
        out = ssh(cfg, f"cd ~ && mkdir -p {q(cfg['gcode_dir'])} && command -v python3 >/dev/null "
                       f"&& echo ok:$(hostname) || echo nopython")
        if out.strip() == "nopython":
            raise CliError("remote_error", "python3 is missing on the HMI", "Install python3 there.")
        result.update(tested=True, remote_hostname=out.strip().split(":", 1)[1])
    save_config(cfg)

    if interactive and not a.no_skill:
        ans = input("\nInstall the agent skill for Claude Code / other agents? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            result["skill"] = install_skill(["claude", "agents"], link=False)

    def text(r):
        lines = [f"Saved {r['config_path']}"]
        if r["tested"]:
            lines.append(f"Connection OK: reached {r['remote_hostname']}, ~/{r['config']['gcode_dir']} ready")
        for s in r.get("skill", []):
            lines.append(f"Skill installed: {s}")
        return "\n".join(lines)
    return result, text


def skill_source():
    return resources.files("carvera_cli").joinpath("skill", "SKILL.md")


def install_skill(targets, link):
    done = []
    src = skill_source()
    for t in targets:
        dest_dir = os.path.join(SKILL_TARGETS[t], "carvera")
        if os.path.islink(dest_dir) or os.path.isfile(dest_dir):
            os.remove(dest_dir)
        if link:
            os.makedirs(SKILL_TARGETS[t], exist_ok=True)
            os.symlink(os.path.dirname(str(src)), dest_dir)
        else:
            os.makedirs(dest_dir, exist_ok=True)
            with resources.as_file(src) as p:
                shutil.copyfile(p, os.path.join(dest_dir, "SKILL.md"))
        done.append(dest_dir)
    return done


def cmd_skill(a):
    if a.action == "show":
        with resources.as_file(skill_source()) as p:
            content = open(p).read()
        return {"path": str(skill_source()), "content": content}, lambda r: r["content"]
    targets = list(SKILL_TARGETS) if a.target == "all" else [a.target]
    if a.action == "uninstall":
        removed = []
        for t in targets:
            d = os.path.join(SKILL_TARGETS[t], "carvera")
            if os.path.islink(d):
                os.remove(d); removed.append(d)
            elif os.path.isdir(d):
                shutil.rmtree(d); removed.append(d)
        return {"removed": removed}, lambda r: "\n".join(f"removed {d}" for d in r["removed"]) or "nothing installed"
    return {"installed": install_skill(targets, a.link)}, lambda r: "\n".join(
        f"installed {d}" for d in r["installed"])


# ---------------------------------------------------------------- project commands

def cmd_projects(a):
    cfg = load_config()
    d = remote_list(cfg, None)
    return {"projects": d["dirs"], "inbox_files": d["files"]}, lambda r: "\n".join(
        [f"{p['name']}/  ({p['files']} files, {p['modified']})" for p in r["projects"]]
        + [f"(inbox, no project) {f['name']}  {human(f['size'])}" for f in r["inbox_files"]]
    ) or "No projects yet. Create one with: carvera new <name>"


def cmd_ls(a):
    cfg = load_config()
    project = check_name("project", a.project) if a.project else None
    d = remote_list(cfg, project)
    res = {"project": project, "path": "~/" + remote_dir(cfg, project), "files": d["files"], "dirs": d["dirs"]}

    def text(r):
        lines = [f"{f['name']}  {human(f['size'])}  {f['modified']}" for f in r["files"]]
        if not project:
            lines += [f"{x['name']}/" for x in r["dirs"]]
        return "\n".join(lines) or "(empty)"
    return res, text


def cmd_new(a):
    cfg = load_config()
    project = check_name("project", a.project)
    q = shlex.quote
    d = remote_dir(cfg, project)
    out = ssh(cfg, f"cd ~ && if [ -d {q(d)} ]; then echo exists; else mkdir -p {q(d)} && echo created; fi").strip()
    return {"project": project, "path": "~/" + d, "created": out == "created"}, \
        lambda r: f"{'Created' if r['created'] else 'Already exists'}: {r['path']}"


def cmd_send(a):
    cfg = load_config()
    files = [os.path.abspath(f) for f in a.files]
    for f in files:
        if not os.path.isfile(f):
            raise CliError("not_found", f"no such file: {f}", exit_code=EXIT_NOT_FOUND)
    warnings, sent = [], []
    q = shlex.quote
    for f in files:
        name = check_name("file", os.path.basename(f))
        if a.project_from_dir:
            project = check_name("project", os.path.basename(os.path.dirname(f)))
        else:
            project = check_name("project", a.project) if a.project else None
        if os.path.splitext(name)[1].lower() not in GCODE_EXT:
            warnings.append(f"{name}: unusual extension for G-code")
        dest_dir = remote_dir(cfg, project)
        dest = f"{dest_dir}/{name}"
        script = (f"cd ~ && mkdir -p {q(dest_dir)} && "
                  f"if [ -e {q(dest)} ] && [ {'1' if a.force else '0'} = 0 ]; then echo EXISTS; exit 0; fi && "
                  f"cat > {q(dest + '.part')} && mv -f {q(dest + '.part')} {q(dest)} && echo OK")
        with open(f, "rb") as fh:
            data = fh.read()
        out = ssh(cfg, script, stdin_data=data, timeout=300).strip()
        if out == "EXISTS":
            raise CliError("exists", f"~/{dest} already exists on the HMI",
                           "Pass --force to overwrite, or rename the file.", EXIT_EXISTS)
        sent.append({"file": f, "project": project, "dest": "~/" + dest, "size": len(data)})
    if a.notify and shutil.which("notify-send"):
        names = ", ".join(os.path.basename(s["file"]) for s in sent)
        subprocess.run(["notify-send", "-i", "document-send", "Sent to Carvera", names], stderr=subprocess.DEVNULL)
    return {"sent": sent, "warnings": warnings}, lambda r: "\n".join(
        [f"sent {os.path.basename(s['file'])} -> {s['dest']}" for s in r["sent"]]
        + [f"warning: {w}" for w in r["warnings"]])


def cmd_rm(a):
    cfg = load_config()
    project = check_name("project", a.project)
    q = shlex.quote
    if a.file:
        target = f"{remote_dir(cfg, project)}/{check_name('file', a.file)}"
        out = ssh(cfg, f"cd ~ && if [ -f {q(target)} ]; then rm -f {q(target)} && echo OK; else echo MISSING; fi").strip()
    else:
        target = remote_dir(cfg, project)
        out = ssh(cfg, f"cd ~ && if [ -d {q(target)} ]; then rmdir {q(target)} 2>/dev/null && echo OK || "
                       f"echo NOTEMPTY; else echo MISSING; fi").strip()
    if out == "MISSING":
        raise CliError("not_found", f"~/{target} does not exist", exit_code=EXIT_NOT_FOUND)
    if out == "NOTEMPTY":
        raise CliError("not_empty", f"project {project!r} still contains files",
                       "Remove the files first with `carvera rm <project> <file>`.")
    return {"removed": "~/" + target}, lambda r: f"removed {r['removed']}"


STATUS_SH = r'''
up=$(cut -d. -f1 /proc/uptime)
ctl=$(pgrep -f "[c]arveracontroller" >/dev/null && echo true || echo false)
log=$(ls -t ~/.kivy/logs/*.txt 2>/dev/null | head -1)
last=""; ver=""
if [ -n "$log" ]; then
  last=$(grep -E "Connected to machine|[Dd]isconnect|Connection (lost|closed)|Connecting via" "$log" | grep -iv pendant | tail -1 | cut -c1-200)
  ver=$(grep -oE "version = [0-9][^ ]*" "$log" | tail -1 | cut -d" " -f3)
fi
dev=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | tr "\n" " ")
pend=$(grep -l 10CE /sys/class/hidraw/*/device/uevent 2>/dev/null | wc -l)
python3 -I -c "import json,sys; a=sys.argv[1:]; print(json.dumps({'hmi_uptime_s':int(a[0]),'controller_running':a[1]=='true','last_connection_event':a[2],'firmware':a[3],'usb_serial':a[4].split(),'pendant_receiver':a[5]!='0'}))" "$up" "$ctl" "$last" "$ver" "$dev" "$pend"
'''


def cmd_status(a):
    cfg = load_config()
    st = json.loads(ssh(cfg, STATUS_SH))
    st["machine_connected"] = st["controller_running"] and "Connected to machine" in st["last_connection_event"]

    def text(r):
        up = r["hmi_uptime_s"]
        return "\n".join([
            f"HMI:         online (up {up // 3600}h {(up % 3600) // 60}m)",
            f"Controller:  {'running' if r['controller_running'] else 'NOT running'}",
            f"Machine:     {'connected' if r['machine_connected'] else 'not connected'}"
            + (f" (firmware {r['firmware']})" if r["firmware"] else ""),
            f"USB serial:  {' '.join(r['usb_serial']) or 'none'}",
            f"Pendant:     {'receiver present' if r['pendant_receiver'] else 'no receiver'}",
        ])
    return st, text


SHOT_SH = r'''
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-$(cd "$XDG_RUNTIME_DIR" 2>/dev/null && ls wayland-* 2>/dev/null | grep -v lock | head -1)}
exec grim -
'''


def cmd_screenshot(a):
    cfg = load_config()
    out = a.out or f"carvera-hmi-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    data = ssh(cfg, SHOT_SH, raw=True)
    if not data.startswith(b"\x89PNG"):
        raise CliError("screenshot_failed", "no image returned",
                       "The HMI needs a running Wayland session and `grim` installed.")
    with open(out, "wb") as fh:
        fh.write(data)
    return {"path": os.path.abspath(out)}, lambda r: r["path"]


# ---------------------------------------------------------------- machine queries (read-only)
#
# Route: Carvera Controller holds the machine's only link (USB serial), the machine isn't on the
# network, and the controller has no API or plugin hook. But the controller already does what we
# need and leaves the results on the HMI:
#   * on every connect it runs `download /sd/config.txt` into its temp dir
#     (/tmp/tmpXXXX/config_<connection>.txt), and
#   * it logs every command it sends (`MDI Sent: …`) and every machine reply except status polls
#     (`MDI Received: …`) to ~/.kivy/logs/.
# So these queries read those two files and nothing else. They send no bytes to the machine or the
# controller and can run at any time, even mid-job. The log tells us when the cached copy went
# stale (a `config-set` / `config-restore` sent after the download).
#
# Enforced in code: the HMI-side script only runs operations named in MACHINE_QUERIES (checked
# here and again on the HMI), and it only opens files read-only. There is no code path here that
# writes to the serial port, the controller, or the SD card.

MACHINE_QUERIES = ("config", "info")
MODEL_NAMES = {"CA1": "Carvera Air", "C1": "Carvera"}

MACHINE_PY = r'''
import glob, json, os, re, sys
OPS = ("config", "info")
op = sys.argv[1] if len(sys.argv) > 1 else ""
if op not in OPS:
    print(json.dumps({"refused": op})); sys.exit(0)

def start_time(pid):
    with open("/proc/stat") as f:
        btime = next(int(l.split()[1]) for l in f if l.startswith("btime"))
    with open("/proc/%s/stat" % pid) as f:
        ticks = int(f.read().rsplit(")", 1)[1].split()[19])
    return btime + ticks / os.sysconf("SC_CLK_TCK")

starts = []
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            if b"carveracontroller" in f.read():
                starts.append(start_time(pid))
    except (OSError, StopIteration, IndexError, ValueError):
        pass
out = {"controller_running": bool(starts), "controller_started": min(starts) if starts else None}

# --- controller log: the current session (from the last connect attempt on)
logs = sorted(glob.glob(os.path.expanduser("~/.kivy/logs/*.txt")), key=os.path.getmtime)
lines = []
if logs and starts and os.path.getmtime(logs[-1]) >= min(starts) - 5:
    with open(logs[-1], errors="replace") as f:
        for raw in f:
            m = re.match(r"^\[(\w+)\s*\] (MDI Sent|MDI Received): ?(.*)$", raw.rstrip("\n"))
            if m and m.group(3).strip():
                lines.append((m.group(1), m.group(2) == "MDI Sent", m.group(3).strip()))
conn_i = max([i for i, l in enumerate(lines) if l[2].startswith("Connecting via")] or [-1])
sess = lines[conn_i:] if conn_i >= 0 else []
s = {"connection": None, "connected": False, "model": None, "firmware": None}
dl_i = -1
for i, (lvl, sent, text) in enumerate(sess):
    if text.startswith("Connecting via"):
        s["connection"] = text.split("via", 1)[1].strip()
    elif text == "Connected to machine!":
        s["connected"] = True
    elif text.startswith("Disconnected via") or text.startswith("Connection lost"):
        s["connected"] = False
    elif not sent and text.startswith("model = "):
        s["model"] = text[8:].split(",")[0].strip()
    elif not sent and text.startswith("version = "):
        s["firmware"] = text[10:].strip()
    elif sent and text == "download /sd/config.txt":
        dl_i = i
after = sess[dl_i + 1:] if dl_i >= 0 else []
s["config_set_after_cache"] = []
s["config_replaced_after_cache"] = None
for lvl, sent, text in after:
    p = text.split()
    if sent and len(p) >= 4 and p[0] == "config-set" and p[1] == "sd":
        s["config_set_after_cache"].append([p[2], " ".join(p[3:])])
    elif sent and p and p[0] in ("config-restore", "config-default"):
        s["config_replaced_after_cache"] = p[0]
jobs = [t for l, sent, t in sess if sent and t.split()[0] in ("play", "suspend", "resume", "abort")]
s["last_job_command"] = jobs[-1].replace("\x01", " ") if jobs else None
errs = [(i, t) for i, (l, sent, t) in enumerate(sess) if not sent and (l == "ERROR" or "alarm" in t.lower())]
s["recent_errors"] = [t for i, t in errs[-5:]]
last_err = errs[-1][0] if errs else -1
s["unlocked_since_last_error"] = bool(errs) and any(
    sent and t.upper() == "$X" for l, sent, t in sess[last_err + 1:])
wcs = {}
for l, sent, t in sess:
    m = re.match(r"^\[(G5[4-9](?:\.\d)?|G28|G30|G92|TLO|PRB)\s*:\s*([^\]]*)\]", t)
    if not sent and m:
        wcs[m.group(1)] = m.group(2)
s["reported_offsets"] = wcs
out["session"] = s

# --- the controller's cached copy of /sd/config.txt
if op == "config":
    cands = [p for p in glob.glob("/tmp/tmp*/config_*.txt")
             if os.stat(p).st_uid == os.getuid() and starts and os.path.getmtime(p) >= min(starts) - 5]
    if cands:
        path = max(cands, key=os.path.getmtime)
        values = {}
        with open(path, errors="replace") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if line:
                    kv = line.split(None, 1)
                    values[kv[0]] = kv[1].strip() if len(kv) > 1 else ""
        out["config"] = {"cache_path": path, "cached_at": os.path.getmtime(path), "values": values}
print(json.dumps(out))
'''


def machine_query(cfg, op):
    if op not in MACHINE_QUERIES:
        raise CliError("refused", f"machine query {op!r} is not on the read-only allowlist")
    q = shlex.quote
    d = json.loads(ssh(cfg, f"python3 -I -c {q(MACHINE_PY)} {q(op)}"))
    if "refused" in d:
        raise CliError("refused", f"the HMI refused machine query {d['refused']!r}")
    return d


def ts(t):
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else None


def need_controller(d):
    if not d["controller_running"]:
        raise CliError("controller_not_running", "Carvera Controller is not running on the HMI",
                       "Start the controller at the machine and connect it, then retry.")


def machine_config(cfg):
    """Return (values, source, warnings) from the controller's copy of /sd/config.txt."""
    d = machine_query(cfg, "config")
    need_controller(d)
    s, c = d["session"], d.get("config")
    if not c:
        raise CliError("no_config",
                       "the controller has no copy of the machine config yet (machine not connected?)",
                       "Connect the machine in Carvera Controller; it reads /sd/config.txt on connect.")
    if s["config_replaced_after_cache"]:
        raise CliError("config_stale",
                       f"`{s['config_replaced_after_cache']}` was sent after the controller read the config, "
                       "so its copy is out of date",
                       "Reset the machine and reconnect it in Carvera Controller, then retry.")
    values, pending, warnings = dict(c["values"]), {}, []
    for key, new in s["config_set_after_cache"]:
        pending[key] = {"cached": values.get(key), "set": new}
        values[key] = new
    for key, p in pending.items():
        warnings.append(f"{key} was changed with config-set after the controller read the config "
                        f"({p['cached']} -> {p['set']}); showing the new value, which the machine "
                        "uses after its next reset")
    source = {"file": "/sd/config.txt", "via": "Carvera Controller's copy, read on connect",
              "cache_path": c["cache_path"], "read_at": ts(c["cached_at"]),
              "machine_connected": s["connected"], "connection": s["connection"],
              "model": s["model"], "firmware": s["firmware"], "changed_since_read": pending}
    if not s["connected"]:
        warnings.append("the machine is not connected right now; values are from the last connection")
    return values, source, warnings


def config_text(r):
    width = max((len(k) for k in r["values"]), default=0)
    lines = [f"{k:<{width}}  {v}" for k, v in r["values"].items()]
    return "\n".join(lines + [f"warning: {w}" for w in r["warnings"]])


def cmd_config(a):
    cfg = load_config()
    values, source, warnings = machine_config(cfg)
    if a.action == "dump":
        return {"values": values, "source": source, "warnings": warnings}, config_text
    missing = [k for k in a.keys if k not in values]
    if missing:
        raise CliError("not_found", f"not in /sd/config.txt: {', '.join(missing)}",
                       "Keys are case-sensitive, e.g. coordinate.anchor1_x; list them all with "
                       "`carvera config dump`.", EXIT_NOT_FOUND)
    return {"values": {k: values[k] for k in a.keys}, "source": source, "warnings": warnings}, config_text


def cmd_machine(a):
    cfg = load_config()
    d = machine_query(cfg, "info")
    need_controller(d)
    s = d["session"]
    res = {
        "controller_running": True,
        "machine_connected": s["connected"],
        "connection": s["connection"],
        "model": s["model"],
        "model_name": MODEL_NAMES.get(s["model"], s["model"]),
        "firmware": s["firmware"],
        "community_firmware": bool(s["firmware"]) and "c" in s["firmware"].lower(),
        "last_job_command": s["last_job_command"],
        "recent_errors": s["recent_errors"],
        "unlocked_since_last_error": s["unlocked_since_last_error"],
        "reported_offsets": s["reported_offsets"],
        "live_state": None,
        "live_state_note": "Position, work offsets, active tool/TLO and alarm state come from the "
                           "machine's status replies, which only the controller sees and doesn't log. "
                           "Read them on the HMI screen (`carvera screenshot`). reported_offsets holds "
                           "offsets the machine last printed in the MDI log (after `get wcs` / `$#`), "
                           "if any.",
    }

    def text(r):
        lines = [
            f"Machine:     {r['model_name'] or 'unknown'}"
            + (f" ({r['model']})" if r["model"] and r["model_name"] != r["model"] else "")
            + f", {'connected' if r['machine_connected'] else 'NOT connected'}"
            + (f" via {r['connection']}" if r["connection"] else ""),
            f"Firmware:    {r['firmware'] or 'unknown'}" + (" (community)" if r["community_firmware"] else ""),
            f"Last job cmd: {r['last_job_command'] or 'none this session'}",
        ]
        if r["recent_errors"]:
            lines.append("Errors/alarms this session" + (" (unlocked since)" if r["unlocked_since_last_error"] else "")
                         + ":")
            lines += [f"  {e}" for e in r["recent_errors"]]
        for k, v in r["reported_offsets"].items():
            lines.append(f"Last reported {k}: {v}")
        lines.append("Live position/tool/alarm: not available (see --json live_state_note)")
        return "\n".join(lines)
    return res, text


# ---------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(
        prog="carvera",
        description="Manage G-code projects on a Carvera HMI over SSH. Files land in ~/<gcode_dir>/<project>/ "
                    "on the HMI and appear under 'Local' in the Carvera Controller. Never moves the machine.",
    )
    p.add_argument("--json", action="store_true", help="print exactly one JSON document")
    p.add_argument("--version", action="version", version=f"carvera {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("setup", help="configure how to reach the HMI (SSH) and test it")
    sp.add_argument("--host", help="SSH target: alias from ~/.ssh/config, user@host or user@ip")
    sp.add_argument("--port", type=int, help="SSH port (omit for default / ssh config)")
    sp.add_argument("--identity", help="private key file (omit for default / ssh config)")
    sp.add_argument("--gcode-dir", help="project folder on the HMI, relative to home (default: gcode)")
    sp.add_argument("--no-test", action="store_true", help="save without testing the connection")
    sp.add_argument("--no-skill", action="store_true", help="don't offer to install the agent skill")
    sp.add_argument("--show", action="store_true", help="print the current configuration")
    sp.set_defaults(fn=cmd_setup)

    sp = sub.add_parser("skill", help="install/show the agent skill (Claude Code, ~/.agents)")
    sp.add_argument("action", choices=["install", "uninstall", "show"])
    sp.add_argument("--target", choices=["all", "claude", "agents"], default="all")
    sp.add_argument("--link", action="store_true", help="symlink instead of copy (for development)")
    sp.set_defaults(fn=cmd_skill)

    sp = sub.add_parser("projects", help="list projects (folders) on the HMI")
    sp.set_defaults(fn=cmd_projects)

    sp = sub.add_parser("ls", help="list files in a project (or the inbox if omitted)")
    sp.add_argument("project", nargs="?")
    sp.set_defaults(fn=cmd_ls)

    sp = sub.add_parser("new", help="create a project folder")
    sp.add_argument("project")
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("send", help="copy G-code files to the HMI")
    sp.add_argument("files", nargs="+")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("-p", "--project", help="target project (created if missing); default: inbox")
    g.add_argument("--project-from-dir", action="store_true",
                   help="use each file's local parent folder name as the project")
    sp.add_argument("-f", "--force", action="store_true", help="overwrite an existing file")
    sp.add_argument("--notify", action="store_true", help="show a desktop notification")
    sp.set_defaults(fn=cmd_send)

    sp = sub.add_parser("rm", help="remove a file from a project, or an empty project")
    sp.add_argument("project")
    sp.add_argument("file", nargs="?")
    sp.set_defaults(fn=cmd_rm)

    sp = sub.add_parser("status", help="HMI / controller / machine connection status")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("screenshot", help="save a screenshot of the HMI screen")
    sp.add_argument("out", nargs="?")
    sp.set_defaults(fn=cmd_screenshot)

    sp = sub.add_parser("config", help="read the machine's /sd/config.txt (read-only, sends nothing to the machine)")
    csub = sp.add_subparsers(dest="action", required=True)
    cp = csub.add_parser("get", help="value of one or more config keys, e.g. coordinate.anchor1_x")
    cp.add_argument("keys", nargs="+")
    csub.add_parser("dump", help="every key -> value (comments dropped)")
    sp.set_defaults(fn=cmd_config)

    sp = sub.add_parser("machine", help="machine model, firmware, connection (read-only)")
    sp.add_argument("action", choices=["info"])
    sp.set_defaults(fn=cmd_machine)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        data, text = a.fn(a)
        print(json.dumps({"ok": True, "command": a.cmd, "data": data}, indent=2) if a.json else text(data))
        return 0
    except CliError as e:
        if a.json:
            print(json.dumps({"ok": False, "command": a.cmd,
                              "error": {"code": e.code, "message": e.message, "hint": e.hint}}, indent=2))
        else:
            print(f"error: {e.message}" + (f"\nhint: {e.hint}" if e.hint else ""), file=sys.stderr)
        if getattr(a, "notify", False) and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", "Send to Carvera failed", e.message],
                           stderr=subprocess.DEVNULL)
        return e.exit_code
    except (KeyboardInterrupt, EOFError):
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
