# carvera-cli

Manage G-code projects on a [Carvera](https://www.makera.com/) CNC's touchscreen computer (the
**HMI**, e.g. a Raspberry Pi running
[Carvera Controller Community](https://github.com/Carvera-Community/Carvera_Controller)) from your
desktop — and let AI agents do it for you.

Files go over plain SSH to `~/gcode/<project>/` on the HMI and appear under **Local** in the
controller's file browser. From there you upload them to the machine and run them at the machine.
**The tool never moves the machine or starts jobs.**

## Install

```sh
uv tool install git+https://github.com/robin7331/carvera-cli
# or: pipx install git+https://github.com/robin7331/carvera-cli
```

Requires Python ≥ 3.9 locally and `python3` on the HMI (standard on Raspberry Pi OS). No other
dependencies.

## Setup

The HMI must be reachable with SSH **key authentication** from your computer. How you reach it —
LAN IP, hostname, VPN or a Tailscale name — doesn't matter to the tool.

```sh
ssh-copy-id pi@carvera-hmi.local      # once, if you don't have key auth yet
carvera setup                         # interactive: SSH target, port, key, folder; tests the connection
carvera setup --host my-hmi-alias     # or non-interactive (alias from ~/.ssh/config, user@host, user@ip)
carvera setup --show
```

Settings live in `~/.config/carvera/config.json`. `CARVERA_HOST` overrides the host for one call.

## Use

```sh
carvera projects                         # list projects
carvera new "Front panel"                # create a project
carvera send -p "Front panel" *.nc       # send files into it (refuses to overwrite without --force)
carvera send --project-from-dir ~/CNC/front-panel/roughing.nc   # project = local folder name
carvera ls "Front panel"
carvera rm "Front panel" roughing.nc     # remove a file; `carvera rm "Front panel"` removes an empty project
carvera status                           # HMI online? controller running? machine connected? pendant?
carvera screenshot hmi.png               # what the HMI screen shows (needs grim + Wayland on the HMI)
carvera config get coordinate.anchor1_x coordinate.anchor1_y   # machine config values (read-only)
carvera config dump                      # all of /sd/config.txt as key -> value
carvera machine info                     # model, firmware, connection, recent errors
```

Put `--json` before the subcommand for machine-readable output
(`{"ok": true, "command": …, "data": …}` / `{"ok": false, "error": {"code", "message", "hint"}}`).

## Agent skill

The package ships a skill that teaches agents (Claude Code and others reading `~/.agents/skills`)
to handle requests like *"send that to the Carvera"* or *"make a Carvera project for the front
panel"* — and never to start jobs.

```sh
carvera skill install            # copies into ~/.claude/skills/carvera and ~/.agents/skills/carvera
carvera skill install --link     # symlink instead (when developing from a checkout)
carvera skill uninstall
```

`carvera setup` offers to install it at the end.

## Status details

`status` reads the HMI side without touching the machine: whether a `carveracontroller` process
runs, the last connection event in the controller's Kivy log (`~/.kivy/logs`), the reported firmware
version, USB serial devices, and whether an XHC WHB04B/LHB04B pendant receiver is plugged in.

## Reading the machine

`config get`, `config dump` and `machine info` are read-only and send nothing to the machine. The
controller holds the machine's only link (USB serial) and has no API or plugin hook. But on every
connect it downloads `/sd/config.txt` into its temp directory
(`/tmp/tmpXXXX/config_<connection>.txt`), and it logs every command it sends and most replies to
`~/.kivy/logs`. These commands read those two files, so they work while the controller runs, even
mid-job. If the log shows a `config-set` after the download, `config get` returns the new value with
a warning; after `config-restore` / `config-default` it refuses (`config_stale`) until the machine
is reconnected.

Live position, work offsets, active tool and alarm state are only in status replies that the
controller neither logs nor exposes, so `machine info` doesn't report them. Use `carvera screenshot`.

## License

MIT
