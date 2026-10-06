#!/usr/bin/env bash
# Regression test for the alerting branch of .github/workflows/stack-monitor.yml.
#
# Why this exists: the monitor runs every 15 minutes, so an outage that is
# reported on every run would post ~96 comments a day onto one issue and train
# everyone to ignore it. The step therefore fingerprints the FAIL/WARN lines and
# stays silent when the same failure repeats.
#
# The danger in that fix is over-suppressing — going quiet when something NEW
# breaks. Both halves are asserted here:
#
#   A. no open issue                -> creates one
#   B. open issue, new fingerprint  -> comments
#   C. open issue, same fingerprint -> SILENT        (the dedupe)
#   D. failure CHANGED since last   -> comments      (not over-suppressed)
#
# `gh` is stubbed, and the step body is read out of the workflow file itself,
# so this exercises the artifact that ships rather than a copy of it.
#
# Usage: bash tools/test_monitor_dedupe.sh
set -uo pipefail

repo_dir=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/fixtures"

failures=0

# ── stub gh ───────────────────────────────────────────────────────────────
cat > "$work/bin/gh" <<'STUB'
#!/usr/bin/env bash
state="$GH_STUB_STATE"
case "$1 $2" in
  "label create") exit 0 ;;
  "issue list")    cat "$state/issue_number" 2>/dev/null || true; exit 0 ;;
  "issue view")    cat "$state/comments" 2>/dev/null || true; exit 0 ;;
  "issue create")  echo "CREATE"  >> "$GH_STUB_LOG"; exit 0 ;;
  "issue comment") echo "COMMENT" >> "$GH_STUB_LOG"; exit 0 ;;
esac
echo "stub gh: unhandled args: $*" >&2
exit 1
STUB
chmod +x "$work/bin/gh"

# ── extract the real step body ────────────────────────────────────────────
python - "$repo_dir" "$work/step.sh" <<'PY' || exit 1
import sys, yaml
repo, out = sys.argv[1], sys.argv[2]
d = yaml.safe_load(open(f"{repo}/.github/workflows/stack-monitor.yml", encoding="utf-8"))
steps = d["jobs"]["probe"]["steps"]
name = "Open or update the alert issue"
try:
    step = next(s for s in steps if s.get("name") == name)
except StopIteration:
    sys.exit(f"FAIL: no step named {name!r} in stack-monitor.yml")
open(out, "w", newline="\n").write(step["run"])
print(f"extracted {len(step['run'].splitlines())} lines from stack-monitor.yml")
PY

# ── fixtures ──────────────────────────────────────────────────────────────
cat > "$work/fixtures/report.txt" <<'REPORT'
FAIL  CORS               no Access-Control-Allow-Origin for https://fibre-fe-98f8e0.gitlab.io
WARN  backend /healthz   404 -- the deployed revision predates the health probe
REPORT

# a second thing breaking on top of the first
cat > "$work/fixtures/report-changed.txt" <<'REPORT'
FAIL  CORS               no Access-Control-Allow-Origin for https://fibre-fe-98f8e0.gitlab.io
FAIL  engine /health     unreachable -- URLError: connection refused
REPORT

fp_of() { (grep -E '^(FAIL|WARN)' "$1" || echo 'no-fail-lines') | sha256sum | cut -c1-12; }
fp=$(fp_of "$work/fixtures/report.txt")
fp_changed=$(fp_of "$work/fixtures/report-changed.txt")

if [ "$fp" = "$fp_changed" ]; then
  echo "FAIL: the two fixtures must fingerprint differently, else case D proves nothing"
  exit 1
fi

# ── the cases ─────────────────────────────────────────────────────────────
# expect_action: CREATE | COMMENT | NONE
run_case() {
  local label="$1" expect="$2" issue_number="$3" comments="$4" report="$5"
  rm -rf "$work/state"; mkdir -p "$work/state"
  [ -n "$issue_number" ] && printf '%s\n' "$issue_number" > "$work/state/issue_number"
  printf '%s' "$comments" > "$work/state/comments"
  : > "$work/log"

  # The probe step writes report.txt into the workspace root, and the alert
  # step reads it from there, so reproduce that layout.
  cp "$report" "$work/report.txt"
  ( cd "$work" && \
    PATH="$work/bin:$PATH" GH_STUB_STATE="$work/state" GH_STUB_LOG="$work/log" \
    bash "$work/step.sh" ) > "$work/out.txt" 2>&1
  local rc=$?
  local got; got=$(tr -d '\n' < "$work/log" 2>/dev/null)
  # NONE is the human-readable name for "gh was not called at all".
  local want="$expect"; [ "$want" = "NONE" ] && want=""

  if [ "$got" = "$want" ] && [ "$rc" = "0" ]; then
    printf 'PASS  %-46s (gh: %s)\n' "$label" "${got:-silent}"
  else
    printf 'FAIL  %-46s expected %s, got %s (exit %s)\n' \
      "$label" "${expect:-silent}" "${got:-silent}" "$rc"
    sed 's/^/        | /' "$work/out.txt"
    failures=$((failures + 1))
  fi
}

run_case "A. no open issue                  -> create"  CREATE  ""  "" \
  "$work/fixtures/report.txt"
run_case "B. open issue, new fingerprint    -> comment" COMMENT "7" \
  "<!-- stack-monitor-fp:deadbeefdead -->" "$work/fixtures/report.txt"
run_case "C. same failure again             -> silent"  NONE    "7" \
  "<!-- stack-monitor-fp:${fp} -->" "$work/fixtures/report.txt"
run_case "D. failure changed since last     -> comment" COMMENT "7" \
  "<!-- stack-monitor-fp:${fp} -->" "$work/fixtures/report-changed.txt"

echo
if [ "$failures" -eq 0 ]; then
  echo "all 4 cases passed"
  exit 0
fi
echo "$failures case(s) failed"
exit 1
