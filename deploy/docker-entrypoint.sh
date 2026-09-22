#!/bin/sh
# Container entry point, run by tini (PID 1) - design doc 09 §1.
#
#   no arguments, or options only  ->  curator-daemon [options]   (the service, the default)
#   anything else                  ->  run as given: curation ..., curator-rotate-master-key, sh ...
#
#   docker run IMAGE                          the Daemon
#   docker run IMAGE --check-config           the Daemon, validate the configuration and exit
#   docker run IMAGE curation --help          the CLI
#
# exec replaces this shell, so tini's child is the program itself and SIGTERM reaches it
# directly (the Daemon's graceful stop depends on it, 09 §2.3).
if [ "$#" -eq 0 ]; then
    set -- curator-daemon
else
    case "$1" in
        -*) set -- curator-daemon "$@" ;;
    esac
fi
exec "$@"
