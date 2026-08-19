#!/bin/bash
# Baixa o OpenNGC (NGC + IC, ~14 mil objetos). CC-BY-SA 4.0, Mattia Verga.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/data"
curl -sSL -o "$ROOT/data/NGC.csv" \
  "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv"
wc -l "$ROOT/data/NGC.csv"
