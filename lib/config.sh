# Shared configuration for the shell helpers. Sourced, not executed.
# Settings are read through config.py, so the environment overrides and the
# config file layering work the same here as in the Python code.

TQ_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TQ_ROOT="$(dirname "${TQ_LIB}")"

if [ -z "${TQ_SOURCE_REPO}" ]; then
	TQ_SOURCE_REPO="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import config; print(config.source_repo())' "${TQ_LIB}" 2>/dev/null)"
fi

if [ ! -d "${TQ_SOURCE_REPO}" ]; then
	echo "tq: sourceRepo is not set or not a directory. Copy config.example.json to config.json and fill it in." >&2
	exit 1
fi
