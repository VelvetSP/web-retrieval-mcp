#!/usr/bin/env bash
# Provision the pinned Valkey binary used by isolated integration tests.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd -P)"
MANIFEST="$HERE/manifest.env"
CONFIG="$HERE/valkey.conf"
ARCHIVE_TOOL="$HERE/archive_tool.py"
VERIFIER="$HERE/verify_binary.py"
DEFAULT_PREFIX="${TMPDIR:-/tmp}/web-retrieval-valkey-fixture"
PREFIX="$DEFAULT_PREFIX"
PREFIX_GIVEN=0
APPLY=0
ROLLBACK=0
LAB="${WEBRET_VALKEY_INSTALL_LAB:-0}"

die() { printf 'web-retrieval-valkey: %s\n' "$*" >&2; exit 2; }

usage() {
  sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'
  printf '%s\n' \
    'Usage: tests/fixtures/valkey/install.sh [--apply | --rollback] [--prefix ABSOLUTE_DIR]' \
    '  no flag     validate state and print the pinned install plan; no writes/downloads' \
    '  --apply     download, verify, install atomically, and retain one rollback version' \
    '  --rollback  atomically select the previous verified version and restart only Valkey'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --rollback) ROLLBACK=1 ;;
    --prefix) shift; [ "$#" -gt 0 ] || die '--prefix needs a path'; PREFIX="$1"; PREFIX_GIVEN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

[ "$APPLY" = 0 ] || [ "$ROLLBACK" = 0 ] || die '--apply and --rollback are mutually exclusive'
case "$PREFIX" in /*) ;; *) die '--prefix must be absolute' ;; esac
[ "$PREFIX" != / ] || die 'refusing broad prefix /'
[ "$PREFIX" != "$HOME" ] || die 'refusing home directory as prefix'
[ ! -L "$PREFIX" ] || die 'prefix must not be a symlink'

# Artifact overrides require an explicit isolated-test marker and prefix.
if [ -n "${WEBRET_VALKEY_MANIFEST:-}" ] || [ -n "${WEBRET_VALKEY_ARCHIVE_FILE:-}" ] \
   || [ -n "${WEBRET_VALKEY_VERIFIER:-}" ]; then
  [ "$LAB" = 1 ] || die 'installer overrides require WEBRET_VALKEY_INSTALL_LAB=1'
  if [ "$PREFIX_GIVEN" != 1 ] || [ "$PREFIX" = "$DEFAULT_PREFIX" ]; then
    die 'installer overrides require an explicit isolated --prefix'
  fi
  MANIFEST="${WEBRET_VALKEY_MANIFEST:-$MANIFEST}"
  VERIFIER="${WEBRET_VALKEY_VERIFIER:-$VERIFIER}"
fi
for path in "$MANIFEST" "$CONFIG" "$ARCHIVE_TOOL" "$VERIFIER"; do
  case "$path" in /*) ;; *) die "support path is not absolute: $path" ;; esac
  if [ ! -f "$path" ] || [ -L "$path" ]; then
    die "missing real support file: $path"
  fi
done

# The manifest path is deliberately replaceable only in the isolated lab gate above.
# shellcheck disable=SC1090
source "$MANIFEST"
: "${VALKEY_VERSION:?}" "${VALKEY_DISTRO:?}" "${VALKEY_ARCH:?}" \
  "${VALKEY_ARCHIVE:?}" "${VALKEY_URL:?}" "${VALKEY_SHA256:?}"
[[ "$VALKEY_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'invalid pinned version'
[[ "$VALKEY_DISTRO" =~ ^[a-z0-9]+$ ]] || die 'invalid pinned distro'
[[ "$VALKEY_ARCH" =~ ^[A-Za-z0-9_]+$ ]] || die 'invalid pinned architecture'
[[ "$VALKEY_SHA256" =~ ^[0-9a-f]{64}$ ]] || die 'invalid pinned SHA-256'
[ "$VALKEY_ARCHIVE" = "valkey-${VALKEY_VERSION}-${VALKEY_DISTRO}-${VALKEY_ARCH}.tar.gz" ] \
  || die 'archive name does not match pinned version tuple'
case "$VALKEY_URL" in https://download.valkey.io/releases/"$VALKEY_ARCHIVE") ;; *) die 'URL is not the exact official pinned archive' ;; esac

ACTIVE_LINK="$PREFIX/valkey"
TARGET_DIR="$PREFIX/valkey-$VALKEY_VERSION"

active_target() {
  if [ ! -e "$ACTIVE_LINK" ] && [ ! -L "$ACTIVE_LINK" ]; then return 1; fi
  [ -L "$ACTIVE_LINK" ] || die 'active valkey path exists but is not a symlink'
  local raw base resolved
  raw="$(readlink "$ACTIVE_LINK")" || die 'cannot read active symlink'
  case "$raw" in /*) resolved="$raw" ;; *) resolved="$PREFIX/$raw" ;; esac
  base="$(basename "$resolved")"
  [[ "$base" =~ ^valkey-[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'active symlink target has an invalid name'
  [ "$(dirname "$resolved")" = "$PREFIX" ] || die 'active symlink escapes prefix'
  if [ ! -d "$resolved" ] || [ -L "$resolved" ]; then
    die 'active symlink is broken or targets a non-directory'
  fi
  printf '%s\n' "$resolved"
}

installed_dirs() {
  [ -d "$PREFIX" ] || return 0
  local candidate name
  for candidate in "$PREFIX"/valkey-*; do
    [ -e "$candidate" ] || continue
    name="$(basename "$candidate")"
    [[ "$name" =~ ^valkey-[0-9]+\.[0-9]+\.[0-9]+$ ]] \
      || die "unexpected valkey path under prefix: $candidate"
    if [ ! -d "$candidate" ] || [ -L "$candidate" ]; then
      die "installed version path is not a real directory: $candidate"
    fi
    printf '%s\n' "$candidate"
  done
}

verify_tree() {
  local dir="$1" version="${1##*/valkey-}" expected="${2:-}"
  local args=("$ARCHIVE_TOOL" verify-installed --root "$dir" --version "$version")
  [ -z "$expected" ] || args+=(--archive-sha256 "$expected")
  python3 "${args[@]}" >/dev/null
}

verify_candidate() {
  local dir="$1" version="${1##*/valkey-}"
  verify_tree "$dir" "${2:-}"
  python3 "$VERIFIER" --root "$dir" --version "$version" --config "$CONFIG"
}

cleanup_versions() {
  local keep_active="$1" keep_previous="${2:-}" candidate
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    [ "$candidate" = "$keep_active" ] && continue
    [ -n "$keep_previous" ] && [ "$candidate" = "$keep_previous" ] && continue
    verify_tree "$candidate"
    printf 'web-retrieval-valkey: removing superseded verified version %s\n' "$(basename "$candidate")"
    rm -rf -- "$candidate"
  done < <(installed_dirs)
}

current=""
if current="$(active_target)"; then
  verify_tree "$current"
fi

if [ "$ROLLBACK" = 1 ]; then
  [ -n "$current" ] || die 'cannot roll back without an active installed version'
  mapfile -t candidates < <(installed_dirs)
  previous=""
  for candidate in "${candidates[@]}"; do
    [ "$candidate" = "$current" ] && continue
    [ -z "$previous" ] || die 'rollback is ambiguous: more than one previous version is installed'
    previous="$candidate"
  done
  [ -n "$previous" ] || die 'no previous installed version is available for rollback'
  verify_candidate "$previous"
  tmp_link="$PREFIX/.valkey-link.$$"
  if [ -e "$tmp_link" ] || [ -L "$tmp_link" ]; then
    die "temporary link already exists: $tmp_link"
  fi
  ln -s "$(basename "$previous")" "$tmp_link"
  mv -Tf -- "$tmp_link" "$ACTIVE_LINK"
  printf 'web-retrieval-valkey: rolled back active link to %s\n' "$(basename "$previous")"
  printf 'web-retrieval-valkey: isolated fixture — no service restart performed\n'
  exit 0
fi

if [ "$APPLY" != 1 ]; then
  printf 'web-retrieval-valkey: DRY RUN — pinned Valkey %s\n' "$VALKEY_VERSION"
  printf 'web-retrieval-valkey: source %s\n' "$VALKEY_URL"
  printf 'web-retrieval-valkey: SHA-256 %s\n' "$VALKEY_SHA256"
  printf 'web-retrieval-valkey: target %s\n' "$TARGET_DIR"
  if [ -n "$current" ]; then
    printf 'web-retrieval-valkey: active %s (integrity verified)\n' "$(basename "$current")"
  else
    printf 'web-retrieval-valkey: no active install\n'
  fi
  printf 'web-retrieval-valkey: nothing downloaded, written, linked, started, or restarted\n'
  exit 0
fi

mkdir -p "$PREFIX"
if [ ! -d "$PREFIX" ] || [ -L "$PREFIX" ]; then
  die 'prefix is not a real directory'
fi
exec 9>"$PREFIX/.install.lock"
flock -x 9

# Re-resolve state under the lock.
current=""
if current="$(active_target)"; then verify_tree "$current"; fi
if [ "$current" = "$TARGET_DIR" ]; then
  verify_candidate "$TARGET_DIR" "$VALKEY_SHA256"
  mapfile -t others < <(installed_dirs)
  previous=""
  for candidate in "${others[@]}"; do
    [ "$candidate" = "$current" ] && continue
    if [ -z "$previous" ] || [[ "$(basename "$candidate")" > "$(basename "$previous")" ]]; then
      previous="$candidate"
    fi
  done
  cleanup_versions "$current" "$previous"
  printf 'web-retrieval-valkey: already installed and active; no-op\n'
  exit 0
fi
if [ -e "$TARGET_DIR" ] || [ -L "$TARGET_DIR" ]; then
  die "target exists before install and is not the active verified version: $TARGET_DIR"
fi

stage="$(mktemp -d "$PREFIX/.valkey-install.XXXXXX")"
cleanup_stage() { [ -n "${stage:-}" ] && [ -d "$stage" ] && rm -rf -- "$stage"; }
trap cleanup_stage EXIT
archive="$stage/$VALKEY_ARCHIVE"
if [ -n "${WEBRET_VALKEY_ARCHIVE_FILE:-}" ]; then
  source_archive="$WEBRET_VALKEY_ARCHIVE_FILE"
  case "$source_archive" in /*) ;; *) die 'lab archive path must be absolute' ;; esac
  if [ ! -f "$source_archive" ] || [ -L "$source_archive" ]; then
    die 'lab archive is not a real file'
  fi
  cp -- "$source_archive" "$archive"
else
  curl --fail --location --proto '=https' --tlsv1.2 \
    --connect-timeout 15 --max-time 300 --output "$archive" "$VALKEY_URL"
fi
actual_sha="$(sha256sum "$archive" | awk '{print $1}')"
[ "$actual_sha" = "$VALKEY_SHA256" ] \
  || die "archive digest mismatch: expected $VALKEY_SHA256, got $actual_sha"
mkdir "$stage/extract"
root="$(python3 "$ARCHIVE_TOOL" extract --archive "$archive" --dest "$stage/extract" \
  --version "$VALKEY_VERSION" --distro "$VALKEY_DISTRO" --arch "$VALKEY_ARCH")"
candidate="$stage/valkey-$VALKEY_VERSION"
mv -- "$root" "$candidate"
python3 "$ARCHIVE_TOOL" write-marker --root "$candidate" --version "$VALKEY_VERSION" \
  --archive-sha256 "$VALKEY_SHA256" >/dev/null
verify_candidate "$candidate" "$VALKEY_SHA256"
mv -- "$candidate" "$TARGET_DIR"

tmp_link="$PREFIX/.valkey-link.$$"
if [ -e "$tmp_link" ] || [ -L "$tmp_link" ]; then
  die "temporary link already exists: $tmp_link"
fi
ln -s "$(basename "$TARGET_DIR")" "$tmp_link"
mv -Tf -- "$tmp_link" "$ACTIVE_LINK"
previous="$current"
cleanup_versions "$TARGET_DIR" "$previous"
printf 'web-retrieval-valkey: installed and selected Valkey %s\n' "$VALKEY_VERSION"
printf 'web-retrieval-valkey: no service was started or restarted\n'
