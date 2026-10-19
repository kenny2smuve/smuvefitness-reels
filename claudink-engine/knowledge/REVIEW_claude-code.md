# Claude Code mechanics: verified against code.claude.com docs (2026-10-18)

Sources read: /docs/en/permissions, /docs/en/permission-modes, /docs/en/settings, /docs/en/sub-agents,
/docs/en/skills, /docs/en/cli-reference, /docs/en/memory, /docs/en/scheduled-tasks,
/docs/en/desktop-scheduled-tasks. Quotes are verbatim.

| id | severity | where | problem | evidence | fix |
|----|----------|-------|---------|----------|-----|
| cc-1 | blocker | v1 3.3 "F:\My Drive = //f/My Drive" | Correct, but the kit must not also write D:/... forms (they never match) and must use Edit() for writes: "If you write a path rule for Write ... Claude Code accepts the rule but never consults it". | permissions: "On Windows, paths are normalized to POSIX form before matching. C:\Users\alice becomes /c/Users/alice, so use //c/**/.env". | settings.json uses only //d/ and //f/ and //c/ forms; Edit(...) rules for every write scope; no Write(...) path rules. |
| cc-2 | major | v1 3.3 "Project allows only apply once the folder is trusted" | True, and stronger: a -p run never shows the trust dialog and "trusting a parent folder doesn't count", so a headless scheduled run that relies on project allows will prompt or deny. | permissions "Project allow rules and workspace trust": "Claude Code shows the trust dialog in interactive sessions only. A claude -p run or an SDK session never shows it". | Put every allow and additionalDirectories entry in ~/.claude/settings.json as well (the kit's README gives the exact block). Desktop scheduled tasks: "Allow rules from ~/.claude/settings.json also apply to scheduled task sessions." |
| cc-3 | major | v1 3.3 deny list | Deny rules are the right mechanism and hold in every mode: "Deny rules block in every mode, including bypassPermissions." Compound commands are covered: "Deny and ask rules apply when any subcommand matches them, including a command nested inside a subshell". But a deny on Bash(rm *) "doesn't match the same program invoked in a different form" (full path, sh -c) and Read/Edit deny rules "don't apply ... to arbitrary subprocesses that read or write files indirectly, like a Python or Node script". | permissions "Bash rule limits", "Read and Edit deny rules apply to ...". | Keep the deny list (it stops the usual form) and state in the prompt that tools/safeio.py is the real guard for Python-side writes; never present settings.json as a sandbox. |
| cc-4 | major | v1 3.9 `claude -p ... --permission-mode auto --permission-prompts none` | Both flags are real: --permission-mode accepts default, acceptEdits, plan, auto, dontAsk, bypassPermissions, manual; --permission-prompts none "Pass none when nobody can answer, and Claude Code denies them instead" (requires v2.1.259+). bypassPermissions needs a one-time interactive acceptance: "With no acceptance recorded, claude --bg --permission-mode bypassPermissions is refused until you accept the dialog in an interactive session." and allow rules have no effect in it. | cli-reference, permission-modes. | CLI fallback invocation: `claude -p "<prompt>" --permission-mode auto --permission-prompts none` with allows in ~/.claude/settings.json; alternative for a locked-down run: `--permission-mode dontAsk` (auto-denies anything not allowed, never waits). Do not use bypassPermissions in the .cmd. |
| cc-5 | blocker | v1 3.9 two scheduled tasks 05:30 and 06:40 | Desktop scheduled tasks skip a run when "the previous run was still in progress, or other scheduled tasks were already running", and run only "while the app is open and your computer is awake". A weekly run that overruns 06:40 silently skips the daily run. | desktop-scheduled-tasks "Review history ... Hover a skipped entry to see why". | One scheduled task, `reel-daily` at 06:40; the daily-run skill performs the weekly block after delivery on Sundays. Keep `reel-weekly` only as a Manual-schedule task for on-demand runs. |
| cc-6 | major | v1 3.9 "the Desktop app runs them while the PC is awake" | Also: missed runs get exactly one catch-up run on wake, possibly late at night: "A task scheduled for 9am might run at 11pm if your computer was asleep all day." Tasks are created in Code tab -> Routines -> New routine -> Local (Desktop >= 1.1.5368) or by asking in a session; each task has its own permission mode and "Run now" + "always allow" avoids stalls; prompts live in ~/.claude/scheduled-tasks/<name>/SKILL.md; each run starts a few minutes late by design. | desktop-scheduled-tasks. | The daily-run skill opens with the late-run guardrail (plan only the next valid slots; after 20:15 ET deliver for tomorrow; never make a reel for a passed slot) and the prompt says the task runs late by design, so slot A is never set before 07:30 on the X1 morning arm delivered the day before. |
| cc-7 | minor | v1 3.4 agents "tool Read only" | Frontmatter is `tools: Read` (comma-separated string or list), fields are camelCase (`permissionMode`, `maxTurns`, `disallowedTools`); unknown fields are ignored silently. The Read tool renders images, so a Read-only critic can view the contact sheets. | sub-agents frontmatter table. | Kit agents use `tools:` strings; critic gets `tools: Read` and `permissionMode: default`; analyst/scout/rules-keeper as planned. |
| cc-8 | minor | v1 3.3 "allowed-tools: list in SKILL.md frontmatter" | `allowed-tools` is real: "Accepts a space- or comma-separated string, or a YAML list"; it grants the listed tools only for the invoking turn: "The grant clears when you send your next message". For a scheduled run it is useful but not sufficient: the session-wide allows live in settings. | skills frontmatter. | Keep allowed-tools in daily-run/weekly skills; rely on ~/.claude/settings.json allows for the session. |
| cc-9 | minor | v1 3.2 CLAUDE.md "@knowledge/RULES.md" | Import syntax is right; relative to the CLAUDE.md file; "target under 200 lines"; a path with spaces needs backslashes; imported files load at launch (they count against context). | memory "Import additional files". | Keep `@knowledge/RULES.md`; keep CLAUDE.md under 120 lines; never import reel-bible.md. |
| cc-10 | major | v1 3.3 `additionalDirectories: F:/My Drive/ClaudeInk Reels` | Lives under `permissions.additionalDirectories`; it grants read without prompts and edit per the mode; as a project setting it also waits for trust (cc-2). Network (UNC) paths cannot be added; a mapped drive is fine. | permissions "Working directories", "Project allow rules and workspace trust". | Set `permissions.additionalDirectories: ["F:/My Drive/ClaudeInk Reels"]` in BOTH project and user settings. Footage stays read-only via Read(//f/My Drive/**) allows (not an additional directory). |
| cc-11 | minor | v1 3.3 `defaultMode` | `"auto"` and `"bypassPermissions"` in project settings do not take effect; auto is the built-in start mode for interactive sessions on v2.1.283+. | permission-modes "Which mode a session starts in". | Do not set defaultMode in the project file; pick Auto in the Desktop picker / per scheduled task. |
| cc-12 | minor | prompt section 3 step 3 verify | Reads outside working dirs: "the first read outside the working directories ... prompts you" in auto mode unless an allow rule matches; Read(//f/My Drive/**) in user settings removes that prompt. | permission-modes auto mode list. | Keep the Read allows in user settings; the verify step (ffprobe a Drive clip) catches a missing rule on day one. |

## Corrected snippets

### settings.json permissions (Windows; POSIX forms only; Edit for writes)
```json
{
  "permissions": {
    "allow": [
      "Bash(py *)", "Bash(python *)", "Bash(pip *)", "Bash(yt-dlp *)",
      "Bash(ffmpeg *)", "Bash(ffprobe *)",
      "Bash(D:/ClaudeInk/ffmpeg71/bin/ffmpeg.exe *)", "Bash(D:/ClaudeInk/ffmpeg71/bin/ffprobe.exe *)",
      "Bash(/d/ClaudeInk/ffmpeg71/bin/ffmpeg.exe *)", "Bash(/d/ClaudeInk/ffmpeg71/bin/ffprobe.exe *)",
      "PowerShell(py *)", "PowerShell(python *)", "PowerShell(pip *)", "PowerShell(yt-dlp *)",
      "PowerShell(ffmpeg *)", "PowerShell(ffprobe *)",
      "PowerShell(D:\\ClaudeInk\\ffmpeg71\\bin\\ffmpeg.exe *)", "PowerShell(D:\\ClaudeInk\\ffmpeg71\\bin\\ffprobe.exe *)",
      "Read(//f/My Drive/**)", "Read(//d/ClaudeInk/**)", "Read(//c/Users/Firelake330/Desktop/igserver/**)",
      "Edit(//d/ClaudeInk/reel-engine/**)", "Edit(//f/My Drive/ClaudeInk Reels/**)"
    ],
    "deny": [
      "Bash(rm *)", "Bash(del *)", "Bash(rmdir *)", "Bash(format *)", "Bash(mv *)", "Bash(move *)",
      "Bash(git push *)",
      "PowerShell(Remove-Item *)", "PowerShell(Move-Item *)", "PowerShell(rm *)", "PowerShell(del *)",
      "PowerShell(rmdir *)", "PowerShell(ri *)", "PowerShell(mv *)", "PowerShell(move *)",
      "Edit(//d/ClaudeInk/runner/**)", "Edit(//d/ClaudeInk/shop/**)", "Edit(//d/ClaudeInk/out/**)",
      "Edit(//f/My Drive/Shop shorts/**)", "Edit(//f/My Drive/Session*/**)"
    ],
    "additionalDirectories": ["F:/My Drive/ClaudeInk Reels"]
  },
  "env": { "PYTHONUTF8": "1" }
}
```
The same `permissions` block goes into `~/.claude/settings.json` (user scope), where it applies to
`-p` runs and Desktop scheduled tasks without the trust dialog.

### Subagent frontmatter
```yaml
---
name: critic
description: Blind panel judge. Ranks anonymised reel packets; never sees names or numbers.
tools: Read
permissionMode: default
---
```

### Skill frontmatter
```yaml
---
name: daily-run
description: The 06:40 ET daily program. Use when the scheduled task fires or Claudin says "run the day".
allowed-tools: Bash(py *) Bash(python *) Bash(ffmpeg *) Bash(ffprobe *) Read Edit Write Glob Grep
---
```

### Headless invocation (CLI fallback for schtasks)
```
claude -p "Read CLAUDE.md, then run the daily program exactly as .claude/skills/daily-run/SKILL.md says." --permission-mode auto --permission-prompts none --max-turns 400
```
(`--permission-prompts none` requires Claude Code v2.1.259 or later; on an older build drop it and
use `--permission-mode dontAsk`.)

### Scheduled task (preferred: Desktop)
Code tab -> Routines -> New routine -> Local. Name `reel-daily`, folder `D:\ClaudeInk\reel-engine`,
schedule Daily 06:40, permission mode Auto, worktree OFF. Instructions: "Read CLAUDE.md, then run the
daily program exactly as .claude/skills/daily-run/SKILL.md says." Click Run now once and choose
"always allow" on every prompt. One task only: the Sunday weekly block runs inside daily-run.
