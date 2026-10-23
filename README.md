# Unstepped plugins

Unstepped's Claude Code marketplace. It holds only the **installer** for Unstepped Studio, the editing team's Home and
review page for client videos. The app itself is downloaded from the company's Unstepped Cloud after you sign in, so
this repository has no app code and no client material.

## Install (team members)

You need a Mac, Claude Code (the Claude desktop app's Code tab, or `claude` in Terminal), and your work email.

1. Add this marketplace and install the plugin. In a Claude Code session:

   ```
   /plugin marketplace add Umairshaffi/Unstepped-Studio-2
   /plugin install unstepped-studio@unstepped
   ```

   or in Terminal:

   ```
   claude plugin marketplace add Umairshaffi/Unstepped-Studio-2
   claude plugin install unstepped-studio@unstepped
   ```

2. Start a new Claude session and say **"set up Unstepped Studio"**.
3. A sign-in page opens in your browser. Enter your name and work email and press **Request access**: your admin gets
   an email with your one-time code. When they send it to you, enter it on the same page. (Already have a code from your
   admin? Choose **I already have a code**.) Unstepped Studio downloads, Home opens on Setup, and it installs what
   editing needs (it asks for your Mac's password once, for Homebrew). Add your API keys at the end.

After that Home starts by itself when you log in, and updates install by themselves. Restarting never signs you out.

## For the maintainer

- `./release.sh app 2.1.0 "What changed"` publishes the app from `~/unstepped-review` to Unstepped Cloud (your Mac must
  be signed in as the owner). Signed-in Macs install it on their next check-in. Nothing goes to GitHub.
- `./release.sh installer 2.2.1` after changing the installer (scripts, SKILL.md, hooks): it bumps the version,
  validates and commits. Then `git push`. Team members get it with `/plugin marketplace update unstepped`.
- The server (sign-in, Macs, the dashboard, shared styles, releases) is `~/unstepped-cloud`; see its README.

The review page is a fork of the one in the cooked. plugin by NextWork (MIT; its license ships with the app).
