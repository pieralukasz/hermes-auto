#!/bin/bash
# Builds "Hermes Auto Bar.app" (menu bar only, no Dock icon) from this package.
# Usage: scripts/bundle-app.sh [release|debug] [output dir]
set -euo pipefail

CONFIG="${1:-release}"
OUT_DIR="${2:-.build}"
VERSION="0.1.0"
BUNDLE_ID="io.github.pieralukasz.hermes-auto-bar"
APP_DIR="$OUT_DIR/Hermes Auto Bar.app"

cd "$(dirname "$0")/.."
swift build -c "$CONFIG"
BINARY="$(swift build -c "$CONFIG" --show-bin-path)/hermes-auto-bar"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$BINARY" "$APP_DIR/Contents/MacOS/hermes-auto-bar"

cat > "$APP_DIR/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>hermes-auto-bar</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleName</key>
    <string>Hermes Auto Bar</string>
    <key>CFBundleDisplayName</key>
    <string>Hermes Auto Bar</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>26.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHumanReadableCopyright</key>
    <string>MIT License</string>
</dict>
</plist>
PLIST

# Pin the signature to the bundle id so macOS keeps any grants across rebuilds.
codesign --force --deep --sign - --identifier "$BUNDLE_ID" \
    --requirements "=designated => identifier \"$BUNDLE_ID\"" "$APP_DIR"

echo "Built $APP_DIR"
