# Unstepped plugins

Unstepped's Claude Code marketplace. It holds only the **installer** for Unstepped Studio: the app itself is downloaded
from the company's Unstepped Cloud, during a 7-day trial or after signing in, so this repository has no app code.

## For the team

In Claude (the desktop app or a terminal):

```
/plugin marketplace add <the address your admin sent>
/plugin install unstepped-studio@unstepped
```

Then ask Claude to "set up Unstepped Studio", open Home, and sign in on Home > Settings > Account with your work email
and the one-time code from your admin. Updates install by themselves. macOS only; it needs ffmpeg and Node.

## For the maintainer

- `./release.sh app 2.1.0 "What changed"` publishes the app from `~/unstepped-review` to Unstepped Cloud (your Mac
  must be signed in as the service's owner). Signed-in Macs install it on their next check-in.
- `./release.sh installer 2.0.1` after changing the installer (scripts, SKILL.md, hooks), then `git push`.
- The server (sign-in, devices, the dashboard, shared styles, releases) is `~/unstepped-cloud`; see its README.

The review page is a fork of the one in the cooked. plugin by NextWork (MIT; its license ships with the app).
