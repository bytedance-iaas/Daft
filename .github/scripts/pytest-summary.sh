#!/usr/bin/env bash
# Run pytest with the given arguments; when it fails, append the tail of its output to the
# job summary, which the public run page shows without signing in (the step logs need a
# GitHub login, the annotations only cover failed tests, not collection errors or aborts).
set -uo pipefail
log="$(mktemp)"
python -m pytest "$@" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ] && [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### pytest $* (exit $rc)"
    echo '```'
    grep -v -i -E 'token|secret|password' "$log" | tail -n 80
    echo '```'
  } >> "$GITHUB_STEP_SUMMARY"
fi
exit "$rc"
