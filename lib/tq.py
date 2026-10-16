#!/usr/bin/env python3
"""tq - answer translators' questions on a Crowdin project, from the source code.

Subcommands:
  (none)    The control room: pull the queue, check filed Tasks in Jira, look up
            each key's commit and ticket, draft every new question, then review.
  pull      Fetch the open issue queue and group it by source string.
  review    Step through each reply and choose send, file, redraft or skip.
  preview   Print the exact text that would be posted. Changes nothing.
  facts     Print the facts gathered from the source for one or more keys.
  draft     Draft replies for keys that have none.
  ticket    Preview the follow-up Task for a key. Files nothing.
  followups List the Tasks tq has filed and where each stands.
  usage     What drafting has cost so far.
  doctor    Check the whole setup: token scopes, repos, team, Jira, tools.
  post      Batch post with a [y/N] confirmation. Always requires --only.

The parts that must be exact (who asked, what the code substitutes, whether the
string is spoken or drawn, what has been filed) are gathered deterministically.
A model only writes the prose, from those facts.
"""
import argparse, json, os, re, shutil, subprocess, sys, textwrap, threading, time, urllib.error, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

__version__ = "0.21.2"

# No call should hang the review loop indefinitely.
HTTP_TIMEOUT = 60

# A draft starting with this posts the reply but leaves the issue open.
HOLD_RE = re.compile(r"^<!--\s*tq:\s*hold\s*-->\s*", re.I)

PROJECT = config.project_id()
API = config.get("crowdinApi")
BASE = "%s/projects/%d" % (API, PROJECT)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE = os.path.join(config.DATA, "state.json")
LINKS = os.path.join(config.DATA, "links.json")
ANSWERS = os.path.join(config.DATA, "answers")
# Never mention ourselves, nor the localization manager the translators address.
SKIP_MENTION = config.skip_mention() | config.team()     # never ping our own team


def build_id():
    """Describe exactly which tq is running.

    The version alone is not enough here. tq runs straight from a git checkout
    rather than an installed package, so between two tagged versions the thing
    on disk can be any commit, or edited. The commit and a dirty marker say
    which one it actually is, and both degrade quietly when git is unavailable
    or this is an unpacked copy rather than a clone.
    """
    try:
        import subprocess
        def g(*a):
            return subprocess.run(["git", "-C", ROOT] + list(a), capture_output=True,
                                  text=True, timeout=5)
        r = g("rev-parse", "--short", "HEAD")
        if r.returncode:
            return __version__
        sha = r.stdout.strip()
        dirty = " modified" if g("status", "--porcelain", "--", ".",
                                 ":(exclude)answers").stdout.strip() else ""
        return "%s (%s%s)" % (__version__, sha, dirty)
    except Exception:
        return __version__


def wrapped(label, text, indent):
    """Print a label and its text without ever cutting it short.

    These lines used to truncate. On a string carrying several placeholders that
    is dangerous rather than untidy: a {1} past the cut is invisible, so a reply
    that never accounts for it still looks complete at the point of approval.
    """
    body = " ".join((text or "").split())
    width = max(40, 96 - len(indent) - len(label))
    lines = textwrap.wrap(body, width) or [""]
    print("%s%s%s" % (indent, label, lines[0]))
    for extra in lines[1:]:
        print("%s%s%s" % (indent, " " * len(label), extra))


def placeholder_gap(g):
    """Placeholders in the English that the drafted reply never mentions.

    Translators ask what the placeholders become more often than anything else,
    so an answer that covers {0} and silently ignores {1} looks complete and is
    not. Cheap to check, and it fails in the direction of asking a human.
    """
    if not g.get("body"):
        return []
    in_en = set(re.findall(r"\{\d\}", g.get("english") or ""))
    return sorted(in_en - set(re.findall(r"\{\d\}", g["body"])))


def strip_handles(text):
    """Drop the at-mentions of our own side from a quoted question.

    Translators address the localization manager by handle when they ask, and
    echoing that back adds noise to the review screen without adding meaning.
    The handles come from config so no colleague's name is hard coded here.
    """
    for name in SKIP_MENTION:
        text = text.replace("@" + name, "")
    return text.strip()


SETUP = """
How to get a Crowdin token for tq
---------------------------------
1. Sign in to Crowdin and open your account settings, then the API section.
   Personal Access Tokens live there, not under the project's own settings.

2. Create a token ("New Token"). Under Scopes, open Projects and tick exactly two:

     Projects (List, Get, Create, Edit)   Read only
     Source files & strings               Read and Write

   Leave everything else unticked. Write on "Source files & strings" is not
   optional, and it is the easy thing to get wrong: posting a comment and
   resolving an issue both count as writing to the SOURCE, so a Read only
   token reads the queue fine and then fails with 403 the moment you send.
   There is no separate scope for comments, and none is needed for images.

3. "Granular access" at the bottom is optional. Leave it off, or enable it and
   select only this project.

4. Store it somewhere your shell can reach, and never commit it:

     export CROWDIN_PERSONAL_TOKEN=...

   Either export it in your shell profile, or put that line in a credentials
   file and point tq at the file:

     export TQ_CREDENTIALS_FILE=~/.config/somewhere/credentials

   tq sources that file only when the variable is not already set, and never
   prints the value.

5. Check it with:

     tq doctor
"""


def token():
    t = os.environ.get("CROWDIN_PERSONAL_TOKEN")
    if not t:
        # An empty value is its own trap: the variable exists, so every "is it
        # set?" check passes while every request fails.
        where = os.environ.get("TQ_CREDENTIALS_FILE")
        why = "is empty" if t == "" else "is not set"
        extra = ("\n%s was sourced, so check the token is actually assigned there."
                 % where) if where and t == "" else ""
        sys.exit("tq: CROWDIN_PERSONAL_TOKEN %s.%s\n%s" % (why, extra, SETUP))
    return t


def check_token():
    """Report whether the token exists, authenticates, and may write.

    Presence is not enough on either count. A token can be set and empty, and a
    token can read the whole queue and still be refused the moment it posts,
    because comments are writes to the SOURCE rather than to a translation.
    """
    t = os.environ.get("CROWDIN_PERSONAL_TOKEN") or ("demo" if os.environ.get("TQ_DEMO") else None)
    if not t:
        print("  token          NOT SET" if t is None else "  token          SET BUT EMPTY")
        return False
    print("  token          present (%d characters, not shown)" % len(t))

    st, res = call("GET", BASE)
    if st == 401:
        print("  authentication FAILED (401). The token is wrong, revoked or expired.")
        return False
    if st == 403:
        print("  authentication ok, but this token cannot see project %d." % PROJECT)
        return False
    if st != 200:
        print("  authentication UNCLEAR (HTTP %s): %s" % (st, json.dumps(res)[:120]))
        return False
    print("  project %-6d %s" % (PROJECT, (res.get("data") or {}).get("name", "readable")))

    # Probe the write scope without writing anything: aim a comment at a string
    # id that cannot exist. 403 means the scope is missing; 400 or 404 means the
    # scope is fine and only the data was rejected.
    st2, _ = call("POST", BASE + "/comments",
                  {"stringId": 999999999, "text": "scope probe", "type": "comment"})
    if st2 == 403:
        print("  write scope    MISSING. Reading works, sending will fail.")
        print("                 Regenerate with 'Source files & strings' = Read AND Write.")
        return False
    if st2 in (400, 404):
        print("  write scope    ok (nothing was created by this check)")
        return True
    print("  write scope    UNCLEAR (HTTP %s)" % st2)
    return False


def cmd_draft(args):
    """Draft answers through the API. The only part of tq that spends money."""
    sys.path.insert(0, HERE)
    import draft
    state = load()
    if args.keys:
        wanted = [x for x in args.keys.replace(" ", "").split(",") if x]
        targets = [g for g in state if g["key"] in wanted]
    else:
        targets = [g for g in state if not g.get("body")]
    if not targets:
        print("nothing to draft."); return
    print("drafting %d key(s)%s" % (len(targets), "" if args.apply else " (dry run, use --apply to save)"))
    tot_in = tot_out = 0
    for g in targets:
        q = "\n".join((i.get("question") or "") for i in g["issues"])
        text, usage = draft.ask(g["key"], q, g.get("english", ""), model=args.model)
        if text is None:
            print("  %-46s FAILED %s" % (g["key"][:46], usage.get("error"))); continue
        tot_in += usage["input_tokens"]; tot_out += usage["output_tokens"]
        print("=" * 78)
        print("%s   [%s in / %s out%s, %s via %s]"
              % (g["key"], usage["input_tokens"], usage["output_tokens"],
                 ", $%.4f" % usage["cost_usd"] if usage.get("cost_usd") else "",
                 usage["model"], usage.get("via", "?")))
        print("  " + text.replace("\n", "\n  "))
        if args.apply:
            marker = "<!-- tq: hold -->\n" if g.get("hold") else ""
            open(os.path.join(ANSWERS, g["key"] + ".md"), "w").write(marker + text + "\n")
            draft.record(g["key"], usage)
            draft.mark_accepted(g["key"])
    print("=" * 78)
    print("total %d in / %d out across %d key(s)%s"
          % (tot_in, tot_out, len(targets), "" if args.apply else "; nothing saved"))


def cmd_usage(args):
    sys.path.insert(0, HERE)
    import draft
    data = draft.load_usage()
    if not data:
        print("no drafting recorded yet. tq draft --apply writes usage.json."); return
    ti = to = 0
    for k in sorted(data):
        v = data[k]
        ti += v.get("input_tokens", 0); to += v.get("output_tokens", 0)
        tc = locals().get("tc", 0.0) + v.get("cost_usd", 0.0)
        print("  %-52s %6d in %5d out  $%.4f  x%d" % (k[:52], v.get("input_tokens", 0),
              v.get("output_tokens", 0), v.get("cost_usd", 0.0), v.get("drafts", 1)))
    tc = sum(v.get("cost_usd", 0.0) for v in data.values())
    print("  %-52s %6d in %5d out  $%.4f  across %d key(s)" % ("TOTAL", ti, to, tc, len(data)))


def cmd_facts(args):
    """Collect what the source says about a key, before anyone writes prose."""
    sys.path.insert(0, HERE)
    import keyfacts
    for k in [x for x in args.keys.replace(" ", "").split(",") if x]:
        keyfacts.report(k)


def doctor_rest():
    """Everything past Crowdin and drafting. Returns False if any of it blocks use.

    Each of these used to surface only halfway through a review: a wrong source
    path as a crash, missing Jira credentials at the moment of filing, and a
    renamed remote as replies that quietly lost their commit link.
    """
    sys.path.insert(0, HERE)
    ok = True
    repo = os.path.expanduser(os.environ.get("TQ_SOURCE_REPO") or config._CFG.get("sourceRepo") or "")
    if os.path.isdir(os.path.join(repo, ".git")) or os.path.isfile(os.path.join(repo, ".git")):
        print("  source repo    %s" % repo)
        if config.get("canonicalRemote"):
            import links as _links
            m = _links.canonical_master()
            print("  canonical      %s" % (m or "NOT FOUND: add a remote matching canonicalRemote, "
                                          "or replies lose their commit link"))
            ok = ok and bool(m)
    else:
        print("  source repo    NOT A GIT CHECKOUT: %r (set sourceRepo in config.json)" % repo)
        ok = False
    team = sorted(config.team())
    mine = me()
    if mine:
        print("  crowdin user   %s%s" % (mine, "" if mine in team else
              "  (NOT in team: add it to team.json and push, so every tq knows you)"))
    print("  team           %s" % (", ".join(team) if team else
                                   "not set: add \"team\" to team.json with every Crowdin "
                                   "username that answers translators"))
    ok = ok and bool(team)
    import followup
    u, _t = followup.creds()
    if u:
        st, _ = followup.jira("GET", "myself")
        print("  jira           %s" % ("signed in (filing works)" if st == 200 else
                                       "credentials refused (HTTP %s)" % st))
        ok = ok and st == 200
    else:
        print("  jira           no credentials: reading works, filing [f]/[t] does not "
              "(set JIRA_API_USER and JIRA_API_TOKEN)")
    print("  ripgrep        %s" % ("found" if shutil.which("rg") else
                                   "not found: source scans fall back to grep and can take "
                                   "over a minute per key"))
    hist = os.path.join(config.DATA, "lang-history.tsv")
    print("  history index  %s" % ("present" if os.path.exists(hist) else
                                   "missing: build it once with lib/build-history-index.sh "
                                   "lang-history.tsv (links still work, more slowly)"))
    return ok


def cmd_doctor(args):
    print("  config         %s (yours), %s (shared)" % (config._PATH, config._SHARED))
    creds = os.environ.get("TQ_CREDENTIALS_FILE")
    print("  credentials    %s" % (creds if creds else "TQ_CREDENTIALS_FILE not set"))
    ok = check_token()
    # Optional on purpose: drafting is the only part that costs money, and the
    # queue can be worked end to end with answers written by hand.
    sys.path.insert(0, HERE)
    import draft
    ak = os.environ.get("ANTHROPIC_API_KEY")
    via = draft.backend()
    if via == "api":
        print("  drafting       API key (%d characters, not shown)" % len(ak))
    elif via == "demo":
        print("  drafting       demo drafter (writes from the facts, no model call)")
    elif via == "cli":
        print("  drafting       claude CLI, on your Claude Code login (no key needed)")
    else:
        print("  drafting       unavailable: no claude CLI and no ANTHROPIC_API_KEY")
        print("                 everything except draft and redraft still works")
    ok = doctor_rest() and ok
    if ok:
        print("\nReady. Run 'tq' to review the queue.")
    else:
        print(SETUP)
    sys.exit(0 if ok else 1)


def call(method, url, body=None):
    if os.environ.get("TQ_DEMO"):
        import demo_backend
        return demo_backend.crowdin(method, url, body)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def get_all(path):
    out, off = [], 0
    while True:
        st, res = call("GET", "%s%s%slimit=500&offset=%d"
                       % (BASE, path, "&" if "?" in path else "?", off))
        if st != 200:
            sys.exit("GET %s failed (%s): %s" % (path, st, json.dumps(res)[:300]))
        rows = res.get("data", [])
        out += [x["data"] for x in rows]
        if len(rows) < 500:
            return out
        off += 500


def me():
    st, res = call("GET", API + "/user")
    return (res.get("data") or {}).get("username", "") if st == 200 else ""


def closed_holds(groups, us, links):
    """Held replies whose Crowdin issue was closed anyway, so they stay in view.

    A hold tells the translator we will come back, and only an open issue kept
    the string in the queue. When the issue was resolved regardless, by the
    translator or by an older tq, the promise dropped out of sight with nothing
    tracking it. A string is brought back here while its answer file is still a
    hold, or while a Task filed for it has not been released.
    """
    open_keys = {g["key"] for g in groups.values()}
    fu = _followups()
    keys = set(k for k, v in fu.items() if not v.get("released"))
    for name in os.listdir(ANSWERS) if os.path.isdir(ANSWERS) else []:
        if name.endswith(".md") and "#" not in name:
            if HOLD_RE.match(open(os.path.join(ANSWERS, name)).read()):
                keys.add(name[:-3])
    out = []
    for key in sorted(keys - open_keys):
        st, res = call("GET", BASE + "/strings?scope=identifier&limit=5&filter="
                       + urllib.parse.quote(key))
        s = next((d["data"] for d in (res.get("data") or []) if st == 200
                  and (d["data"].get("identifier") == key or d["data"].get("context") == key)),
                 None)
        if not s:
            continue
        thread = get_all("/comments?stringId=%d" % s["id"])
        ours = [c.get("createdAt") or "" for c in thread
                if (c.get("user") or {}).get("username") in us]
        if not ours:
            continue                              # an unsent draft, not a broken promise
        asked = [c for c in thread if c.get("type") == "issue"]
        if not asked:
            continue
        body, src = "", key + ".md"
        path = os.path.join(ANSWERS, src)
        if os.path.exists(path):
            body = open(path).read().strip()
        hold = bool(HOLD_RE.match(body))
        mentions = []
        for c in sorted(thread, key=lambda c: c.get("createdAt") or ""):
            u = (c.get("user") or {}).get("username")
            if u and u not in SKIP_MENTION and u not in mentions:
                mentions.append(u)
        g = {"stringId": s["id"], "key": key, "english": s.get("text") or "",
             "fileId": s.get("fileId"), "mentions": mentions,
             "issues": [{"commentId": c["id"], "lang": c.get("languageId"),
                         "username": (c.get("user") or {}).get("username"),
                         "fullName": (c.get("user") or {}).get("fullName"),
                         "asked": (c.get("createdAt") or "")[:10],
                         "createdAt": c.get("createdAt") or "",
                         "issueType": c.get("issueType"),
                         "question": strip_handles(c.get("text") or "")} for c in asked],
             "body": HOLD_RE.sub("", body, count=1).strip(), "bodyFrom": src,
             "hold": hold, "stale": False, "repliedAt": max(ours)[:10],
             # Still answered-and-waiting while the reply is a hold; once the
             # file holds the final answer, review offers to send it.
             "awaiting": hold, "issueClosed": True}
        g.update(links.get(key, {}))
        out.append(g)
        time.sleep(0.12)
    return out


def cmd_pull(args):
    links = json.load(open(LINKS)) if os.path.exists(LINKS) else {}
    whoami = me()
    # A reply from anyone on the team answers the thread. Counting only the
    # person running tq would draft and offer a second answer to a thread a
    # teammate had already answered.
    us = config.team() | ({whoami} if whoami else set())
    issues = get_all("/comments?type=issue&issueStatus=unresolved")
    print("open issues: %d" % len(issues))

    # One source string can carry several open issues, one per language, and they
    # share a single comment thread. Group them so the thread gets ONE reply and
    # every issue on it is resolved, instead of near-duplicate comments.
    groups = {}
    for c in issues:
        s = c.get("string") or {}
        g = groups.setdefault(s.get("id"), {
            "stringId": s.get("id"), "key": s.get("context") or "",
            "english": s.get("text") or "", "fileId": s.get("fileId"), "issues": [],
        })
        g["issues"].append({
            "commentId": c["id"], "lang": c.get("languageId"),
            "username": (c.get("user") or {}).get("username"),
            "fullName": (c.get("user") or {}).get("fullName"),
            "asked": (c.get("createdAt") or "")[:10],
            "createdAt": c.get("createdAt") or "",
            "issueType": c.get("issueType"),
            "question": strip_handles(c.get("text") or ""),
        })

    for g in groups.values():
        thread = get_all("/comments?stringId=%d" % g["stringId"])
        openers = [i["username"] for i in g["issues"]]
        mentions = list(dict.fromkeys(openers))
        for c in sorted(thread, key=lambda c: c.get("createdAt") or ""):
            u = (c.get("user") or {}).get("username")
            if u and u not in SKIP_MENTION and u not in mentions:
                mentions.append(u)
        g["mentions"] = mentions
        # A follow-up question on a key already answered must NOT reuse the old
        # body, so a per-issue file wins over the per-key one.
        body, src = "", ""
        for i in g["issues"]:
            ip = os.path.join(ANSWERS, "%s#%s.md" % (g["key"], i["commentId"]))
            if os.path.exists(ip):
                body, src = open(ip).read().strip(), os.path.basename(ip)
                break
        if not body:
            kp = os.path.join(ANSWERS, g["key"] + ".md")
            if os.path.exists(kp):
                body, src = open(kp).read().strip(), os.path.basename(kp)
        # A draft that opens with the hold marker is an interim reply: it says we
        # are looking into it, so the issue must stay OPEN. Resolving it would
        # drop the question out of the translator's queue while it is still
        # genuinely unanswered, and nothing would bring anyone back to it.
        g["hold"] = bool(HOLD_RE.match(body))
        if g["hold"]:
            body = HOLD_RE.sub("", body, count=1).strip()
        g["body"], g["bodyFrom"] = body, src

        # An issue opened AFTER our own last comment on this thread is a reply to
        # us, so the existing body answers a question nobody asked. File mtimes
        # cannot show this (copying a file resets them); comment order can.
        ours = [c.get("createdAt") or "" for c in thread
                if (c.get("user") or {}).get("username") in us]
        last_ours = max(ours) if ours else ""
        followups = [i for i in g["issues"]
                     if last_ours and (i.get("createdAt") or "") > last_ours]
        g["stale"] = bool(followups) and not any(
            os.path.exists(os.path.join(ANSWERS, "%s#%s.md" % (g["key"], i["commentId"])))
            for i in followups)
        g["repliedAt"] = last_ours[:10]
        # Answered, and every open issue predates our reply: the issue is being
        # kept open on purpose (a hold, or a follow-up Task), not waiting for us.
        g["awaiting"] = bool(last_ours) and not followups

        g.update(links.get(g["key"], {}))
        time.sleep(0.12)

    state = sorted(list(groups.values()) + closed_holds(groups, us, links),
                   key=lambda g: g["key"])
    for i, g in enumerate(state, 1):
        g["n"] = i
    json.dump(state, open(STATE, "w"), indent=1)
    print("strings: %d  (grouped from %d issues)" % (len(state), len(issues)))
    missing = [g["key"] for g in state if not g["body"]]
    stale = [g["key"] for g in state if g.get("stale")]
    awaiting = [g["key"] for g in state if g.get("awaiting") and not g.get("issueClosed")]
    if awaiting:
        print("answered, kept open on purpose: %d %s" % (len(awaiting), awaiting))
    closed = [g["key"] for g in state if g.get("issueClosed")]
    if closed:
        print("held, but the Crowdin issue was closed: %d %s" % (len(closed), closed))
    fu = _followups()
    ready = check_followups(state, fu)
    if ready:
        print("follow-up Task DONE, hold can be released: %s" % ready)
    # Written again, now that each filed Task's status is on the state, so the
    # review screen can offer the release without asking Jira a second time.
    json.dump(state, open(STATE, "w"), indent=1)
    nolink = [g for g in state if not g.get("commitUrl") and not g.get("ctxInternal")]
    if nolink:
        print("looking up the commit and ticket for %d new key(s)..." % len(nolink), flush=True)
        for g in nolink:
            resolve_links(g)
        json.dump(state, open(STATE, "w"), indent=1)
    nolink = [g["key"] for g in state if not g.get("commitUrl")]
    print("drafted: %d   MISSING a draft: %d %s"
          % (len(state) - len(missing), len(missing), missing if missing else ""))
    if stale:
        print("STALE (a newer question arrived after the draft was written): %s" % stale)
    if nolink:
        print("no commit found for: %s" % nolink)


def resolve_links(g):
    """Find a key's commit and context ticket now, and cache them in links.json.

    These used to come only from a batch run by hand, so a key asked about for
    the first time went out with no context ticket until someone remembered it.
    """
    sys.path.insert(0, HERE)
    import links as _links
    found = _links.resolve(_links.introducing_commit(g["key"]))
    if not found.get("commitSha"):
        return False
    keep = {k: found.get(k, "") for k in
            ("ctxTicket", "ctxSummary", "ctxUrl", "commitUrl", "commitSha", "ctxInternal")}
    saved = json.load(open(LINKS)) if os.path.exists(LINKS) else {}
    saved[g["key"]] = keep
    json.dump(saved, open(LINKS, "w"), indent=1, sort_keys=True)
    _LINKS_CACHE[g["key"]] = keep
    g.update(keep)
    return True


def load():
    if not os.path.exists(STATE):
        sys.exit("no state.json - run `tq.py pull` first")
    return json.load(open(STATE))


IMAGE_NOTE = ("The image is generated from the editor's own source code rather than captured "
              "from a screen, so it shows exactly what the code draws.")


def has_image(key):
    """Comparison images are rendered by a product-specific module left out here."""
    return False


_LINKS_CACHE = {}


def links_for(g):
    """Read links.json at SEND time, not pull time.

    Baking links into state.json during pull meant a key whose links were
    resolved afterwards posted as bare prose until the next pull. Reading here
    keeps the reply correct regardless of when the links were worked out.
    """
    if not _LINKS_CACHE and os.path.exists(LINKS):
        try:
            _LINKS_CACHE.update(json.load(open(LINKS)))
        except ValueError:
            pass
    fresh = _LINKS_CACHE.get(g["key"], {})
    return (g.get("ctxUrl") or fresh.get("ctxUrl", ""),
            g.get("ctxSummary") or fresh.get("ctxSummary", ""),
            g.get("commitUrl") or fresh.get("commitUrl", ""),
            g.get("ctxInternal") or fresh.get("ctxInternal", ""))


def compose(g, with_image=False):
    mention = " ".join("@" + m for m in g["mentions"])
    parts = ["%s %s" % (mention, g["body"])]
    if with_image:
        # Say so, so a reconstruction is never mistaken for the shipped UI.
        parts.append(IMAGE_NOTE)
    ctx_url, ctx_summary, commit_url, ctx_internal = links_for(g)
    tail = []
    # Say so when a link is missing. Omitting the line silently reads as though
    # nobody looked, and the translator cannot tell the difference.
    #
    # A ticket we traced but cannot link is its own case, distinct from finding
    # nothing. The commit message names the ticket in its first word, so
    # claiming we found no ticket contradicts what the translator reads the
    # moment they follow the commit link.
    if ctx_url:
        tail.append("Context ticket: %s (%s)" % (ctx_url, ctx_summary))
    if commit_url:
        tail.append("Language key added in: %s" % commit_url)
    if ctx_internal and not ctx_url:
        tail.append("This work was tracked as %s, which is an internal ticket "
                    "we cannot share, so the commit above is the fullest public "
                    "record of it." % ctx_internal)
    elif not ctx_url and not commit_url:
        tail.append("We could not trace this string to a commit or a Jira ticket.")
    elif not ctx_url:
        tail.append("We could not trace this string to a Jira ticket.")
    elif not commit_url:
        tail.append("We could not trace this string to the commit that added it.")
    if tail:
        parts.append("\n".join(tail))
    return "\n\n".join(parts)


def pick(state, only):
    """Select by language key (stable) or by ordinal (convenient but fragile).

    state.json is untracked shared state with no locking, so another session
    running `pull` can renumber it between a preview and a post. Keys survive
    that; ordinals do not. Both are accepted, and post resolves them against a
    freshly pulled state so a stale number cannot address the wrong thread.
    """
    if not only:
        return state
    tokens = [t for t in only.replace(" ", "").split(",") if t]
    nums = {int(t) for t in tokens if t.lstrip("-").isdigit()}
    keys = {t for t in tokens if not t.lstrip("-").isdigit()}
    chosen = [g for g in state if g["n"] in nums or g["key"] in keys]
    missing = keys - {g["key"] for g in chosen}
    if missing:
        sys.exit("no such key in state.json: %s" % ", ".join(sorted(missing)))
    unseen = nums - {g["n"] for g in chosen}
    if unseen:
        sys.exit("no such number in state.json: %s" % sorted(unseen))
    return chosen


def cmd_preview(args):
    state = pick(load(), args.only)
    for g in state:
        langs = ",".join(sorted({i["lang"] for i in g["issues"]}))
        print("=" * 78)
        print("#%-2d %s   [%s]   string %s   %d issue(s)"
              % (g["n"], g["key"], langs, g["stringId"], len(g["issues"])))
        wrapped("EN: ", g["english"], "    ")
        for i in g["issues"]:
            wrapped("Q  (%s, %s): " % (i["username"], i["asked"]), i["question"], "    ")
        gap = placeholder_gap(g)
        if gap:
            print("    !! reply does not mention %s, which the English uses"
                  % ", ".join(gap))
        if not g["body"]:
            print("    !! NO DRAFT - would be skipped")
            continue
        if g.get("stale"):
            print("    !! FOLLOW-UP: this issue was raised after our reply on %s, and the"
                  % g.get("repliedAt"))
            print("       existing draft (%s) answers the earlier question. Would be skipped."
                  % g.get("bodyFrom"))
            print("       Write answers/%s#%s.md to answer it."
                  % (g["key"], g["issues"][0]["commentId"]))
            continue
        print("    -- would POST to string %s --" % g["stringId"])
        for line in compose(g).split("\n"):
            print("       " + line)
        if g.get("hold"):
            print("    -- would LEAVE OPEN (hold): %s"
                  % ", ".join(str(i["commentId"]) for i in g["issues"]))
        else:
            print("    -- would RESOLVE comment ids: %s"
                  % ", ".join(str(i["commentId"]) for i in g["issues"]))
    print("=" * 78)
    print("%d string(s) previewed. Nothing was sent." % len(state))


def editor_url(g):
    lang = (g["issues"][0]["lang"] or "").lower().replace("-", "")
    return ("%s/%s/%%s/en-%%s#%%s" % (config.get("crowdinEditor"), config.get("crowdinProjectSlug"))
            % (g.get("fileId"), lang, g["stringId"]))


# Whether an image is likely to earn its place. The reviewer decides; this only
# puts a recommendation in front of them, with the reason it was made.
_ASKS_TO_SEE = re.compile(
    r"what does it mean|screenshot|what is (a|an|the)\b|look(s)? like|picture|image|visual",
    re.I)


def image_hint(g):
    """Return (level, reason). level is 'recommended', 'available' or None."""
    if not has_image(g["key"]):
        return None, ""
    for i in g["issues"]:
        if _ASKS_TO_SEE.search(i["question"] or ""):
            return "recommended", "the translator asked what it looks like"
    # A bare visual noun carries no sentence to reason from, so a picture helps.
    if len(g["english"].split()) <= 2:
        return "recommended", "a one-word visual term with no sentence around it"
    return "available", "the string names something visual"


def resolve_issues(g):
    """Mark every open issue on the thread resolved. Returns the marks."""
    marks = []
    for i in g["issues"]:
        st2, _ = call("PATCH", "%s/comments/%s" % (BASE, i["commentId"]),
                      [{"op": "replace", "path": "/issueStatus", "value": "resolved"}])
        marks.append("%s=%s" % (i["commentId"], "ok" if st2 == 200 else "FAIL(%s)" % st2))
    return marks


def send_one(g, with_image=False, leave_open=False):
    """Post the reply for one string and resolve every open issue on it.

    with_image renders the comparison from source, uploads it, and attaches it to
    the COMMENT. It is never tagged to the string as a screenshot: generated
    images do not belong under CONTEXT beside real product screenshots.
    """
    body = {"stringId": g["stringId"], "text": compose(g, with_image),
            "targetLanguageId": g["issues"][0]["lang"], "type": "comment"}
    st, res = call("POST", BASE + "/comments", body)
    if st not in (200, 201):
        return False, "POST failed %s %s" % (st, json.dumps(res)[:160])
    posted = res["data"]["id"]
    if g.get("issueClosed"):
        _release(g)
        return True, "posted %s (the issue was already closed)" % posted
    if g.get("hold") or leave_open:
        return True, "posted %s, left %d issue(s) OPEN for follow-up" % (
            posted, len(g["issues"]))
    marks = []
    for i in g["issues"]:
        st2, _ = call("PATCH", "%s/comments/%s" % (BASE, i["commentId"]),
                      [{"op": "replace", "path": "/issueStatus", "value": "resolved"}])
        marks.append("%s=%s" % (i["commentId"], "ok" if st2 == 200 else "FAIL(%s)" % st2))
    return True, "posted %s%s, resolved %s" % (
        posted, " with image" if with_image else "", " ".join(marks))


# Whole words only. Prefix matching sent the comment when the reviewer typed the
# word the prompt itself shows for skipping, because "skip" starts with "s", and
# sent the whole remaining queue on "abort". Anything unrecognised is re-asked
# rather than quietly treated as a skip, so a typo cannot pass for an answer.
_CHOICES = {"s": "s", "send": "s", "g": "g", "image": "g", "i": "i",
            "k": "k", "skip": "k", "": "k",
            "a": "a", "all": "a", "q": "q", "quit": "q",
            "r": "r", "redraft": "r", "f": "f", "sf": "f", "file": "f",
            "rf": "rf", "fr": "rf"}


def read_choice(prompt, img):
    while True:
        raw = input(prompt).strip().lower()
        c = _CHOICES.get(raw)
        if c is None:
            print("  '%s' is not one of the options. Enter on its own skips." % raw)
            continue
        if c in ("g", "i") and not img:
            print("  no image is available for this one.")
            continue
        return c


_USAGE = {}


def _usage_for(key):
    """Token counts for a key, when it was drafted through tq draft.

    Answers written by hand have no entry, and that is the honest result rather
    than a zero: nothing was spent because no model was called.
    """
    if not _USAGE:
        sys.path.insert(0, HERE)
        try:
            import draft
            _USAGE.update(draft.load_usage() or {"__none__": {}})
        except Exception:
            _USAGE["__none__"] = {}
    return _USAGE.get(key)


def redraft(g):
    """Offer three fresh answers and let the reviewer take one.

    Three options come back from a single call. The facts are most of the input,
    so asking three times would pay for them three times over; asking once and
    paying for the longer output buys the same choice for less.

    Returns True when the draft was replaced.
    """
    sys.path.insert(0, HERE)
    try:
        import draft
    except ImportError as e:
        print("  redraft unavailable: %s" % e); return False
    if draft.backend() is None:
        print("  redraft needs the claude CLI or ANTHROPIC_API_KEY. Run 'tq doctor'.")
        return False
    q = "\n".join((i.get("question") or "") for i in g["issues"])
    print("  asking for three alternatives (3 independent calls)%s..."
          % (", as an interim hold reply" if g.get("hold") else ""))
    try:
        variants, usage = draft.ask_variations(g["key"], q, g.get("english", ""),
                                               hold=bool(g.get("hold")))
    except SystemExit as e:
        print("  %s" % e); return False
    if not variants:
        print("  redraft failed: %s" % usage.get("error", "no text returned")); return False
    # Bill it here, not after the choice. The call has already happened, so
    # declining every option still cost what it cost, and a ledger that only
    # counts accepted drafts under-reports the real spend.
    draft.record(g["key"], usage)
    _USAGE.clear()
    for n, pc in enumerate(usage.get("per_call", []), 1):
        if pc.get("error"):
            print("    call %d  FAILED: %s" % (n, pc["error"]))
        else:
            print("    call %d  %5d in / %4d out%s" % (
                n, pc["input_tokens"], pc["output_tokens"],
                "  $%.4f" % pc["cost_usd"] if pc.get("cost_usd") else ""))
    print("    ------  %5d in / %4d out%s  total for the fanout, %s via %s"
          % (usage.get("input_tokens", 0), usage.get("output_tokens", 0),
             "  $%.4f" % usage["cost_usd"] if usage.get("cost_usd") else "",
             usage.get("model", "?"), usage.get("via", "?")))
    if usage.get("failed"):
        print("    %d of %d calls failed, and were still charged for what they used."
              % (usage["failed"], usage.get("calls", 0)))
    if usage.get("duplicates"):
        print("    %d sample(s) came back identical and were collapsed."
              % usage["duplicates"])
    for n, v in enumerate(variants, 1):
        print("\n  [%d] %s" % (n, v.replace("\n", "\n      ")))
    valid = [str(i) for i in range(1, len(variants) + 1)]
    keep_label = "keep the current draft" if g.get("body") else "skip, saving none"
    kept = "keeping the current draft" if g.get("body") else "nothing saved"
    while True:
        try:
            pick = input("\n  keep which? [%s]  or [k] to %s > "
                         % ("-".join([valid[0], valid[-1]]) if len(valid) > 1 else "1",
                            keep_label)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  %s." % kept); return False
        if pick in ("k", ""):
            print("  %s." % kept); return False
        if pick in valid:
            chosen = variants[int(pick) - 1]
            # A hold is marked in the file, not in the text. Writing the chosen
            # text over the file dropped the marker, and the next [s] would then
            # have resolved an issue that was meant to stay open.
            marker = "<!-- tq: hold -->\n" if g.get("hold") else ""
            open(os.path.join(ANSWERS, g["key"] + ".md"), "w").write(marker + chosen + "\n")
            draft.mark_accepted(g["key"])
            _USAGE.clear()
            g["body"] = chosen
            print("  saved to answers/%s.md." % g["key"])
            return True
        print("  pick one of %s, or k to %s." % (", ".join(valid), keep_label))


# Filed Tasks, by language key, read from Jira once per run. Jira is the record
# because it is the one place every user of tq can see: the local file this
# replaced only knew what one person had filed, so a teammate was offered [t]
# for a string that already had a Task.
_FU = None


def _followups():
    global _FU
    if _FU is None:
        sys.path.insert(0, HERE)
        import followup
        _FU = followup.find_all()
        if _FU is None:
            print("!! could not read filed Tasks from Jira; filing is off for this run "
                  "so nothing is filed twice.")
    return _FU or {}


def _followups_known():
    _followups()
    return _FU is not None


def _filed(g, ticket, url):
    _followups()[g["key"]] = {"ticket": ticket, "url": url, "stringId": str(g["stringId"]),
                              "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "Open",
                              "category": "new", "resolution": None, "released": False}


def _release(g):
    """Mark the Task released in Jira, so every user stops being offered it."""
    entry = _followups().get(g["key"])
    if not entry or entry.get("released"):
        return
    sys.path.insert(0, HERE)
    import followup
    ok, _ = followup.add_label(entry["ticket"], followup.RELEASED)
    entry["released"] = ok
    if not ok:
        print("  !! could not label %s as released; it may be offered again." % entry["ticket"])


def _filing_blocked(g):
    """Why filing must not go ahead, or None."""
    if not _followups_known():
        return "Jira could not be read, so a Task filed by someone else would be missed."
    fu = _followups().get(g["key"])
    if fu:
        return "%s is already filed for this key." % fu["url"]
    return None


def _promises(text):
    sys.path.insert(0, HERE)
    import followup
    return followup.promises_followup(text)


def ticket_preview(g):
    """Build the Task for a key and show it. Creates nothing."""
    sys.path.insert(0, HERE)
    import followup
    links = json.load(open(LINKS)) if os.path.exists(LINKS) else {}
    print("  looking up the owning team and the call site...", flush=True)
    import keyfacts
    sites = keyfacts.call_sites(g["key"])        # one scan, shared by both steps
    summary, kind = followup.default_summary(g, sites)
    fields, comps, owners, rel = followup.build(g, links, g.get("body") or "", summary, sites,
                                                   editor_url(g))
    fields["_kind"] = kind
    print("  Task for %s" % PROJECT_LABEL)
    print("    problem     %s" % {"unused": "key is used nowhere", "placeholders":
          "placeholder count does not match the code", "label": "a UI name reads as prose",
          "wording": "wording or punctuation", "model": "summarised by the model",
          "other": "not classified"}.get(fields.get("_kind"), "?"))
    print("    summary     %s" % fields["summary"])
    print("    components  %s" % (", ".join(c["name"] for c in comps) or "NONE FOUND"))
    print("    code        %s" % (rel or "no call site found"))
    print("    owners      %s" % (", ".join(owners) or "none listed in CODEOWNERS"))
    return followup, links, fields, comps


PROJECT_LABEL = "%s (anything in it may be read outside the team)" % (config.get("jiraProject") or "Jira")


def send_and_file(g):
    """File the follow-up Task, then send the reply, then print the link.

    Filing comes first on purpose. If Jira refuses, the promise has not been
    sent yet, so there is no reply out in the world pointing at a ticket that
    does not exist.
    """
    why = _filing_blocked(g)
    if why:
        return False, "%s Nothing was filed or sent." % why
    followup, links, fields, comps = ticket_preview(g)
    if not comps:
        return False, ("no Jira component could be found for this key, and the project requires "
                       "one. Nothing was filed or sent.")
    try:
        edited = input("    summary (type a new one to replace it, Enter keeps it) > ").strip()
        if edited:
            fields["summary"] = edited.replace("\u2014", ",")[:250]
        ids = ", ".join(str(i["commentId"]) for i in g["issues"])
        # [f] leaves the issue open. An open issue is the only thing anyone is
        # prompted to look at; a paper trail on the Task alone gets lost,
        # since nothing reviews work that is not marked as an issue in Crowdin.
        fate = "leave issue %s OPEN until the Task is done" % ids
        if input("    file this Task, send the reply, and %s? [y/N] > " % fate
                 ).strip().lower() not in ("y", "yes"):
            return False, "nothing filed, nothing sent."
    except (EOFError, KeyboardInterrupt):
        return False, "nothing filed, nothing sent."
    fields.pop("_kind", None)
    key, url = followup.create(fields)
    if not key:
        return False, "the Task was not filed (%s). The reply was NOT sent." % url
    _filed(g, key, url)
    ok, detail = send_one(g, leave_open=True)
    if not ok:
        return False, ("filed %s but the reply failed: %s. Send it with [s] when ready."
                       % (url, detail))
    return True, "%s\n  FILED: %s\n  (pass the link along in Slack by hand)" % (detail, url)


def cmd_ticket(args):
    """Preview the follow-up Task for a key. Creates nothing."""
    state = load()
    for k in [x for x in args.keys.replace(" ", "").split(",") if x]:
        g = next((x for x in state if x["key"] == k), None)
        if not g:
            print("no such key in the queue: %s" % k); continue
        print("=" * 78)
        followup, links, fields, comps = ticket_preview(g)
        print("    ---- description ----")
        for para in fields["description"]["content"]:
            print("    " + "".join(n.get("text", "") for n in para["content"])[:160])
    print("=" * 78)
    print("preview only. Nothing was filed.")


def check_followups(state, fu):
    """Ask Jira how each filed Task stands, and mark those that are finished.

    This is what lets a kept-open issue close. Without it the only record that
    the Task was done is the Task itself, which nobody here is watching.
    """
    if not fu:
        return []
    ready = []
    for g in state:
        entry = fu.get(g["key"])
        if not entry:
            continue
        g["fuTicket"], g["fuUrl"] = entry["ticket"], entry["url"]
        g["fuStatus"], g["fuResolution"] = entry["status"], entry["resolution"]
        g["fuDone"] = entry["category"] == "done"
        if g["fuDone"]:
            ready.append(g["key"])
    return ready


def offer_release(g):
    """The Task is done: offer to resolve the Crowdin issue, nothing else."""
    ids = ", ".join(str(i["commentId"]) for i in g["issues"])
    print("  %s is %s%s." % (g["fuTicket"], g.get("fuStatus") or "done",
                            " (%s)" % g["fuResolution"] if g.get("fuResolution") else ""))
    try:
        ans = input("  release the hold and resolve issue %s? [y/N] > " % ids).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if ans not in ("y", "yes"):
        print("  left open."); return False
    marks = resolve_issues(g)
    if all(m.endswith("=ok") for m in marks):
        _release(g)
    print("  RESOLVED: %s" % " ".join(marks))
    return all(m.endswith("=ok") for m in marks)


def cmd_followups(args):
    """Every filed follow-up and where its Task stands."""
    fu = _followups()
    if not fu:
        print("no follow-ups filed yet."); return
    for k in sorted(fu):
        e = fu[k]
        state_ = "released" if e.get("released") else (
            "DONE, ready to release" if e["category"] == "done" else (e["status"] or "status unknown"))
        print("  %-11s %-26s %s" % (e["ticket"], state_[:26], k[:60]))


def file_only(g):
    """File the follow-up Task for a thread already answered. Posts nothing.

    A hold sent with [s] goes out promising to come back, with no Task behind
    it, and the thread is then skipped as answered. This closes that gap without
    answering the translator a second time.
    """
    why = _filing_blocked(g)
    if why:
        return False, "%s Nothing filed." % why
    followup, links, fields, comps = ticket_preview(g)
    if not comps:
        return False, "no Jira component could be found, and the project requires one. Nothing filed."
    try:
        edited = input("    summary (type a new one to replace it, Enter keeps it) > ").strip()
        if edited:
            fields["summary"] = edited.replace("\u2014", ",")[:250]
        if input("    file this Task? The reply is already posted%s. [y/N] > "
                 % ("" if g.get("issueClosed") else ", and the issue stays open")
                 ).strip().lower() not in ("y", "yes"):
            return False, "nothing filed."
    except (EOFError, KeyboardInterrupt):
        return False, "nothing filed."
    fields.pop("_kind", None)
    key, url = followup.create(fields)
    if not key:
        return False, "the Task was not filed (%s)." % url
    _filed(g, key, url)
    return True, "FILED: %s\n  (pass the link along in Slack by hand)" % url


def autodraft(state, only=None):
    """Write one draft for every new question, so review opens on a reply to judge.

    A new question used to arrive with nothing to show, and the only way forward
    was a three-option fanout per key. One call each is a third of that, and [r]
    is still there when the first draft is not good enough.

    Facts are gathered one key at a time: keyfacts reports on stdout, and two
    threads swapping stdout at once scramble each other's facts. Only the model
    calls, which are most of the wait, run side by side.
    """
    wanted = {g["key"] for g in pick(state, only)}
    todo = [g for g in state if g["key"] in wanted and not g["body"]
            and not g.get("awaiting") and not g.get("stale")]
    if not todo:
        return 0
    sys.path.insert(0, HERE)
    try:
        import draft
    except ImportError as e:
        print("drafting unavailable: %s" % e); return 0
    if draft.backend() is None:
        print("%d new question(s) have no draft, and drafting needs the claude CLI. "
              "Run 'tq doctor'." % len(todo))
        return 0
    print("drafting %d new question(s), one call each..." % len(todo), flush=True)
    jobs = []
    for g in todo:
        q = "\n".join((i.get("question") or "") for i in g["issues"])
        jobs.append((g, q, draft.facts_for(g["key"])))
    out = {}
    def one(g, q, facts):
        try:
            out[g["key"]] = draft.ask(g["key"], q, g.get("english", ""), facts=facts)
        except Exception as e:                    # one failure must not sink the rest
            out[g["key"]] = (None, {"error": str(e)})
    threads = [threading.Thread(target=one, args=j) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    made = 0
    for g, _q, _f in jobs:
        text, usage = out.get(g["key"], (None, {"error": "no result"}))
        if not text:
            print("  %-46s FAILED %s" % (g["key"][:46], usage.get("error", "")))
            continue
        # Billed whether or not the draft is sent: the call has been made.
        draft.record(g["key"], usage)
        draft.mark_accepted(g["key"])
        open(os.path.join(ANSWERS, g["key"] + ".md"), "w").write(text + "\n")
        g["body"], g["bodyFrom"] = text, g["key"] + ".md"
        made += 1
        print("  %-46s %5d in / %3d out%s" % (
            g["key"][:46], usage.get("input_tokens", 0), usage.get("output_tokens", 0),
            "  $%.4f" % usage["cost_usd"] if usage.get("cost_usd") else ""))
    _USAGE.clear()
    json.dump(state, open(STATE, "w"), indent=1)
    return made


def update_self():
    if os.environ.get("TQ_DEMO"):
        return
    """Fast-forward this checkout, so everyone works from the team's latest drafts."""
    # fetch + a fast-forward merge rather than pull: a pull.rebase setting turns
    # pull into a rebase, which refuses to run over any edited draft, while a
    # fast-forward merge only stops when an incoming file collides with one.
    r = subprocess.run(["git", "-C", ROOT, "fetch", "-q"], capture_output=True, text=True)
    if r.returncode == 0:
        r = subprocess.run(["git", "-C", ROOT, "merge", "--ff-only", "-q", "@{u}"],
                           capture_output=True, text=True)
    if r.returncode != 0:
        print("!! could not update tq from git (continuing with this copy): %s"
              % (r.stderr.strip().splitlines() or ["unknown error"])[-1][:160])


def cmd_review(args):
    if not args.no_refresh:
        update_self()
        cmd_pull(argparse.Namespace())
        print()
    if not getattr(args, "no_draft", False) and autodraft(load(), args.only):
        print()
    queue = [g for g in pick(load(), args.only)]
    sent = skipped = 0
    send_rest = False
    for idx, g in enumerate(queue, 1):
        langs = ",".join(sorted({i["lang"] for i in g["issues"]}))
        print("=" * 78)
        print("[%d/%d]  #%-2d %s   [%s]   %d issue(s)"
              % (idx, len(queue), g["n"], g["key"], langs, len(g["issues"])))
        wrapped("EN: ", g["english"], "        ")
        for i in g["issues"]:
            wrapped("Q (%s, %s): " % (i["username"], i["asked"]), i["question"], "        ")
        gap = placeholder_gap(g)
        if gap:
            print("        !! reply does not mention %s, which the English uses"
                  % ", ".join(gap))
        u = _usage_for(g["key"])
        if u:
            # Spend and authorship are separate facts. A declined redraft costs
            # tokens while the text shown is still the hand-written one, so this
            # line reports what was spent and, separately, who wrote the text.
            rounds, calls = u.get("drafts", 1), u.get("calls", 3 * u.get("drafts", 1))
            print("        spent on drafts: %d in / %d out%s, %d call(s) in %d round(s), %s"
                  % (u.get("input_tokens", 0), u.get("output_tokens", 0),
                     ", $%.4f" % u["cost_usd"] if u.get("cost_usd") else "",
                     calls, rounds, u.get("model", "?")))
            print("        current text:    %s"
                  % ("chosen from a model draft" if u.get("accepted")
                     else "written by hand (no model draft accepted)"))
        print("        %s" % editor_url(g))
        level, why = image_hint(g)
        if level:
            print("        image %s: %s" % (level.upper(), why))
        print("-" * 78)
        if g.get("issueClosed") and g.get("awaiting"):
            fu = _followups().get(g["key"])
            print("  HELD %s, but the Crowdin issue was closed, so only this list keeps it in view."
                  % (g.get("repliedAt") or "earlier"))
            if fu and g.get("fuDone"):
                print("  %s is %s. Replace the hold in answers/%s.md with the final answer,"
                      % (fu["ticket"], g.get("fuStatus") or "done", g["key"]))
                print("  and the next tq offers to send it.")
            elif fu:
                print("  tracked by %s, %s." % (fu["url"], g.get("fuStatus") or "status unknown"))
            else:
                try:
                    ans = input("  [t] file the follow-up Task now (posts nothing)  "
                                "Enter skips > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\n  stopped."); break
                if ans in ("t", "task", "f", "file"):
                    ok, detail = file_only(g)
                    print("  %s %s" % ("DONE:" if ok else "NOT FILED:", detail))
                    sent += ok; skipped += (not ok); print(); continue
            skipped += 1; print(); continue
        if g.get("awaiting") and g.get("fuDone"):
            released = offer_release(g)
            sent += released; skipped += (not released); print(); continue
        if g.get("awaiting"):
            fu = _followups().get(g["key"])
            print("  ANSWERED %s, issue kept open on purpose (%s)."
                  % (g.get("repliedAt") or "earlier",
                     "tracked by %s, %s" % (fu["url"], g.get("fuStatus") or "status unknown")
                     if fu else "hold, no Task filed"))
            if not fu:
                # The reply promised to come back, and nothing tracks it yet.
                try:
                    ans = input("  [t] file the follow-up Task now (posts nothing)  "
                                "Enter skips > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\n  stopped."); break
                if ans in ("t", "task", "f", "file"):
                    ok, detail = file_only(g)
                    print("  %s %s" % ("DONE:" if ok else "NOT FILED:", detail))
                    sent += ok; skipped += (not ok); print(); continue
            print("  Skipping, so it is not answered twice. %s" % (
                "tq offers to resolve it once %s is done." % fu["ticket"] if fu
                else "Resolve it in Crowdin when done."))
            skipped += 1; continue
        if not g["body"]:
            # The control room drafts here rather than sending the reviewer off
            # to run a separate command and come back.
            try:
                ans = input("  NO DRAFT (drafting failed above).  [d]raft three options now  [q]uit  "
                            "(Enter skips) > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  stopped."); break
            if ans in ("q", "quit"):
                print("  stopped."); break
            if ans not in ("d", "draft") or not redraft(g):
                print("  skipped."); skipped += 1; print(); continue
        if g.get("stale"):
            print("  FOLLOW-UP: the draft answers an earlier question - skipping.")
            print("  Write answers/%s#%s.md to answer it." % (g["key"], g["issues"][0]["commentId"]))
            skipped += 1; continue
        for line in compose(g).split("\n"):
            print("  " + line)
        print("-" * 78)
        if any(re.search(r"glossar", i.get("question") or "", re.I) for i in g["issues"]):
            print("  !! asks about the glossary. We maintain it: make the change in Crowdin "
                  "(Resources > Glossaries)")
            print("     BEFORE sending, since the reply says it is done.")
        fu = _followups().get(g["key"])
        if fu:
            print("  follow-up already filed: %s" % fu.get("url"))
        elif _promises(g.get("body")):
            print("  !! this reply promises a follow-up. [f] sends it and files the Task.")

        if send_rest:
            choice = "s"
        else:
            try:
                img = has_image(g["key"])
                star = "*" if (image_hint(g)[0] == "recommended") else ""
                opts = "  [s]end" + ("  send + ima[g]e%s  [i]mage only" % star if img else "") \
                       + "  [f] send + file  [r]edraft  [rf] redraft + file  [k]skip  [a]ll remaining  [q]uit  (Enter skips) > "
                while True:
                    choice = read_choice(opts, img)
                    if choice == "i":
                        print("  image preview is not part of this build.")
                        continue
                    if choice == "rf":
                        # Redraft, then go straight to filing and sending. Keeping
                        # the current draft still files: the underlying problem
                        # is real whether or not the new wording promises it.
                        if redraft(g):
                            print("-" * 78)
                            for line in compose(g).split("\n"):
                                print("  " + line)
                            print("-" * 78)
                        choice = "f"
                        break
                    if choice == "r":
                        if redraft(g):
                            print("-" * 78)
                            for line in compose(g).split("\n"):
                                print("  " + line)
                            print("-" * 78)
                        continue
                    break
            except (EOFError, KeyboardInterrupt):
                print("\n  stopped."); break
        if choice == "q":
            print("  stopped."); break
        if choice == "a":
            remaining = len(queue) - queue.index(g)
            confirm = input("  send the remaining %d without reviewing them? [y/N] > "
                            % remaining).strip().lower()
            if confirm not in ("y", "yes"):
                print("  skipped."); skipped += 1; print(); continue
            send_rest, choice = True, "s"
        if choice == "g" and has_image(g["key"]):
            ok, detail = send_one(g, with_image=True)
            print("  %s %s" % ("SENT:" if ok else "ERROR:", detail))
            sent += ok; skipped += (not ok); time.sleep(0.2); print(); continue
        if choice == "f":
            ok, detail = send_and_file(g)
            print("  %s %s" % ("DONE:" if ok else "NOT SENT:", detail))
            sent += ok; skipped += (not ok); time.sleep(0.2); print(); continue
        if choice == "s":
            ok, detail = send_one(g)
            print("  %s %s" % ("SENT:" if ok else "ERROR:", detail))
            sent += ok; skipped += (not ok)
            time.sleep(0.2)
        else:
            print("  skipped."); skipped += 1
        print()
    print("=" * 78)
    print("sent %d, skipped %d, of %d reviewed." % (sent, skipped, len(queue)))


def cmd_post(args):
    if not args.only:
        sys.exit("refusing to post without --only; name the numbers explicitly")
    if not args.no_refresh:
        print("refreshing state before posting (another session may have renumbered it)...")
        cmd_pull(argparse.Namespace())
        print()
    state = load()
    chosen = pick(state, args.only)
    print("selected:")
    for g in chosen:
        print("   #%-2d %-46s string %s" % (g["n"], g["key"], g["stringId"]))
    print()
    ready = [g for g in chosen if g["body"] and not g.get("stale") and not g.get("awaiting")]
    if not ready:
        sys.exit("nothing to post: no usable drafts among the selected numbers")

    # Always show the dry run first, so what gets sent is never a surprise.
    cmd_preview(argparse.Namespace(only=args.only))
    n_issues = sum(len(g["issues"]) for g in ready)
    print()
    print("ABOUT TO SEND: %d comment(s) to Crowdin, resolving %d issue(s), as the token's owner."
          % (len(ready), n_issues))

    if not args.apply:
        if not sys.stdin.isatty():
            sys.exit("not a terminal and --apply was not given; nothing sent")
        try:
            ans = input("Post them? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(); sys.exit("aborted; nothing sent")
        if ans not in ("y", "yes"):
            sys.exit("aborted; nothing sent")

    print()
    for g in ready:
        ok, detail = send_one(g)
        print("#%-2d %-42s %s %s" % (g["n"], g["key"], "sent" if ok else "ERROR", detail))
        time.sleep(0.2)


def main():
    p = argparse.ArgumentParser(prog="tq.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="tq %s" % build_id())
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("pull").set_defaults(fn=cmd_pull)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    dr = sub.add_parser("draft")
    dr.add_argument("keys", nargs="?", default="",
                    help="one key or several comma separated; default is every key with no draft")
    dr.add_argument("--model", default=None)
    dr.add_argument("--apply", action="store_true",
                    help="write answers/<key>.md instead of only printing")
    dr.set_defaults(fn=cmd_draft)
    sub.add_parser("usage").set_defaults(fn=cmd_usage)
    sub.add_parser("followups").set_defaults(fn=cmd_followups)
    tk = sub.add_parser("ticket")
    tk.add_argument("keys", help="one key or several comma separated")
    tk.set_defaults(fn=cmd_ticket)
    kf = sub.add_parser("facts")
    kf.add_argument("keys", help="one language key, or several comma separated")
    kf.set_defaults(fn=cmd_facts)
    rv = sub.add_parser("review")
    rv.add_argument("--only")
    rv.add_argument("--no-refresh", action="store_true")
    rv.add_argument("--no-draft", action="store_true",
                    help="do not draft new questions automatically")
    rv.set_defaults(fn=cmd_review)
    pv = sub.add_parser("preview"); pv.add_argument("--only"); pv.set_defaults(fn=cmd_preview)
    po = sub.add_parser("post")
    po.add_argument("--only", required=True)
    po.add_argument("--apply", action="store_true",
                    help="skip the [y/N] prompt (for non-interactive runs)")
    po.add_argument("--no-refresh", action="store_true",
                    help="do not re-pull before posting (not recommended)")
    po.set_defaults(fn=cmd_post)
    a = p.parse_args()
    if not getattr(a, "cmd", None):
        a.fn, a.only, a.no_refresh, a.no_draft = cmd_review, None, False, False
    # On stderr, so it identifies the run without ever landing in piped output.
    sys.stderr.write("tq %s\n" % build_id())
    a.fn(a)


if __name__ == "__main__":
    main()
