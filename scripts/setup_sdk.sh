#!/bin/bash
# Prepare the SVBony arm64 dylib for use through ctypes.
#
# The macOS library in the indi-3rdparty repository is x86_64 only, which is
# useless on Apple Silicon natively. The one shipped with AstroDMx is arm64 and
# from the same SDK (v1.13.4 / API 3.0.0), so that is what development uses. For
# distribution, get the official SDK from SVBony.
#
# Two details that break everything if ignored:
#  - the dylib references libusb through @executable_path/../Resources/lib, a
#    path that only exists inside the AstroDMx bundle; we rewrite it to
#    @loader_path
#  - on arm64, modifying a dylib invalidates its signature and dyld refuses to
#    load it; re-signing ad hoc is mandatory
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${SVB_SDK_SRC:-/Applications/AstroDMx Capture.app/Contents/Resources/lib}"
DEST="$ROOT/vendor/lib"

[ -f "$SRC/libSVBCameraSDK.dylib" ] || {
  echo "SDK not found in $SRC"
  echo "Set SVB_SDK_SRC to the folder holding libSVBCameraSDK.dylib."
  exit 1
}
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
