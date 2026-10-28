#!/bin/zsh
set -eu
cd -- "${0:A:h}"
APP_BUNDLE="$PWD/Igloo.app"
BUILD_ARCH="${1:-native}"
BUILD_RUNTIME="${2:-bundled}"
case "$BUILD_RUNTIME" in bundled|system) ;; *) print -u2 "Choose bundled or system Python."; exit 2 ;; esac
case "$BUILD_ARCH" in
  native) BUILD_ARCH="$(uname -m)" ;;
  arm64|x86_64|universal) ;;
  *) print -u2 "Usage: ./build.command [native|arm64|x86_64|universal]"; exit 2 ;;
esac
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources/backend"
IGLOO_PYTHON="$(command -v python3)"
cp backend/organizer.py backend/lifecycle.py backend/preferences.py backend/planning.py backend/classification.py backend/automation.py backend/task_cli.py LICENSE "$APP_BUNDLE/Contents/Resources/backend/"
cp LICENSE "$APP_BUNDLE/Contents/Resources/LICENSE"
cp scripts/update_installer.py "$APP_BUNDLE/Contents/Resources/"
cp assets/demo-catalog.json assets/demo-tasks.json "$APP_BUNDLE/Contents/Resources/"
"$IGLOO_PYTHON" scripts/build_icon.py assets/IglooIcon.png "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
if [[ "$BUILD_RUNTIME" == bundled ]]; then
  "$IGLOO_PYTHON" scripts/bundle_python.py "$PWD" "$APP_BUNDLE" "$BUILD_ARCH"
else
  rm -rf -- "$APP_BUNDLE/Contents/Resources/python"
fi
BUILD_TMP="$(mktemp -d)"
trap 'rm -rf -- "$BUILD_TMP"' EXIT
SDK_PATH="$(xcrun --sdk macosx --show-sdk-path)"
SOURCES=(app/Igloo.swift app/LifecycleViews.swift app/ReviewViews.swift app/PreferencesViews.swift app/SpaceViews.swift app/WizardViews.swift app/AboutView.swift app/DetailViews.swift app/NavigationViews.swift app/TasksView.swift app/DemoCatalog.swift app/ScopeInspector.swift app/UpdateInstaller.swift app/Updates.swift)
for TARGET_ARCH in arm64 x86_64; do
  if [[ "$BUILD_ARCH" == "$TARGET_ARCH" || "$BUILD_ARCH" == universal ]]; then
    /usr/bin/swiftc -swift-version 5 -O -parse-as-library -sdk "$SDK_PATH" -target "$TARGET_ARCH-apple-macos14.0" "${SOURCES[@]}" -o "$BUILD_TMP/Igloo-$TARGET_ARCH" -framework SwiftUI -framework AppKit
  fi
done
if [[ "$BUILD_ARCH" == universal ]]; then
  /usr/bin/lipo -create "$BUILD_TMP/Igloo-arm64" "$BUILD_TMP/Igloo-x86_64" -output "$APP_BUNDLE/Contents/MacOS/Igloo"
else
  cp "$BUILD_TMP/Igloo-$BUILD_ARCH" "$APP_BUNDLE/Contents/MacOS/Igloo"
fi
"$IGLOO_PYTHON" -c 'from pathlib import Path; import json,os,plistlib,sys,re
r=json.loads(Path("release.json").read_text())
if not re.fullmatch(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})(\.(0|[1-9][0-9]{0,5}))?",r["version"]) or type(r["build"]) is not int or r["build"]<1:raise SystemExit("Invalid release metadata")
release=os.environ.get("IGLOO_RELEASE_BUILD") == "1"
name="Igloo" if release else "Igloo Dev"
identifier="local.igloo" if release else "local.igloo.dev"
Path(sys.argv[1]).write_bytes(plistlib.dumps({
"CFBundleName":name,"CFBundleDisplayName":name,"CFBundleIdentifier":identifier,
"CFBundleVersion":str(r["build"]),"CFBundleShortVersionString":r["version"],
"IglooRuntime":sys.argv[2],"CFBundleExecutable":"Igloo","CFBundleIconFile":"AppIcon.icns","CFBundlePackageType":"APPL",
"LSMinimumSystemVersion":r["minimumSystemVersion"],"NSHighResolutionCapable":True
}))' "$APP_BUNDLE/Contents/Info.plist" "$BUILD_RUNTIME"
# Sign nested Python executables and extension modules before the outer app.
"$IGLOO_PYTHON" -c 'from pathlib import Path; import subprocess,sys
app=Path(sys.argv[1]);identity=sys.argv[2]
magic={bytes.fromhex(x) for x in ["feedface","cefaedfe","feedfacf","cffaedfe","cafebabe","bebafeca","cafebabf","bfbafeca"]}
for p in sorted((app/"Contents/Resources/python").rglob("*")):
 if p.is_file() and not p.is_symlink():
  with p.open("rb") as f: head=f.read(4)
  if head in magic:
   args=["/usr/bin/codesign","--force","--sign",identity]
   if identity!="-":args+=["--options","runtime","--timestamp"]
   subprocess.run(args+[str(p)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
' "$APP_BUNDLE" "${IGLOO_SIGNING_IDENTITY:--}"
if [[ "${IGLOO_SIGNING_IDENTITY:--}" == "-" ]]; then
  /usr/bin/codesign --force --sign - "$APP_BUNDLE"
else
  /usr/bin/codesign --force --options runtime --timestamp --sign "$IGLOO_SIGNING_IDENTITY" "$APP_BUNDLE"
fi
print "Built standalone app: $APP_BUNDLE ($BUILD_ARCH)"
