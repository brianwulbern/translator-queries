"""A key's introducing commit and the Jira ticket that carries its context.

Shared by tq, which resolves one key at a time when a new question arrives. A
key asked about for the first time otherwise has no links until someone works
them out by hand, so the reply would go out without its context ticket.

A key normally lands on a sub-task, which holds no description of its own; the
context lives on the STORY above it, so a task resolves to its parent while a
Story or Epic is used as is.

Tickets are fetched unauthenticated on purpose. That is exactly the access an
outside translator has, so a ticket this cannot read is one they could not open
either, and its link is dropped rather than sent as a dead end.
"""
import json, os, re, subprocess, time, urllib.request

import config

GH = config.get("commitUrlBase")
JIRA = config.get("jiraHost")
LANG_REL = config.get("languageFile")
HISTORY = os.path.join(config.DATA, "lang-history.tsv")


def jira(key):
    if os.environ.get("TQ_DEMO"):
        import demo_backend
        return demo_backend.jira_issue(key)
    url = "%s/rest/api/3/issue/%s?fields=summary,issuetype,parent" % (JIRA, key)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.load(r)
        return None if "errorMessages" in d else d
    except Exception:
        return None


def git(*a):
    return subprocess.run(["git"] + list(a), cwd=config.source_repo(),
                          capture_output=True, text=True).stdout.strip()


def introducing_commit(key):
    """'sha|date|author|subject' for the commit that added the key, or ''.

    The history index answers in milliseconds but only knows what existed when
    it was built. A newer key is looked up with a pickaxe over the month before
    the index, which takes seconds rather than the 56 a full scan costs. The
    month is not slack: a commit keeps its commit date when it merges, so a key
    committed days before the index was built can still reach master after it.
    """
    if os.path.exists(HISTORY):
        hit = subprocess.run(["grep", "-a", "^%s\t+\t" % re.escape(key), HISTORY],
                             capture_output=True, text=True,
                             env=dict(os.environ, LC_ALL="C")).stdout.splitlines()
        if hit:
            return hit[-1].split("\t")[4]           # git log is newest first
        since = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(HISTORY) - 30 * 86400))
    else:
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 120 * 86400))
    out = git("log", "--reverse", "--since=" + since, "--date=short",
              "--format=%h|%ad|%an|%s", "-G", "^%s=" % re.escape(key), "--", LANG_REL)
    return out.splitlines()[0] if out else ""


_MASTER = []


def canonical_master():
    """'<remote>/master' for the remote that points at the canonical repository.

    Found by URL, not by name. Checkouts call that remote upstream, origin or
    anything else, and a hard-coded name made every commit link vanish without
    an error on any checkout that named it differently.
    """
    if not _MASTER:
        found = ""
        pattern = config.get("canonicalRemote")
        for line in git("remote", "-v").splitlines():
            parts = line.split()
            if len(parts) >= 2 and pattern and re.search(pattern, parts[1]):
                found = parts[0] + "/master"
                break
        _MASTER.append(found)
    return _MASTER[0]


def resolve(intro, cache=None):
    """Links for one 'sha|date|author|subject' line. cache spares repeat Jira reads."""
    cache = {} if cache is None else cache
    r = {"commitUrl": "", "ctxTicket": "", "ctxUrl": "", "ctxSummary": "",
         "ctxHow": "", "ctxInternal": ""}
    m = re.match(r"([0-9a-f]+)\|([\d-]+)\|(.+?)\|(.+)", intro or "")
    if not m:
        return r
    short, date, _author, subject = m.groups()
    subject = re.sub(r"\s*\[.*$", "", subject)
    full = git("rev-parse", short)
    master = canonical_master() or ("HEAD" if not config.get("canonicalRemote") else "")
    on_master = bool(master) and subprocess.run(
        ["git", "merge-base", "--is-ancestor", short, master],
        cwd=config.source_repo(), capture_output=True).returncode == 0
    r["commitSha"], r["commitDate"] = full[:12], date
    r["commitUrl"] = (GH + full) if (full and on_master) else ""
    found = re.findall(r"\b[A-Z][A-Z0-9]+-\d+", subject)
    if not found:
        return r
    tk = found[0]
    if tk not in cache:
        d = jira(tk)
        if d is None:
            # Keep the id even though the link is dropped. The commit subject
            # names it anyway, so the reply can say it is internal rather than
            # claim nothing was found.
            cache[tk] = ("", "", "restricted")
        else:
            f = d["fields"]; it = (f.get("issuetype") or {}).get("name", ""); p = f.get("parent")
            if it in ("Story", "Epic") or not p:
                cache[tk] = (tk, f.get("summary") or "", it)
            else:
                pk = p["key"]
                cache[tk] = ((pk, (p.get("fields") or {}).get("summary") or "", it + " -> story")
                             if jira(pk) else (tk, f.get("summary") or "", it + " (parent restricted)"))
    r["ctxTicket"], r["ctxSummary"], r["ctxHow"] = cache[tk]
    r["ctxUrl"] = "%s/browse/%s" % (JIRA, r["ctxTicket"]) if r["ctxTicket"] else ""
    r["ctxInternal"] = tk if r["ctxHow"] == "restricted" else ""
    return r
