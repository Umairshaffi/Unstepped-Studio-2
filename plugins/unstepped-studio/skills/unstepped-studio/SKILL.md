---
name: unstepped-studio
description: Unstepped Studio - a local home page and review page for editing client videos with Claude Code. Home (styles, video folders, settings) runs on this Mac at 127.0.0.1:47900; each style is a skill with a main folder and a parent Claude session, each video gets its own subsession and a review page where the user leaves notes on moments of the draft (with HyperFrames Studio live beside them) and approves. Use when the user says "set up Unstepped Studio", "install the review page", "open Home", "start Home", "open the review page", "review this draft", "new style", "new video", or shares this skill and asks how to use it. Not for cutting or styling a video itself (the editing skills do that); this is where drafts are reviewed and styles are kept.
---

# Unstepped Studio

A local web app for an editing team that works through Claude Code:

- **Home** (`home.py`, port 47900): **Videos** (one folder per style, cards that scrub on hover, status of
  every edit), **Styles** (each style's guide, brief, references, B-roll, graphics, fonts and colours),
  **+ New style** (a brief: references, brand files, preferences, a main folder), **New video** (raw footage + a
  first message, handed to the style's parent session), **Settings** (API keys, tools, home folder, alerts).
- **Review page** (`review.py`, one per edit): watch the draft, leave notes on exact moments (or on a layer in the
  live HyperFrames Studio preview), Submit them to Claude, approve. Approving the final draft adds its media to the
  style's library.
- **Styles** (`style.py`): the brief, reference cuts, the library, and one subfolder per video.

It's a fork of the review page in the cooked. / ai-editor plugin by NextWork (MIT; see `app/LICENSE-cooked`).

## Install (first time, or to repair)

The plugin is only the installer: the app comes from the company's Unstepped Cloud, and only after signing in. When the
user asks to set up Unstepped Studio, run

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/unstepped-studio/scripts/setup.sh"
```

On a new Mac it opens a sign-in page in the browser: the person signs in there with their work email and the one-time
code their admin gave them (never ask for the code in chat, never guess one). Then the app installs, Home is set to start
at login, and it opens straight onto Home > Setup: Apple's developer tools, Homebrew (it asks for the Mac's password once,
in a Terminal window), FFmpeg, Node, yt-dlp, uv, the HyperFrames engine, the editing Python and models, and the
company's video skills (installed into ~/.claude/skills). The person adds their API keys last. On a Mac that's already
signed in, setup just repairs and starts Home. Updates and the team's skills stay in step by themselves.

## Accounts and the lock

`python3 ~/unstepped-review/cloud.py status` says where this Mac stands: signed-in, or locked (not signed in yet, signed
out, logged out by the admin, offline too long, or an update needed). Restarts never sign anyone out. When it's locked, Home shows a
sign-in screen and `review.py`, `style.py` and `shared.py` refuse to run: tell the user to sign in on Home >
Settings > Account; don't try to get around it. Signing out, or an admin's logout, removes the shared styles this Mac
downloaded; the person's own styles and video projects stay. Admins open the team dashboard from Home > Settings >
Account (people, Macs, codes, activity, logout).

## Start Home

```bash
/usr/bin/python3 ~/unstepped-review/home.py serve          # opens the browser; run it in the background
```

Its URL carries a login token (`?t=...`); `~/unstepped-review/home/token` holds it. `serve --no-open` skips the
browser. Only one Home runs at a time; starting it again just prints the running URL.

## How the work is organised

```
Claude parent session (one per style)   <->  <main folder>/          (the user picks it on + New style)
                                              brief/    BRIEF.md, brief.json, PARENT.md, VIDEO.md, brand/
                                              library/  references/, approved B-roll, graphics, fonts, style.json
Claude subsession (one per video)       <->  videos/<video>/  raw/, files/, project/ (HyperFrames), edit/ (review), lessons.md
```

- **New style** (Home > Styles > + New style): the form saves the brief and makes the folders and the skill
  (`~/.claude/skills/<style>/`). **Start it in the Claude app** opens the parent session, which follows
  `brief/PARENT.md`: it asks for anything missing from the brief (never guesses), watches the references with the
  `analyze` skill, confirms its reading with the user, writes `GUIDE.md`, picks the editing skills, then asks for the
  raw footage. Only the parent writes `GUIDE.md`.
- **New video** (Home > New video): style, raw footage, other files, first message. Home makes
  `videos/<name>/`, writes its `VIDEO.md`, copies a message to the clipboard and opens the parent session; the user
  pastes and sends it, and the parent starts the video's subsession with `start_session` (prompt: that `VIDEO.md`),
  then records it: `python3 ~/unstepped-review/style.py video <style> "<name>" --session <local_ id>`.
- **The subsession** edits with the skills the guide names, builds in `project/`, and opens each draft for review:

```bash
python3 ~/unstepped-review/review.py "<main>/videos/<name>/edit" start --video <draft.mp4> --project "<main>/videos/<name>/project" --style <style>
python3 ~/unstepped-review/review.py "<edit>" wait                       # blocks until the user submits or approves
python3 ~/unstepped-review/review.py "<edit>" round --video <next.mp4> --changed "what changed"
python3 ~/unstepped-review/review.py "<edit>" resolve <id> fixed --reply "..."
```

  `start` opens the review page in the browser. `wait` prints each note with its time, the element or layer it was
  left on, and the frame. Lasting lessons go in `videos/<name>/lessons.md`; on the final approve the media joins the
  library and the subsession reports to the parent, which folds the lessons into `GUIDE.md`.

## Style libraries

Each style's `library/style.json` holds sections: `broll`, `graphics` (motion-graphic screens), `screens` (UI, documents,
end cards), `effects`, `transitions` (`type`: `effect` made in the edit, or `overlay` for light leaks, burns, green-screen
stingers), `grades` (LUTs: .cube, .3dl, .look), and the cuts from the references. Every item can carry `tags`, `prompt`,
`how` (how it was made) and `recreate` (how to make it again).

```bash
python3 ~/unstepped-review/style.py find <style|'*'> <words>          # search by tags, names, notes: do this before sourcing
python3 ~/unstepped-review/style.py tag <style> <uid> --add "lower third, amber" --how "..." --recreate "..."
python3 ~/unstepped-review/style.py add <style> --section grades --file look.cube --name "Warm desk" --tags warm
python3 ~/unstepped-review/style.py copy <from> <to> <uid> ...        # items into another style
python3 ~/unstepped-review/style.py export <style> [--videos] [--brief]   # the style as a shareable skill zip
python3 ~/unstepped-review/style.py import <zip> [--folder <main>] [--as <name>]
```

A video subsession keeps `videos/<name>/library.json` as it builds (each graphic by element id, B-roll by file, the
effects, transitions, screens and LUTs, with tags, prompt, how and recreate); the final approve reads it into the
library. Only references, brand files and approved videos go in a library, never drafts. On Home, a style's library
tabs search, select, copy to another style and download a zip (tags and notes stay internal, for Claude's searches); the style's menu
exports it, and Styles > Import a style brings one in.

## Shared styles

Styles > Shared lists the company's shared styles on Unstepped Cloud (signed-in Macs only). A style's ⋯ menu > Move to
Shared uploads it; the Shared tab downloads, updates and uploads changes. A downloaded shared style lives in
`~/Library/Application Support/Unstepped/shared/<style>/` (its `videos/` links to the person's own folder). From
Terminal: `python3 ~/unstepped-review/shared.py list | status <style> | push <style> | pull <style>`.

## Rules

- Quote every path: main folders often contain spaces.
- Don't edit a style's `GUIDE.md` or `SKILL.md` from a video subsession.
- Never print API keys; they live in `~/.zshrc` (or another tool's `.env`, which Settings shows).
- `python3 ~/unstepped-review/style.py -h` and `review.py <folder> -h` list every command.
