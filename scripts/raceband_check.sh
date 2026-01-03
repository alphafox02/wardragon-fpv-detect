#!/usr/bin/env bash
# Copyright 2025-2026 CEMAXECUTER LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -u -o pipefail

SUSCLI_BIN=${SUSCLI_BIN:-suscli}
PROFILE=${PROFILE:-fpv58_race_8m}
BANDWIDTH=${BANDWIDTH:-1.8e6}
DT=${DT:-0.2}
Q=${Q:-10}
DURATION=${DURATION:-8}
RETRIES=${RETRIES:-2}
FORMATTER=${FORMATTER:-json}

# 5.9-ish raceband centers to probe directly.
FREQS=(
  5905e6
  5917e6
  5925e6
  5945e6
)

declare -A MAX_PAL
declare -A MAX_NTSC
declare -A HITS_100
declare -A ATTEMPTS

max_float() {
  awk -v a="$1" -v b="$2" 'BEGIN{if (b > a) print b; else print a}'
}

is_ge() {
  awk -v v="$1" -v t="$2" 'BEGIN{exit !(v >= t)}'
}

run_probe() {
  local freq="$1"
  local attempt=1
  local max_pal="0"
  local max_ntsc="0"
  local hit_100="0"

  while (( attempt <= RETRIES + 1 )); do
    local tmp
    tmp="$(mktemp)"

    local cmd=(
      "$SUSCLI_BIN" fpvdet
      --profile="$PROFILE"
      --bandwidth="$BANDWIDTH"
      --dt="$DT"
      --q="$Q"
      --formatter="$FORMATTER"
      --frequency="$freq"
    )

    timeout "${DURATION}s" "${cmd[@]}" >"$tmp" 2>/dev/null
    local status=$?

    local lines=0
    while IFS= read -r line; do
      if [[ "$line" == *'"signal":{'* ]]; then
        local vals
        vals="$(sed -n 's/.*"signal":{"pal":\\([0-9.e+-]*\\),"ntsc":\\([0-9.e+-]*\\)}.*/\\1 \\2/p' <<<"$line")"
        if [[ -n "$vals" ]]; then
          local pal ntsc
          pal="${vals%% *}"
          ntsc="${vals##* }"
          max_pal="$(max_float "$max_pal" "$pal")"
          max_ntsc="$(max_float "$max_ntsc" "$ntsc")"
          lines=$((lines + 1))
        fi
      fi
    done < "$tmp"

    rm -f "$tmp"

    # Treat timeout as expected if we got any output.
    if (( lines > 0 )); then
      break
    fi

    # Retry on empty output or crash.
    if (( status == 124 || status == 139 || status != 0 )); then
      attempt=$((attempt + 1))
      continue
    fi
    break
  done

  if is_ge "$max_pal" 100 || is_ge "$max_ntsc" 100; then
    hit_100="1"
  fi

  MAX_PAL["$freq"]="$max_pal"
  MAX_NTSC["$freq"]="$max_ntsc"
  HITS_100["$freq"]="$hit_100"
  ATTEMPTS["$freq"]="$attempt"
}

echo "Raceband spot-check (${DURATION}s each, retries=$RETRIES)"
echo "Profile=${PROFILE} BW=${BANDWIDTH} DT=${DT} Q=${Q}"
echo

for freq in "${FREQS[@]}"; do
  echo "Checking ${freq}..."
  run_probe "$freq"
done

echo
echo "Summary:"
for freq in "${FREQS[@]}"; do
  pal="${MAX_PAL[$freq]:-0}"
  ntsc="${MAX_NTSC[$freq]:-0}"
  hit="${HITS_100[$freq]:-0}"
  attempts="${ATTEMPTS[$freq]:-1}"
  if [[ "$hit" == "1" ]]; then
    printf "%s  pal=%s  ntsc=%s  HIT=100  attempts=%s\n" "$freq" "$pal" "$ntsc" "$attempts"
  else
    printf "%s  pal=%s  ntsc=%s  hit=0     attempts=%s\n" "$freq" "$pal" "$ntsc" "$attempts"
  fi
done
