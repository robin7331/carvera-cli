---
name: carvera
description: Send G-code to a Carvera CNC and organize it in projects with the `carvera` CLI — create project folders on the Carvera HMI (the touchscreen computer at the machine running Carvera Controller), send/copy .nc/.gcode/.cnc files into them, list projects and files, remove files, check whether the HMI, controller and machine are online, take a screenshot of the HMI screen, and set up how the HMI is reached. Use when the user says things like "send that to the Carvera", "put this on the CNC", "make a Carvera project for …", "what's on the Carvera", "is the Carvera connected", "show me the Carvera screen", or "set up the carvera CLI". Never moves the machine or starts jobs.
---

# Carvera projects with `carvera`

A Carvera CNC is driven by a touchscreen computer next to it (the **HMI**, usually a Raspberry Pi)
running Carvera Controller. `carvera` puts files on that HMI over SSH:

```
~/<gcode_dir>/              on the HMI = "Local" in the controller's file browser
├── <project>/part.nc       one folder per project
└── loose.nc                files sent without a project (inbox)
```

At the machine the operator opens the file browser → **Local** → project → uploads the file to the
machine's SD card (**Remote**) and runs it there. That last step is deliberately manual.

## Always

- Pass `--json` when you need to read results: stdout is one document `{"ok", "command",
  "data" | "error"}`. On failure read `error.code`, `error.message`, `error.hint`.
- **Never** move the machine, start, pause or stop a job, or send commands to the controller. This
  tool cannot, and you must not try other ways (raw SSH commands, the controller UI, serial ports).
  The operator runs jobs at the machine.
- Don't overwrite: `send` refuses existing files (exit 5). Only add `--force` when the user asked to
  replace that file.
- Project names: letters, digits, spaces and `. _ - + ( )`, max 80 chars, no slashes. Pick a short
  human name from the user's words (e.g. "Front panel v2"); reuse an existing project when it
  obviously matches — check `carvera projects` first.
- Don't print or repeat connection details (hostnames, IPs, key paths) unless the user asks.

## Commands

| Task | Command |
|---|---|
| List projects (+ loose inbox files) | `carvera --json projects` |
| List files in a project | `carvera --json ls "<project>"` (no project = inbox) |
| Create a project | `carvera new "<project>"` (no-op if it exists) |
| Send files into a project | `carvera send -p "<project>" file1.nc file2.nc` (creates the project) |
| Send, project = each file's local folder name | `carvera send --project-from-dir path/to/<project>/file.nc` |
| Send into the inbox | `carvera send file.nc` |
| Replace an existing file | `carvera send -p "<project>" --force file.nc` |
| Remove a file / an empty project | `carvera rm "<project>" "<file>"` / `carvera rm "<project>"` |
| Is everything online? | `carvera --json status` → `controller_running`, `machine_connected`, `firmware`, `pendant_receiver` |
| See the HMI screen | `carvera screenshot /tmp/carvera.png`, then look at the image |

`send` warns (does not fail) on unusual extensions; G-code is normally `.nc`, `.gcode`, `.cnc`,
`.tap`, `.ngc`.

## Setup (only when not configured or the user asks)

Commands fail with `error.code = "not_configured"` (exit 6) until setup has run. Setup only needs an
SSH target that already works with key authentication from this computer — an alias from
`~/.ssh/config`, `user@host` or `user@ip`. LAN, VPN or Tailscale names all work the same way.

- Ask the user how the HMI is reached (or use the alias they name), then run
  `carvera setup --host <target> [--port N] [--identity <keyfile>] [--gcode-dir gcode]`.
  It tests SSH, creates the folder and saves `~/.config/carvera/config.json`.
- `carvera setup --show` prints the current configuration.
- If SSH asks for a password, setup fails: the user must set up key auth first
  (`ssh-copy-id <target>`), which needs their password — let them run it themselves.
- `carvera skill install` installs this skill for Claude Code (`~/.claude/skills`) and other agents
  (`~/.agents/skills`).

## Typical requests

- *"Send that to the Carvera"* → find the file(s) the user means (recent CAM export, path in the
  conversation). If they belong to a job/part, send into a project named after it (`-p`);
  otherwise ask, or use `--project-from-dir` when the local folder already names the project.
  Report where each file landed and that it's under **Local → <project>** on the HMI.
- *"Create a project for X and send the toolpaths"* → `carvera new "X"`, then
  `carvera send -p "X" <files>`.
- *"What's on the Carvera?"* → `carvera --json projects`.
- *"Is the Carvera ready?"* → `carvera --json status`; if `machine_connected` is false, say so and
  offer a screenshot to see what the screen shows.

## Exit codes

| exit | `error.code` | meaning / what to do |
|---|---|---|
| 0 | – | ok |
| 1 | `remote_error`, `not_empty`, `screenshot_failed` | read message/hint |
| 2 | `bad_name`, `bad_config` | fix the name or setup arguments |
| 3 | `unreachable` | HMI off or not reachable from this computer; tell the user, don't guess other addresses |
| 4 | `not_found` | project/file doesn't exist — list first |
| 5 | `exists` | file already there; ask before `--force` |
| 6 | `not_configured` | run setup (see above) |
