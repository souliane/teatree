#!/bin/zsh
# Register a no-Python, unattended macOS host pressure sampler for Docker workers.
set -e
[[ "$(uname -s)" == Darwin ]] || exit 0

script_dir="${0:A:h}"
publisher="${script_dir:h}/hooks/scripts/host-pressure-publish.zsh"
[[ -x "$publisher" ]] || { print -u2 "host pressure publisher missing or not executable: $publisher"; exit 1; }
host_root="${TEATREE_HOST_HOME:-$HOME}"
data_dir="${T3_LOOP_REGISTRY_DIR:-${XDG_DATA_HOME:-$host_root/.local/share}/teatree}"
agents_dir="$host_root/Library/LaunchAgents"
plist="$agents_dir/com.teatree.host-pressure.plist"
mkdir -p -- "$agents_dir"
umask 077
temporary="$plist.$$"
trap 'rm -f -- "$temporary"' EXIT
xml_publisher="$(print -r -- "$publisher" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
xml_home="$(print -r -- "$host_root" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
xml_data="$(print -r -- "$data_dir" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
mirror_dir="${T3_HOST_PRESSURE_MIRROR_DIR:-$host_root/.local/share/teatree-host-pressure}"
mkdir -p -- "$mirror_dir"
xml_mirror="$(print -r -- "$mirror_dir" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
cat >"$temporary" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.teatree.host-pressure</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>$xml_publisher</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>TEATREE_HOST_HOME</key><string>$xml_home</string>
    <key>T3_LOOP_REGISTRY_DIR</key><string>$xml_data</string>
    <key>T3_HOST_PRESSURE_MIRROR_DIR</key><string>$xml_mirror</string>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartInterval</key><integer>15</integer>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
if [[ ! -f "$plist" ]] || ! cmp -s -- "$temporary" "$plist"; then
    mv -f -- "$temporary" "$plist"
    launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
else
    rm -f -- "$temporary"
    if ! launchctl print "gui/$(id -u)/com.teatree.host-pressure" >/dev/null 2>&1; then
        launchctl bootstrap "gui/$(id -u)" "$plist"
    fi
fi
launchctl kickstart -k "gui/$(id -u)/com.teatree.host-pressure"
print "host pressure: launchd publisher active (15-second cadence)"
