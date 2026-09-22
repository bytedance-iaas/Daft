#!/usr/bin/env bash
# Run pytest with the given arguments; when it fails, show the tail of its output where the
# public run page shows it without signing in: an error annotation (the step logs need a
# GitHub login, and pytest's own annotations only cover failed tests, not collection errors or
# aborts), plus the job summary.
set -uo pipefail
log="$(mktemp)"
python -m pytest "$@" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ]; then
  tail_text="$(grep -v -i -E 'token|secret|password' "$log" | tail -n 30)"
  if [ -n "${GITHUB_ACTIONS:-}" ]; then
    # workflow-command escaping: % first, then CR and LF
    esc="${tail_text//'%'/'%25'}"
    esc="${esc//$'\r'/'%0D'}"
    esc="${esc//$'\n'/'%0A'}"
    echo "::error title=pytest $* (exit $rc)::${esc}"
  fi
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    { echo "### pytest $* (exit $rc)"; echo '```'; echo "$tail_text"; echo '```'; } >> "$GITHUB_STEP_SUMMARY"
  fi
fi
exit "$rc"
