#!/usr/bin/env bash
# Headed acceptance probe for the Camoufox scroll_into_view path.
#
# Reproduces the failure that makes scrollintoview unusable: Playwright's
# locator.scroll_into_view_if_needed waits for actionability, so a target that
# is hidden, zero-sized, or absent never resolves and the 22s worker deadline
# fires, poisoning the session. A visible offscreen target must scroll; a
# non-actionable target must fail fast without poisoning.
#
# The probe generates its own local page and uses its own session. It never
# touches the shared opencode-camoufox session.
set -u

REPO="${AGENT_BROWSER_REPO:-/Users/braito/Documents/Code/Projects/agent-browser-camoufox/agent-browser}"
BIN="${AGENT_BROWSER_BIN:-$REPO/cli/target/release/agent-browser}"
SESSION="${SCROLL_TEST_SESSION:-scroll-probe}"
ENGINE_ARGS=(--engine camoufox --session "$SESSION" --headed true)

WORK="$(mktemp -d "${TMPDIR:-/tmp}/scroll-probe.XXXXXX")"
PAGE="$WORK/probe.html"
cat > "$PAGE" <<'HTML'
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Scroll Into View Probe</title>
<style>
  body { margin: 0; font-family: sans-serif; }
  .spacer { height: 2000px; background: repeating-linear-gradient(#eee 0 40px, #ddd 40px 80px); }
  .box { margin: 40px; padding: 30px; width: 300px; background: #cce; border: 2px solid #36c; }
  #scroller { height: 300px; width: 400px; overflow: auto; border: 3px solid #c33; margin: 40px; }
  #scroller .inner { height: 2500px; background: linear-gradient(#fee, #eff); }
  #scroller #nested { margin-top: 2000px; padding: 20px; background: #6c6; }
  #target { margin: 40px; padding: 30px; width: 300px; background: #cec; border: 2px solid #393; }
  #wide { width: 4000px; height: 100px; background: #ffc; margin-top: 100px; }
  #wide #far-right { margin-left: 3500px; padding: 10px; background: #f9c; display: inline-block; }
  #hidden { display: none; }
  #zero { width: 0; height: 0; overflow: hidden; }
</style>
</head>
<body>
  <h1 id="top">Probe</h1>
  <div class="box" id="inviewelem">ALREADY IN VIEW</div>
  <div id="scroller">
    <div class="inner">
      <div id="nested">NESTED SCROLL CONTAINER TARGET</div>
    </div>
  </div>
  <div class="spacer"></div>
  <div id="target">OFFSCREEN TARGET</div>
  <div id="wide">
    <span id="far-right">FAR RIGHT HORIZONTAL TARGET</span>
  </div>
  <div class="box" id="last">LAST BOX</div>
  <div id="hidden">HIDDEN TARGET</div>
  <div id="zero">ZERO BOX TARGET</div>
  <div id="footer">FOOTER END</div>
</body>
</html>
HTML

pass=0
fail=0
notes=()

run() {
  "$BIN" "${ENGINE_ARGS[@]}" "$@" --json 2>&1
}

json_field() {
  printf '%s' "$1" | jq -r "$2" 2>/dev/null
}

elapsed_since() {
  python3 -c "import time;print(f'{time.time()-$1:.2f}')"
}

assert_visible_target() {
  local label="$1" selector="$2" probe="$3"
  local out ok probe_out value
  out="$(run scrollintoview "$selector")"
  ok="$(json_field "$out" '.success')"
  if [[ "$ok" != "true" ]]; then
    fail=$((fail + 1))
    notes+=("FAIL  $label: scrollintoview returned success=$ok :: $(json_field "$out" '.error')")
    return
  fi
  probe_out="$(run eval "$probe")"
  value="$(json_field "$probe_out" '.data.result')"
  if [[ "$value" == "true" ]]; then
    pass=$((pass + 1))
    notes+=("pass  $label: scrolled and target within viewport")
  else
    fail=$((fail + 1))
    notes+=("FAIL  $label: reported scrolled but probe=$value")
  fi
}

assert_fast_invalid() {
  local label="$1" selector="$2" max="$3"
  local start out code poisoned elapsed
  start="$(python3 -c 'import time;print(time.time())')"
  out="$(run scrollintoview "$selector")"
  elapsed="$(elapsed_since "$start")"
  code="$(json_field "$out" '.code // empty')"
  poisoned="$(json_field "$out" '.data.poisoned // empty')"
  if [[ "$(json_field "$out" '.success')" == "true" ]]; then
    fail=$((fail + 1))
    notes+=("FAIL  $label: expected failure but got success")
    return
  fi
  if [[ "$(python3 -c "print(1 if $elapsed > $max else 0)")" == "1" ]]; then
    fail=$((fail + 1))
    notes+=("FAIL  $label: took ${elapsed}s (limit ${max}s), code=$code poisoned=$poisoned")
    return
  fi
  if [[ "$code" == "camoufox_timeout" || "$poisoned" == "true" ]]; then
    fail=$((fail + 1))
    notes+=("FAIL  $label: failed but timed out/poisoned (code=$code poisoned=$poisoned) in ${elapsed}s")
    return
  fi
  pass=$((pass + 1))
  notes+=("pass  $label: fast non-poisoning rejection (code=$code) in ${elapsed}s")
}

cleanup() {
  run close >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "scroll-intoview headed probe"
echo "  binary:  $BIN"
echo "  session: $SESSION"
echo "  page:    $PAGE"
echo

if [[ ! -x "$BIN" ]]; then
  echo "binary not found or not executable: $BIN" >&2
  exit 2
fi

open_out="$(run open "file://$PAGE")"
if [[ "$(json_field "$open_out" '.success')" != "true" ]]; then
  echo "failed to open test page: $(json_field "$open_out" '.error')" >&2
  exit 2
fi

assert_visible_target "vertical offscreen (#target)" "#target" \
  "(() => { const r = document.getElementById('target').getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight; })()"
assert_visible_target "horizontal offscreen (#far-right)" "#far-right" \
  "(() => { const r = document.getElementById('far-right').getBoundingClientRect(); return r.left >= 0 && r.right <= window.innerWidth; })()"
assert_visible_target "nested scroll (#nested)" "#nested" \
  "(() => { const r = document.getElementById('nested').getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight; })()"
assert_visible_target "page footer (#footer)" "#footer" \
  "(() => { const r = document.getElementById('footer').getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight; })()"

snap_out="$(run snapshot)"
ref="$(printf '%s' "$snap_out" | jq -r '.data.snapshot // empty' 2>/dev/null | grep 'FOOTER END' | grep -oE 'ref=e[0-9]+' | head -1)"
ref="${ref#ref=}"
if [[ -n "$ref" ]]; then
  ref="@$ref"
else
  ref="$(printf '%s' "$snap_out" | grep 'FOOTER END' | grep -oE 'ref=e[0-9]+' | head -1)"
  ref="${ref#ref=}"
  [[ -n "$ref" ]] && ref="@$ref"
fi
if [[ -n "$ref" ]]; then
  assert_visible_target "snapshot ref ($ref)" "$ref" \
    "(() => { const r = document.getElementById('footer').getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight; })()"
else
  notes+=("skip  snapshot ref case: no @e ref returned")
fi

assert_fast_invalid "absent selector (#does-not-exist)" "#does-not-exist" 6
assert_fast_invalid "hidden target (#hidden)" "#hidden" 6
assert_fast_invalid "zero-sized target (#zero)" "#zero" 6
assert_fast_invalid "ambiguous selector (.box x2)" ".box" 6

post="$(run eval "1 + 1")"
if [[ "$(json_field "$post" '.success')" == "true" ]]; then
  pass=$((pass + 1))
  notes+=("pass  session still usable after rejections")
else
  fail=$((fail + 1))
  notes+=("FAIL  session unusable after rejections :: $(json_field "$post" '.error')")
fi

echo "results: $pass passed, $fail failed"
for line in "${notes[@]}"; do
  echo "  $line"
done

[[ "$fail" -eq 0 ]]
