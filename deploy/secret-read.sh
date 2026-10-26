#!/bin/sh

secret_selector() {
    if [ -n "${T3_SECRET_STORE:-}" ]; then
        printf '%s\n' "$T3_SECRET_STORE"
        return
    fi
    file="${TEATREE_SECRET_ENV_FILE:-$(dirname "$0")/teatree.env}"
    selected=pass
    if [ -r "$file" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            line="${line%"$(printf '\r')"}"
            case "$line" in
                export\ T3_SECRET_STORE=*) line="${line#export }" ;;
            esac
            case "$line" in
                T3_SECRET_STORE=*)
                    value="${line#*=}"
                    case "$value" in
                        \"*\") value="${value#\"}"; value="${value%\"}" ;;
                        "'"*"'") value="${value#\'}"; value="${value%\'}" ;;
                    esac
                    selected="${value:-pass}"
                    ;;
            esac
        done < "$file"
    fi
    printf '%s\n' "$selected"
}

operation="${1:-}"
name="${2:-}"
if [ "$operation" = selector ]; then
    secret_selector
    exit 0
fi
if [ -z "$name" ]; then
    echo "secret-read: expected an entry name" >&2
    exit 2
fi
selector="$(secret_selector)"
case "$selector" in
    pass)
        command -v pass >/dev/null 2>&1 || exit 1
        case "$operation" in
            get|host-get)
                value="$(pass show "$name" 2>/dev/null)" || {
                    rc=$?
                    [ "$rc" -eq 1 ] && exit 1
                    exit 2
                }
                first="${value%%'
'*}"
                printf '%s\n' "$first"
                ;;
            has-prefix) pass ls "$name" >/dev/null 2>&1 ;;
            probe-prefix)
                root="${PASSWORD_STORE_DIR:-$HOME/.password-store}"
                entry="$(find "$root/$name" -type f -name '*.gpg' -print -quit 2>/dev/null)"
                [ -n "$entry" ] || exit 1
                entry="${entry#"$root/"}"
                pass show "${entry%.gpg}" >/dev/null 2>&1
                ;;
            *) echo "secret-read: unknown operation $operation" >&2; exit 2 ;;
        esac
        ;;
    sealed)
        [ "$operation" = host-get ] && exit 1
        if [ -n "${T3_SECRET_PYTHON:-}" ]; then
            interpreter="$T3_SECRET_PYTHON"
        elif [ -x /opt/teatree/venv/bin/python ]; then
            interpreter=/opt/teatree/venv/bin/python
        else
            binary="$(command -v t3)" || exit 2
            interpreter="$(dirname "$(readlink -f "$binary")")/python"
        fi
        errors="$(mktemp "${TMPDIR:-/tmp}/t3-secret-read.XXXXXX")" || exit 2
        trap 'rm -f "$errors"' EXIT HUP INT TERM
        python_code='from teatree.config.cold_secret_reader import main; import sys; result = main(); raise SystemExit(42 if result == 1 else result)'
        value="$("$interpreter" -c "$python_code" "$operation" "$name" 2>"$errors")" || {
            rc=$?
            if [ "$rc" -eq 42 ]; then
                exit 1
            fi
            cat "$errors" >&2
            exit 2
        }
        [ -z "$value" ] || printf '%s\n' "$value"
        ;;
    *) echo "secret-read: invalid T3_SECRET_STORE=$selector" >&2; exit 2 ;;
esac
