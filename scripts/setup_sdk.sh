#!/bin/bash
# Prepara a dylib arm64 da SVBony para uso via ctypes.
#
# A lib do repo indi-3rdparty para Mac é x86_64 apenas, inútil em Apple Silicon
# nativo. A que acompanha o AstroDMx é arm64 e da mesma SDK (v1.13.4 / API
# 3.0.0), então é essa que usamos em desenvolvimento. Para distribuir, baixe a
# SDK oficial da SVBony.
#
# Dois detalhes que quebram tudo se ignorados:
#  - a dylib referencia libusb via @executable_path/../Resources/lib, caminho que
#    só existe dentro do bundle do AstroDMx; reescrevemos para @loader_path
#  - em arm64, alterar uma dylib invalida a assinatura e o dyld recusa carregar;
#    é obrigatório reassinar ad-hoc
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${SVB_SDK_SRC:-/Applications/AstroDMx Capture.app/Contents/Resources/lib}"
DEST="$ROOT/vendor/lib"

[ -f "$SRC/libSVBCameraSDK.dylib" ] || { echo "SDK não encontrada em $SRC"; exit 1; }
mkdir -p "$DEST"
cp "$SRC/libSVBCameraSDK.dylib" "$SRC/libusb-1.0.0.dylib" "$DEST/"
chmod u+w "$DEST"/*.dylib
cd "$DEST"
install_name_tool -change "@executable_path/../Resources/lib/libusb-1.0.0.dylib" \
                          "@loader_path/libusb-1.0.0.dylib" libSVBCameraSDK.dylib
install_name_tool -id "@loader_path/libSVBCameraSDK.dylib" libSVBCameraSDK.dylib
install_name_tool -id "@loader_path/libusb-1.0.0.dylib" libusb-1.0.0.dylib
codesign --force -s - libSVBCameraSDK.dylib libusb-1.0.0.dylib
lipo -info libSVBCameraSDK.dylib
echo "ok"
