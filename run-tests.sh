#!/usr/bin/env bash
# Public release gate: source suites, package metadata, and clean-wheel acceptance.
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd -P)" || exit 2
cd "$PROJECT_ROOT" || exit 2

PYTHON_BIN="${WEBRET_TEST_PY:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ACTION REQUIRED: Python interpreter not found: $PYTHON_BIN" >&2
    exit 2
fi
if ! "$PYTHON_BIN" -c 'import anyio, build, hatchling, mcp, twine; from readme_renderer.markdown import variants; assert "GFM" in variants' >/dev/null 2>&1; then
    echo "ACTION REQUIRED: install release dependencies with:" >&2
    echo "  $PYTHON_BIN -m pip install -e '.[all,dev]'" >&2
    exit 2
fi

TEST_PARENT="${TMPDIR:-/tmp}"
case "$TEST_PARENT" in
    /*) ;;
    *) echo "ACTION REQUIRED: TMPDIR must be absolute" >&2; exit 2 ;;
esac
TEST_ROOT="$(mktemp -d "$TEST_PARENT/web-retrieval-tests.XXXXXX")" || exit 2
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/web-retrieval-tests.*) rm -rf -- "$TEST_ROOT" ;;
        *) echo "refusing unsafe test cleanup: $TEST_ROOT" >&2 ;;
    esac
}
trap cleanup_test_root EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

VALKEY_PREFIX="$TEST_ROOT/valkey-fixture"
if ! tests/fixtures/valkey/install.sh --apply --prefix "$VALKEY_PREFIX"; then
    echo "ACTION REQUIRED: could not provision the pinned Valkey test fixture" >&2
    exit 2
fi
export WEBRET_TEST_VALKEY_ROOT="$VALKEY_PREFIX/valkey"
export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

rc=0
for test_file in test_*.py; do
    case "$test_file" in
        test_ssrf_redirect_live.py) continue ;;
    esac
    echo "=== $test_file ==="
    "$PYTHON_BIN" "$test_file" || rc=1
done

ARTIFACTS="$TEST_ROOT/dist"
mkdir "$ARTIFACTS"
echo "=== package build ==="
if ! "$PYTHON_BIN" -m build --no-isolation --outdir "$ARTIFACTS"; then
    rc=1
elif ! "$PYTHON_BIN" -m twine check "$ARTIFACTS"/*; then
    rc=1
else
    set -- "$ARTIFACTS"/web_retrieval_mcp-*.whl "$ARTIFACTS"/web_retrieval_mcp-*.tar.gz
    if [ "$#" -ne 2 ] || [ ! -f "$1" ] || [ ! -f "$2" ]; then
        echo "FAIL: expected exactly one wheel and one sdist in $ARTIFACTS" >&2
        rc=1
    else
        echo "=== public metadata acceptance ==="
        "$PYTHON_BIN" tests/acceptance_public_metadata.py "$1" "$2" || rc=1
        echo "=== installed-wheel acceptance ==="
        "$PYTHON_BIN" tests/acceptance_public_install.py "$1" || rc=1
    fi
fi

if [ "$rc" -eq 0 ]; then
    echo "=== ALL PUBLIC RELEASE GATES GREEN ==="
else
    echo "=== PUBLIC RELEASE GATE FAILURE(S) ===" >&2
fi
exit "$rc"
