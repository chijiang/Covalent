#!/bin/zsh
set -eu

source_app="${0:A:h}/Covalent Desktop.app"
target_dir="$HOME/Applications"
target_app="$target_dir/Covalent Desktop.app"

if [[ ! -d "$source_app" ]]; then
  print -u2 "Covalent Desktop.app was not found next to this installer."
  exit 1
fi

if pgrep -x "Covalent Desktop" >/dev/null 2>&1; then
  print -u2 "Quit Covalent Desktop before installing an update, then run this again."
  exit 1
fi

mkdir -p "$target_dir"
work_dir="$(mktemp -d "$target_dir/.covalent-install.XXXXXX")"
installed=false
cleanup() {
  if [[ "$installed" != true && -d "$work_dir/previous.app" && ! -e "$target_app" ]]; then
    mv "$work_dir/previous.app" "$target_app"
  fi
  rm -rf -- "$work_dir"
}
trap cleanup EXIT

ditto "$source_app" "$work_dir/Covalent Desktop.app"
if [[ -e "$target_app" ]]; then
  mv "$target_app" "$work_dir/previous.app"
fi
mv "$work_dir/Covalent Desktop.app" "$target_app"
installed=true

print "Installed Covalent Desktop for this user in $target_dir."
open -R "$target_app"
