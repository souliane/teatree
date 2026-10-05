#!/bin/zsh
# One host-scoped sample. launchd runs this every 15 seconds even with no agent
# session or statusline render, keeping the worker's <60s feed fresh.
set -e
# A comma-decimal locale would print 2,976 into the JSON.
export LC_ALL=C
[[ "$(uname -s)" == Darwin ]] || exit 0

sysctl_data="$(sysctl -n hw.memsize hw.ncpu vm.loadavg vm.swapusage)" || exit 1
sysctl_lines=("${(@f)sysctl_data}")
[[ ${#sysctl_lines} == 4 ]] || exit 1
cores="${sysctl_lines[2]}"
load_line="${sysctl_lines[3]}"
swap_line="${sysctl_lines[4]}"
[[ "$cores" =~ '^[0-9]+$' && "$cores" -gt 0 ]] || exit 1
[[ "$load_line" =~ '\{[[:space:]]*([0-9]+(\.[0-9]+)?)' ]] || exit 1
load1="${match[1]}"
swap_total="${swap_line#*total = }"
swap_total="${swap_total%%M*}"
swap_used="${swap_line#*used = }"
swap_used="${swap_used%%M*}"
[[ "$swap_total" =~ '^[0-9]+(\.[0-9]+)?$' && "$swap_used" =~ '^[0-9]+(\.[0-9]+)?$' ]] || exit 1

vm_data="$(vm_stat)" || exit 1
page_size=""
free_pages=""
inactive_pages=""
swapins=""
swapouts=""
for line in "${(@f)vm_data}"; do
    case "$line" in
        *'page size of '*)
            page_size="${line#*page size of }"
            page_size="${page_size%% *}"
            ;;
        'Pages free:'*)
            free_pages="${line##* }"
            free_pages="${free_pages%.}"
            ;;
        'Pages inactive:'*)
            inactive_pages="${line##* }"
            inactive_pages="${inactive_pages%.}"
            ;;
        'Swapins:'*)
            swapins="${line##* }"
            swapins="${swapins%.}"
            ;;
        'Swapouts:'*)
            swapouts="${line##* }"
            swapouts="${swapouts%.}"
            ;;
    esac
done
[[ "$page_size" =~ '^[0-9]+$' && "$free_pages" =~ '^[0-9]+$' && "$inactive_pages" =~ '^[0-9]+$' ]] || exit 1

# Occupancy says nothing current on macOS: swap stays allocated long after the pressure
# that filled it. Paging activity and the kernel's own pressure verdict do.
pressure_level="$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null)" || pressure_level=""
[[ "$pressure_level" =~ '^[0-9]+$' ]] || pressure_level=""
swap_pages=""
[[ "$swapins" =~ '^[0-9]+$' && "$swapouts" =~ '^[0-9]+$' ]] && swap_pages=$(( swapins + swapouts ))

host_root="${TEATREE_HOST_HOME:-$HOME}"
data_dir="${T3_LOOP_REGISTRY_DIR:-${XDG_DATA_HOME:-$host_root/.local/share}/teatree}"
umask 077
mkdir -p -- "$data_dir"
target="$data_dir/host-pressure.json"
temporary="$target.$$"
trap 'rm -f -- "$temporary"' EXIT
epoch="$(date +%s)"
ram_mib=$(( (free_pages + inactive_pages) * page_size / 1048576 ))
activity=""
if [[ -n "$swap_pages" ]]; then
    activity=",\"swap_pages\":$swap_pages"
    previous=""
    [[ -r "$target" ]] && IFS= read -r previous <"$target"
    # A rate needs a comparable previous sample: one this publisher wrote, recent, and
    # from before any counter reset (a reboot zeroes vm_stat).
    if [[ "$previous" =~ '"epoch":([0-9]+)' ]]; then
        previous_epoch="${match[1]}"
        if [[ "$previous" =~ '"swap_pages":([0-9]+)' ]]; then
            previous_pages="${match[1]}"
            elapsed=$(( epoch - previous_epoch ))
            if (( elapsed > 0 && elapsed <= 60 && swap_pages >= previous_pages )); then
                activity+=",\"swap_mib_per_s\":$(printf '%.3f' $(( (swap_pages - previous_pages) * page_size / 1048576.0 / elapsed )))"
            fi
        fi
    fi
fi
[[ -n "$pressure_level" ]] && activity+=",\"vm_pressure_level\":$pressure_level"
printf '{"epoch":%s,"cores":%s,"load1":%s,"ram_available_mib":%s,"swap_used_mib":%s,"swap_total_mib":%s%s}\n' \
    "$epoch" "$cores" "$load1" "$ram_mib" "${swap_used%%.*}" "${swap_total%%.*}" "$activity" >"$temporary"
mv -f -- "$temporary" "$target"
mirror_dir="${T3_HOST_PRESSURE_MIRROR_DIR:-}"
if [[ -n "$mirror_dir" && "$mirror_dir" != "$data_dir" ]]; then
    mkdir -p -- "$mirror_dir"
    mirror_temporary="$mirror_dir/host-pressure.json.$$"
    trap 'rm -f -- "$mirror_temporary"' EXIT
    cp -- "$target" "$mirror_temporary"
    mv -f -- "$mirror_temporary" "$mirror_dir/host-pressure.json"
fi
