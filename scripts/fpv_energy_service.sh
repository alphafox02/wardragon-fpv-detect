#!/bin/bash
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

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_CTL="${ROOT_DIR}/scripts/service_controller.sh"
SCAN_SCRIPT="${ROOT_DIR}/scripts/fpv_energy_scan.py"
FPV_DJI_GUARD="${FPV_DJI_GUARD:-1}"
FPV_DJI_GUARD_INTERVAL="${FPV_DJI_GUARD_INTERVAL:-30}"
FPV_OSMOSDR_ARGS="${FPV_OSMOSDR_ARGS:-}"
FPV_PLUTO_URI="${FPV_PLUTO_URI:-}"
FPV_DJI_GUARD_VERBOSE="${FPV_DJI_GUARD_VERBOSE:-0}"

guard_pid=""

for arg in "$@"; do
  case "$arg" in
    -d|--debug)
      FPV_DJI_GUARD_VERBOSE=1
      ;;
  esac
done

# Export so service_controller.sh can see it
export FPV_DJI_GUARD_VERBOSE

start_guard() {
  if [ "${FPV_DJI_GUARD}" != "1" ]; then
    return
  fi
  if [ ! -x "${SERVICE_CTL}" ]; then
    return
  fi
  while true; do
    "${SERVICE_CTL}" stop || true
    sleep "${FPV_DJI_GUARD_INTERVAL}"
  done &
  guard_pid="$!"
}

cleanup() {
  if [ -n "${guard_pid}" ]; then
    kill "${guard_pid}" 2>/dev/null || true
  fi
  if [ -x "${SERVICE_CTL}" ]; then
    "${SERVICE_CTL}" start || true
  fi
}

trap cleanup EXIT

if [ -x "${SERVICE_CTL}" ]; then
  "${SERVICE_CTL}" stop || true
fi

start_guard

extra_args=()
if [ -n "${FPV_OSMOSDR_ARGS}" ]; then
  extra_args+=(--osmosdr-args "${FPV_OSMOSDR_ARGS}")
fi
if [ -n "${FPV_PLUTO_URI}" ]; then
  extra_args+=(--pluto-uri "${FPV_PLUTO_URI}")
fi

python3 "${SCAN_SCRIPT}" "$@" "${extra_args[@]}"
