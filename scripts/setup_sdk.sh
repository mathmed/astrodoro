#!/bin/bash
# Prepare the SVBony arm64 dylib for use through ctypes.
#
# SVBony's public download page only offers the Linux and Windows SDKs, and the
# macOS library in the indi-3rdparty repository is x86_64 only, which is
# useless on Apple Silicon natively. An arm64 build of the same SDK (v1.13.4 /
# API 3.0.0) is prepared once and committed in vendor/lib; this script only
# refreshes it from a folder given in SVB_SDK_SRC.
#
# Two details that break everything if ignored:
#  - the dylib references libusb through @executable_path/../Resources/lib, a
#    path that only exists inside the app bundle it came from; we rewrite it
#    to @loader_path
#  - on arm64, modifying a dylib invalidates its signature and dyld refuses to
#    load it; re-signing ad hoc is mandatory
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/vendor/lib"

if [ -z "${SVB_SDK_SRC:-}" ]; then
  if [ -f "$DEST/libSVBCameraSDK.dylib" ]; then
    echo "using the copy committed in vendor/lib"
    exit 0
  fi
  echo "vendor/lib/libSVBCameraSDK.dylib is missing."
  echo "Set SVB_SDK_SRC to the folder holding libSVBCameraSDK.dylib."
  exit 1
fi
SRC="$SVB_SDK_SRC"

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
