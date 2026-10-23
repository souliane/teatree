git() {
    command git -c core.fsmonitor= -c core.hooksPath=/dev/null "$@"
}
