#!/bin/sh

secret_selector() {
    if [ -n "${T3_SECRET_STORE:-}" ]; then
        printf '%s\n' "$T3_SECRET_STORE"
        return
    fi
    file="${TEATREE_SECRET_ENV_FILE:-$(dirname "$0")/teatree.env}"
    if [ -r "$file" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                T3_SECRET_STORE=*)
                    value="${line#*=}"
                    case "$value" in
                        \"*\") value="${value#\"}"; value="${value%\"}" ;;
                        "'"*"'") value="${value#\'}"; value="${value%\'}" ;;
                    esac
                    printf '%s\n' "$value"
                    return
                    ;;
            esac
        done < "$file"
    fi
    printf 'pass\n'
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
        exec "$interpreter" -m teatree.config.cold_secret_reader "$operation" "$name"
        ;;
    *) echo "secret-read: invalid T3_SECRET_STORE=$selector" >&2; exit 2 ;;
esac
