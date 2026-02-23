#!/usr/bin/env bash
# GitHub 저장소에 version 라벨을 생성합니다.
#
# 사전 조건:
#   - gh CLI 설치 및 인증 필요 (gh auth login)
#   - 저장소 루트에서 실행하거나 gh CLI가 저장소를 자동 감지
#
# 사용법:
#   bash scripts/setup-labels.sh
#
# --force 옵션: 라벨이 이미 존재해도 덮어씁니다 (멱등성 보장)

set -euo pipefail

echo "GitHub 저장소에 version 라벨을 생성합니다..."

gh label create "version:major" \
  --color "d73a4a" \
  --description "Breaking changes - bumps major version" \
  --force

gh label create "version:minor" \
  --color "0075ca" \
  --description "New features - bumps minor version" \
  --force

gh label create "version:patch" \
  --color "2ea44f" \
  --description "Bug fixes - bumps patch version" \
  --force

echo "Labels created successfully:"
echo "  - version:major  (red)   Breaking changes"
echo "  - version:minor  (blue)  New features"
echo "  - version:patch  (green) Bug fixes"
