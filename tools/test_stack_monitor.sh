#!/usr/bin/env bash
# Tests for .github/workflows/stack-monitor.yml.
#
# Two properties, both invisible when broken and both caught only by running
# the workflow rather than reading it:
#
#   1. EXIT STATUS PROPAGATION. The probe runs
#         python3 tools/check_stack.py | tee report.txt
#      and a pipeline's status is the LAST command's, which is tee's, which is
#      always 0. Without `set -o pipefail` the step reports success however
#      badly the check failed, the alert step never runs, and the monitor is
#      silently decorative. This actually happened: a real dispatch concluded
#      success while its own report.txt said "1 FAILED".
#
#   2. ALERT DEDUPE. The workflow runs every 15 minutes and comments on its
#      issue every time it fails, so an outage that lasts would post ~96
#      comments a day onto one issue. The fix fingerprints the FAIL/WARN
#      lines - and its own danger is over-suppressing, going quiet when
#      something NEW breaks. Both halves are asserted.
#
#   3. THE ENGINE HEALTH GRADE. check_stack.py grades the engine's /health
#      payload, and both flags it reads are NESTED: postgis is db_info(),
#      i.e. {"available": false}, and qgis_process is a path or null. Reading
#      the outer level is always truthy, so a dead database link and a missing
#      qgis_process both graded PASS. That is not hypothetical - the live
#      Zeabur engine answered {"postgis": {"available": false}} from
#      2026-07-17 while this monitor printed "postgis ok".
#
# Step bodies are read out of the workflow file, so this exercises the
# artifact that ships rather than a copy of it.
#
# Usage: bash tools/test_stack_monitor.sh
set -uo pipefail

repo_dir=$(cd "$(dirname "$0")/.." && pwd)
wf="$repo_dir/.github/workflows/stack-monitor.yml"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/fixtures"

failures=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; failures=$((failures + 1)); }

# ── extract the steps we test ─────────────────────────────────────────────
python - "$wf" "$work" <<'PY' || exit 1
import sys, pathlib, yaml
wf, out = sys.argv[1], pathlib.Path(sys.argv[2])
d = yaml.safe_load(open(wf, encoding="utf-8"))
steps = {s.get("name"): s for s in d["jobs"]["probe"]["steps"] if s.get("name")}
for name in ("Probe the live stack", "Open or update the alert issue"):
    if name not in steps:
        sys.exit(f"FAIL: step {name!r} missing from {wf}")
    (out / f"{name.split()[0].lower()}.sh").write_text(steps[name]["run"], encoding="utf-8")
print(f"extracted 2 step bodies from {pathlib.Path(wf).name}")
PY

echo
echo "── 1. exit status propagation ──────────────────────────────────────────"

# The step calls `python3`, which some environments (Git Bash on Windows) do
# not provide. Resolve the real interpreter BEFORE shimming it, or the wrapper
# would find itself on PATH and recurse forever.
REAL_PY=$(command -v python3 || command -v python)
if [ -z "$REAL_PY" ]; then
  echo "FAIL: no python interpreter available"; exit 1
fi
cat > "$work/bin/python3" <<SHIM
#!/usr/bin/env bash
exec "$REAL_PY" "\$@"
SHIM
chmod +x "$work/bin/python3"

mkdir -p "$work/probe/tools"
# a check that fails, exactly as check_stack.py does on a real failure
cat > "$work/probe/tools/check_stack.py" <<'PY'
import sys
print("FAIL  CORS               no Access-Control-Allow-Origin")
print("1 FAILED, 0 warnings")
sys.exit(1)
PY
mkdir -p "$work/probepass/tools"
cat > "$work/probepass/tools/check_stack.py" <<'PY'
import sys
print("All checks passed")
sys.exit(0)
PY

probe_rc() { ( cd "$1" && PATH="$work/bin:$PATH" bash "$work/probe.sh" >/dev/null 2>&1 ); echo $?; }

# Assert the SPECIFIC exit code, not merely "non-zero": 127 (command not
# found) is also non-zero and would let a broken test look like a fix.
failing_rc=$(probe_rc "$work/probe")
if [ "$failing_rc" = "1" ]; then
  pass "a failing check makes the probe step exit 1 (propagated, not masked)"
else
  fail "probe step exited $failing_rc, expected 1 — pipefail missing, or the check never ran"
fi

passing_rc=$(probe_rc "$work/probepass")
if [ "$passing_rc" = "0" ]; then
  pass "a passing check still exits 0"
else
  fail "a passing check exited $passing_rc"
fi

if [ -s "$work/probe/report.txt" ]; then
  pass "report.txt is still written for the artifact"
else
  fail "report.txt was not written"
fi

echo
echo "── 2. alert dedupe ─────────────────────────────────────────────────────"

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
  fail "the two fixtures must fingerprint differently, else case D proves nothing"
fi

# expect_action: CREATE | COMMENT | NONE
run_case() {
  local label="$1" expect="$2" issue_number="$3" comments="$4" report="$5"
  rm -rf "$work/state"; mkdir -p "$work/state"
  [ -n "$issue_number" ] && printf '%s\n' "$issue_number" > "$work/state/issue_number"
  printf '%s' "$comments" > "$work/state/comments"
  : > "$work/log"
  cp "$report" "$work/report.txt"   # the probe writes it here; the alert step reads it

  ( cd "$work" && PATH="$work/bin:$PATH" GH_STUB_STATE="$work/state" \
      GH_STUB_LOG="$work/log" bash "$work/open.sh" ) > "$work/out.txt" 2>&1
  local rc=$?
  local got; got=$(tr -d '\n' < "$work/log" 2>/dev/null)
  local want="$expect"; [ "$want" = "NONE" ] && want=""

  if [ "$got" = "$want" ] && [ "$rc" = "0" ]; then
    pass "$label  (gh: ${got:-silent})"
  else
    fail "$label — expected ${want:-silent}, got ${got:-silent} (exit $rc)"
    sed 's/^/        | /' "$work/out.txt"
  fi
}

run_case "A. no open issue             -> create"  CREATE  ""  "" "$work/fixtures/report.txt"
run_case "B. open issue, new fingerprint -> comment" COMMENT "7" \
  "<!-- stack-monitor-fp:deadbeefdead -->" "$work/fixtures/report.txt"
run_case "C. same failure again          -> silent"  NONE    "7" \
  "<!-- stack-monitor-fp:${fp} -->" "$work/fixtures/report.txt"
run_case "D. failure changed since last  -> comment" COMMENT "7" \
  "<!-- stack-monitor-fp:${fp} -->" "$work/fixtures/report-changed.txt"

echo
echo "── 3. the engine health grade reads the nested flags ───────────────────"

# Each case below is a payload shape the engine actually produces. The live
# one is first: 81 days up, qgis found, no database.
engine_case() {
  local label="$1" payload="$2" want="$3" got
  got=$("$REAL_PY" - "$repo_dir" "$payload" <<'PY'
import importlib.util, json, pathlib, sys
repo, payload = pathlib.Path(sys.argv[1]), sys.argv[2]
# check_stack.py is a script under tools/, not an installed module; import it by path.
spec = importlib.util.spec_from_file_location("check_stack", repo / "tools" / "check_stack.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
r = mod.engine_health_result(json.loads(payload), 288.0)
print(f"{r.status}|{r.detail}")
PY
)
  if [ "$got" = "$want" ]; then
    pass "$label"
  else
    fail "$label — expected '$want', got '$got'"
  fi
}

engine_case "postgis.available=false is DOWN/WARN, never a silent 'postgis ok'" \
  '{"uptime_seconds":7000725,"qgis_process":"/usr/bin/qgis_process","postgis":{"available":false}}' \
  'WARN|up 81.0d, qgis ok, postgis DOWN, 288ms'
engine_case "postgis.available=true still PASSes" \
  '{"uptime_seconds":7000725,"qgis_process":"/usr/bin/qgis_process","postgis":{"available":true}}' \
  'PASS|up 81.0d, qgis ok, postgis ok, 288ms'
engine_case "a bare boolean from an older engine build still reads" \
  '{"postgis":false}' \
  'WARN|qgis MISSING, postgis DOWN, 288ms'
engine_case "a null qgis_process is reported, not dropped" \
  '{"qgis_process":null,"postgis":{"available":true}}' \
  'WARN|qgis MISSING, postgis ok, 288ms'
engine_case "neither key present is not a PASS" \
  '{}' \
  'WARN|qgis MISSING, 288ms'

echo
if [ "$failures" -eq 0 ]; then
  echo "all checks passed"
  exit 0
fi
echo "$failures check(s) failed"
exit 1
