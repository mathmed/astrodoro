#!/bin/bash
# Índices do astrometry.net para este equipamento.
#
# SV405CC (pixel 4,63 um) em dobsoniano de 1200 mm:
#   bin1 -> 0,796"/px    bin2 -> 1,592"/px    campo 55,0' x 37,4' nos dois
#
# O solver casa quadriláteros de estrelas, e um quad precisa CABER na imagem.
# Como o lado curto do campo tem 37', as faixas úteis são:
#
#   index-4208  30-42'   81 MB   arquivo único   <- o principal, cai no lado curto
#   index-4209  42-60'   41 MB   arquivo único   <- cabe no lado longo
#   index-4207  22-30'  165 MB   12 arquivos     <- camada de robustez
#   index-4206  16-22'  328 MB   12 arquivos     <- raramente necessário
#   index-4210  60-85'   20 MB   arquivo único   <- maior que o campo, inútil aqui
#
# Atenção: 4206 e 4207 vêm fatiados em 12 arquivos healpix; 4208 em diante são
# arquivo único. Pedir "index-4207.fits" devolve 404.
#
# uso:  scripts/get_indexes.sh [minimo|robusto] [--simular|--confirmar]
#         sem flag   -> pergunta antes de baixar
#         --simular   -> só mostra o que baixaria, não baixa
#         --confirmar -> baixa sem perguntar
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${ASTROMETRY_INDEX_DIR:-$ROOT/data/indexes}"
BASE="http://data.astrometry.net/4200"
NIVEL="${1:-minimo}"
AUTO=""; DRY=""
for a in "$@"; do
  [ "$a" = "--confirmar" ] && AUTO=1
  [ "$a" = "--simular" ] && DRY=1
done

SINGLE="index-4208.fits index-4209.fits"
SPLIT=""
if [ "$NIVEL" = "robusto" ]; then
  for i in $(seq -w 0 11); do SPLIT="$SPLIT index-4207-$i.fits"; done
fi

echo "nível: $NIVEL"
echo "destino: $DEST"
total=0
for f in $SINGLE $SPLIT; do
  sz=$(curl -sS --max-time 25 -r 0-0 -D - -o /dev/null "$BASE/$f" 2>/dev/null \
       | grep -i '^content-range' | tr -d '\r' | sed 's|.*/||')
  [ -n "${sz:-}" ] && total=$((total+sz))
done
echo "arquivos: $(echo $SINGLE $SPLIT | wc -w | tr -d ' ')   total ~$((total/1000000)) MB"

for f in $SINGLE $SPLIT; do echo "  $f"; done
if [ -n "$DRY" ]; then echo "(--simular: nada baixado)"; exit 0; fi
if [ -z "$AUTO" ]; then
  read -r -p "baixar? [s/N] " ans
  [ "$ans" = "s" ] || { echo "cancelado"; exit 0; }
fi

mkdir -p "$DEST"
for f in $SINGLE $SPLIT; do
  if [ -f "$DEST/$f" ]; then echo "  $f já existe"; continue; fi
  echo "  baixando $f"
  curl -# --fail -o "$DEST/$f.part" "$BASE/$f" && mv "$DEST/$f.part" "$DEST/$f"
done

CFG="$(brew --prefix 2>/dev/null || echo /usr/local)/etc/astrometry.cfg"
echo
echo "baixados em $DEST"
echo "agora aponte o solver para a pasta. Em $CFG, garanta:"
echo "    add_path $DEST"
echo "    autoindex"
echo "e confira com:  solve-field --help >/dev/null && echo ok"
