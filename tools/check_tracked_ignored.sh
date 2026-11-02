#!/bin/sh
# Fail when the repository tracks a file that its own ignore rules exclude.
#
# `.gitignore` governs only what git *notices*; it says nothing about what is
# already in the index. A committed scratch database (`db_dev.sqlite3`) or an
# uploaded media file stays in the repo invisibly — nothing else in CI looks at
# the index, so nothing notices.
#
# Two independent signals, because they catch different mistakes:
#
#   1. `git ls-files -ci --exclude-standard` — tracked but excluded, i.e.
#      added with `-f` or covered by a rule added afterwards.
#   2. A short junk list (dependency trees, caches, bytecode, OS droppings),
#      which fails even with no matching rule so a missing rule cannot hide it.
#
# Usage: sh tools/check_tracked_ignored.sh     (run from the repo root)
# Exit 0 = clean, 1 = offenders found or git could not be asked.

set -u

shown=20

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "✖ not a git checkout; cannot inspect the index" >&2
  exit 1
fi

tracked=$(git ls-files | wc -l | tr -d ' ')
if [ "$tracked" = "0" ]; then
  echo "✖ the index is empty; cannot verify" >&2
  exit 1
fi

ignored=$(git ls-files -c -i --exclude-standard || true)
junk=$(git ls-files | grep -E '(^|/)(node_modules|\.npm|__pycache__|\.venv)/|(^|/)\.DS_Store$' || true)

offenders=$(printf '%s\n%s\n' "$ignored" "$junk" | sed '/^$/d' | sort -u)

if [ -n "$offenders" ]; then
  count=$(printf '%s\n' "$offenders" | wc -l | tr -d ' ')
  echo "" >&2
  echo "✖ $count tracked file(s) should not be in the repo:" >&2
  printf '%s\n' "$offenders" | head -n "$shown" | sed 's/^/   /' >&2
  if [ "$count" -gt "$shown" ]; then
    echo "   … and $((count - shown)) more" >&2
  fi
  echo "" >&2
  echo "Remove them from the index without touching the working tree:" >&2
  echo "   git rm -r --cached <path>   (then commit, and add the path to .gitignore)" >&2
  exit 1
fi

echo "✅ $tracked tracked file(s), none ignored or junk."
