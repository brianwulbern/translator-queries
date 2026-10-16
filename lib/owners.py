#!/usr/bin/env python3
"""Resolve the owning GitHub team for a repo path from .github/CODEOWNERS.

CODEOWNERS semantics: the LAST matching pattern wins, not the most specific,
so the file is scanned in order and the final match is kept.
"""
import os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

CO = os.path.join(config.source_repo(), ".github", "CODEOWNERS")

def rules():
    out = []
    if not os.path.exists(CO):
        return out
    for line in open(CO, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#"): continue
        parts = line.split()
        pat, owners = parts[0], [p for p in parts[1:] if p.startswith("@")]
        out.append((pat, owners))
    return out

RULES = rules()

def owner_of(path):
    hit = None
    for pat, owners in RULES:
        p = pat.lstrip("/")
        if path.startswith(p) or re.match(p.replace("*", ".*") + "$", path):
            hit = (pat, owners)          # last match wins
    return hit

if __name__ == "__main__":
    for path in sys.argv[1:]:
        h = owner_of(path)
        print("%-62s -> %s" % (path[:62], (", ".join(h[1]) or "(no owner listed)") if h else "(unmatched)"))
