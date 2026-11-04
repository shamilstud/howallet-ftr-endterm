#!/usr/bin/env bash
# rec.sh <name> <command...> : runs the command and saves "$ command" + output
# to evidence/<name>.txt so the report screenshots show real terminal output.
name=$1; shift
out="$(dirname "$0")/../evidence/$name.txt"
{ echo "\$ $*"; bash -c "$*" 2>&1; } | tee -a "$out"
