#!/bin/sh
# One-time welcome for the aik-sessions plugin.
#
# The first time a Claude Code session starts after install, this opens the page named in
# hooks/welcome-url.txt in the default browser and tells the user it did so. It then
# records the URL under the plugin's data directory and stays silent until the URL changes.
#
# It never blocks a session: every path exits 0, and it does nothing when
#   - welcome-url.txt has no URL (the shipped default),
#   - the URL is not a plain https:// address,
#   - AIK_SESSIONS_NO_WELCOME=1 is set,
#   - the session is in CI or driven by the SDK (nobody is there to read a page).
# On a remote or SSH machine it prints the link instead of launching a browser.
#
# The URL is only ever passed to the opener as a single argument. It is never evaluated
# by a shell, so its contents cannot run commands.

set -u

root=${CLAUDE_PLUGIN_ROOT:-$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)}
data=${CLAUDE_PLUGIN_DATA:-}
url_file="$root/hooks/welcome-url.txt"

[ "${AIK_SESSIONS_NO_WELCOME:-}" = "1" ] && exit 0
[ -n "${CI:-}" ] && exit 0
case "${CLAUDE_CODE_ENTRYPOINT:-}" in sdk*) exit 0 ;; esac
# Without a persistent directory there is no way to remember "already shown".
[ -n "$data" ] || exit 0
[ -r "$url_file" ] || exit 0

# First line that is neither blank nor a comment, with surrounding whitespace trimmed.
# A line with whitespace inside it is malformed and fails the character check below,
# rather than being cut short and opened as a different address.
url=$(grep -v '^[[:space:]]*#' "$url_file" | tr -d '\r' | awk 'NF { print; exit }' \
  | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
[ -n "$url" ] || exit 0
case "$url" in https://?*) ;; *) exit 0 ;; esac
# Allow only characters that are valid in a URL; refuse quotes, spaces and controls.
if printf '%s' "$url" | LC_ALL=C grep -q '[^A-Za-z0-9._~:/?#@!&=+,;%-]'; then
  exit 0
fi

marker="$data/welcome-opened"
if [ -f "$marker" ] && [ "$(cat "$marker" 2>/dev/null)" = "$url" ]; then
  exit 0
fi

mkdir -p "$data" 2>/dev/null || exit 0
# Never write through a symlink planted at the marker path.
[ -L "$marker" ] && exit 0
# Record first: if the browser launch misbehaves, the page must not reopen every session.
printf '%s\n' "$url" > "$marker" 2>/dev/null || exit 0

opened=no
if [ -z "${SSH_CONNECTION:-}${SSH_TTY:-}${CLAUDE_CODE_REMOTE:-}" ]; then
  case "$(uname -s 2>/dev/null)" in
    Darwin)
      open "$url" >/dev/null 2>&1 && opened=yes ;;
    Linux)
      if grep -qi microsoft /proc/version 2>/dev/null && command -v wslview >/dev/null 2>&1; then
        wslview "$url" >/dev/null 2>&1 && opened=yes
      elif command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        xdg-open "$url" >/dev/null 2>&1 && opened=yes
      fi ;;
    MINGW*|MSYS*|CYGWIN*)
      rundll32.exe url.dll,FileProtocolHandler "$url" >/dev/null 2>&1 && opened=yes ;;
  esac
fi

if [ "$opened" = yes ]; then
  message="AIK Sessions is installed. The getting-started page opened in your browser: $url"
else
  message="AIK Sessions is installed. Getting started: $url"
fi
# The URL charset check above excludes quotes and backslashes, so this is valid JSON.
printf '{"systemMessage": "%s"}\n' "$message"
exit 0
