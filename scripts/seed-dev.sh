#!/usr/bin/env bash
# seed_dev — the general seeding toolkit (companion to reset-dev). This wrapper is mainly for
# the inspector; seeding is done by importing seed_dev (see its docstring) or running a fixture
# like docs/demos/seed.py.
#
#   bash scripts/seed-dev.sh --check   # print the current trial balance + income statement
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/seed_dev.py "$@"
