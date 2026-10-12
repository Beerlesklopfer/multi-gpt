#!/bin/bash
# Baut aus .deb-Dateien ein apt-Repository (Suite "stable", Komponente "main").
#
#   scripts/apt-repo.sh <ordner-mit-debs> <zielordner>
#
# Ist APT_SIGNING_KEY gesetzt (privater OpenPGP-Schlüssel, ASCII-armored, ohne
# Passphrase), werden Release (InRelease, Release.gpg) signiert und der
# öffentliche Schlüssel als multi-gpt.gpg und multi-gpt.asc abgelegt. Ohne
# Schlüssel entsteht ein unsigniertes Repository (nur zum lokalen Testen).
# Braucht apt-ftparchive (apt-utils) und gpg.
set -euo pipefail

DEBS=$(realpath "$1")
OUT=$2
SUITE=stable
COMPONENT=main
ARCH=amd64

shopt -s nullglob
debs=("$DEBS"/*.deb)
[ ${#debs[@]} -gt 0 ] || { echo "apt-repo: keine .deb in $DEBS" >&2; exit 1; }

mkdir -p "$OUT/pool/$COMPONENT/m/multi-gpt" "$OUT/dists/$SUITE/$COMPONENT/binary-$ARCH"
cp "${debs[@]}" "$OUT/pool/$COMPONENT/m/multi-gpt/"
cd "$OUT"

apt-ftparchive packages pool > "dists/$SUITE/$COMPONENT/binary-$ARCH/Packages"
gzip -9nkf "dists/$SUITE/$COMPONENT/binary-$ARCH/Packages"

apt-ftparchive \
  -o APT::FTPArchive::Release::Origin=MultiGPT \
  -o APT::FTPArchive::Release::Label=MultiGPT \
  -o APT::FTPArchive::Release::Suite=$SUITE \
  -o APT::FTPArchive::Release::Codename=$SUITE \
  -o APT::FTPArchive::Release::Architectures=$ARCH \
  -o APT::FTPArchive::Release::Components=$COMPONENT \
  -o "APT::FTPArchive::Release::Description=MultiGPT – Debian-Pakete" \
  release "dists/$SUITE" > "dists/$SUITE/Release.tmp"
mv "dists/$SUITE/Release.tmp" "dists/$SUITE/Release"

if [ -n "${APT_SIGNING_KEY:-}" ]; then
  GNUPGHOME=$(mktemp -d)
  export GNUPGHOME
  trap 'rm -rf "$GNUPGHOME"' EXIT
  printf '%s\n' "$APT_SIGNING_KEY" | gpg --batch --quiet --import
  fpr=$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" {print $10; exit}')
  gpg --batch --yes --local-user "$fpr" --digest-algo SHA512 \
    --clearsign -o "dists/$SUITE/InRelease" "dists/$SUITE/Release"
  gpg --batch --yes --local-user "$fpr" --digest-algo SHA512 \
    --armor --detach-sign -o "dists/$SUITE/Release.gpg" "dists/$SUITE/Release"
  gpg --batch --yes --export "$fpr" > multi-gpt.gpg
  gpg --batch --yes --armor --export "$fpr" > multi-gpt.asc
  echo "apt-repo: signiert mit $fpr"
else
  echo "apt-repo: APT_SIGNING_KEY fehlt – Repository bleibt unsigniert." >&2
fi

echo "apt-repo: $(ls pool/$COMPONENT/m/multi-gpt | tr '\n' ' ')"
