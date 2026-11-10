#!/usr/bin/env bash
# Fetch the two upstream PDK repos at the commits this prototype was validated against.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d third_party_gf180mcu-pdk/.git ]; then
  git clone https://github.com/google/gf180mcu-pdk.git third_party_gf180mcu-pdk
fi
git -C third_party_gf180mcu-pdk checkout -q de3240d7529a6970437ac3344820aaae7839f215
if [ ! -d third_party_gf180mcu_fd_pr/.git ]; then
  git clone https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pr.git third_party_gf180mcu_fd_pr
fi
git -C third_party_gf180mcu_fd_pr checkout -q 9f992d5a9186d1f7820c58f039c484ad35b2edea
echo "Sources ready."
