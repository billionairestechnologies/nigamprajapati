#!/usr/bin/env bash
# Double-click this file in Finder to launch the dashboard - it opens a
# Terminal window and runs the same setup as run.sh, but keeps that window
# open on success or failure so you can always read what happened.
cd "$(dirname "$0")"
./run.sh
echo ""
read -r -p "Press Enter to close this window..." _ || true
