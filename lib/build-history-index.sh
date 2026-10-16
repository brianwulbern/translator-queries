#!/bin/bash
# Maps every key to the commits that added or removed it, with values, so a real
# wording change is distinguishable from a line moved by a re-sort. One pass over
# the language file's history replaces a per-key `git log -S`, which is slow on a
# large repository and wrong besides: -S matches a substring, so -S"order=" also
# counts sort-order=.
. "$(dirname "${BASH_SOURCE[0]}")/config.sh"
REPO="${TQ_SOURCE_REPO}"
REL="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import config; print(config.get("languageFile"))' "${TQ_LIB}")"
OUT="$1"
cd "${REPO}" || exit 1
export LC_ALL=C
git log --format="@@C@@%h|%ad|%an|%s" --date=short -U0 --no-renames -p -- "${REL}" 2>/dev/null \
  | awk '
      /^@@C@@/ { commit = substr($0, 6); next }
      /^[+-][a-zA-Z0-9]/ {
        sign = substr($0, 1, 1); rest = substr($0, 2)
        eq = index(rest, "="); if (eq < 2) next
        key = substr(rest, 1, eq - 1); if (key ~ /[[:space:]]/) next
        print key "\t" sign "\tsrc\t" substr(rest, eq + 1) "\t" commit
      }' > "${OUT}"
echo "history index: $(wc -l < "${OUT}") rows, $(cut -f1 "${OUT}" | sort -u | wc -l) keys"
