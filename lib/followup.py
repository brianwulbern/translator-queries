#!/usr/bin/env python3
"""File the follow-up a reply promises, so the promise has an owner.

A reply that says "we will raise this with the product team" is only true if
something makes it happen. This files a Jira Task routed to the team that owns
the code, then hands back the link to pass along by hand.

Routing uses the Jira components of the ticket the string was built under,
because that ticket already names the owning team. CODEOWNERS gives GitHub
users, not Jira components, so it goes in the description as the people to
look at, not as the routing.

The project may be publicly readable, so nothing identifying the translator or
the vendor goes in the ticket. It carries the string, the
question and where the code lives, all of which are public already.
"""
import json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

JIRA = config.get("jiraHost")
PROJECT = config.get("jiraProject")
TASK = config.get("jiraTaskTypeId")
# Promises the tool should notice, so the reviewer is prompted to file one.
PROMISE = re.compile(r"\b(we will|we'll|we are going to)\b[^.]*\b(raise|confirm|check|"
                     r"follow up|come back|look into|flag)\b|\bcome back to you\b", re.I)


def creds():
    if os.environ.get("TQ_DEMO"):
        return "demo", "demo"
    u = os.environ.get("JIRA_API_USER")
    t = os.environ.get("JIRA_API_TOKEN")
    return (u, t) if (u and t) else (None, None)


def jira(method, path, body=None):
    """One Jira REST call through curl.

    The credentials go to curl on stdin as a config line, never on the command
    line, where any user on the machine could read them from the process list.
    """
    if os.environ.get("TQ_DEMO"):
        import demo_backend
        return demo_backend.jira(method, path, body)
    u, t = creds()
    if not u and method != "GET":
        return 0, {"error": "no Jira credentials (JIRA_API_USER/JIRA_API_TOKEN)"}
    # A public project is readable anonymously, so a teammate without Jira credentials can
    # still see what has been filed, and so never files it a second time.
    cfg = ('user = "%s:%s"\n' % (u.replace('"', '\\"'), t.replace('"', '\\"'))) if u else ""
    # Options first, URL last, and the body added as whole option pairs. The
    # first version spliced the body in at a fixed index, which landed between
    # --max-time and its value: curl refused the command, sent nothing, and the
    # failure surfaced only as "HTTP 0".
    cmd = ["curl", "-sS", "-K", "-", "--max-time", "40", "-X", method,
           "-H", "Accept: application/json", "-w", "\n%{http_code}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body)]
    cmd.append("%s/rest/api/3/%s" % (JIRA, path))
    r = subprocess.run(cmd, input=cfg, capture_output=True, text=True)
    if r.returncode != 0:
        return 0, {"error": "curl exit %d: %s" % (r.returncode, (r.stderr or "").strip()[:200])}
    raw, _, code = r.stdout.rpartition("\n")
    try:
        return int(code), (json.loads(raw) if raw.strip() else {})
    except ValueError:
        return int(code or 0), {"raw": raw[:300]}


# Every Task tq files carries these, so Jira itself is the shared record of what
# has been filed. A local file could only ever know what one person filed.
LABEL = "tq-followup"
RELEASED = "tq-released"


def string_label(string_id):
    return "crowdin-%s" % string_id


def _key_from(desc):
    """The language key, read back from the "Key: " line build() writes."""
    for p in (desc or {}).get("content", []):
        runs = p.get("content") or []
        if runs and runs[0].get("text") == "Key: " and len(runs) > 1:
            return runs[1].get("text")
    return None


def find_all():
    """Every Task tq has filed, by language key. None when Jira cannot be read.

    One search, not one call per ticket, and the status comes back with it.
    """
    out, token = {}, None
    while True:
        path = ("search/jql?jql=%s&fields=status,resolution,labels,description,created"
                "&maxResults=100" % ("project%3D" + PROJECT + "%20AND%20labels%3D" + LABEL))
        if token:
            path += "&nextPageToken=" + token
        st, d = jira("GET", path)
        if st != 200:
            return None
        for i in d.get("issues", []):
            f = i.get("fields") or {}
            key = _key_from(f.get("description"))
            if not key:
                continue
            sid = next((l[len("crowdin-"):] for l in f.get("labels", [])
                        if l.startswith("crowdin-")), None)
            s_ = f.get("status") or {}
            entry = {"ticket": i["key"], "url": "%s/browse/%s" % (JIRA, i["key"]),
                     "at": (f.get("created") or "")[:19], "stringId": sid,
                     "status": s_.get("name"),
                     "category": (s_.get("statusCategory") or {}).get("key"),
                     "resolution": (f.get("resolution") or {}).get("name"),
                     "released": RELEASED in f.get("labels", [])}
            # Two Tasks for one key would be a duplicate; keep the older one.
            if key not in out or entry["at"] < out[key]["at"]:
                out[key] = entry
        token = d.get("nextPageToken")
        if d.get("isLast", True) or not token:
            return out


def add_label(ticket, label):
    st, d = jira("PUT", "issue/%s" % ticket, {"update": {"labels": [{"add": label}]}})
    return st == 204, d


def status(ticket):
    """(status name, category key, resolution) for a filed Task.

    The category is what matters: "done" covers Closed, Resolved, Won't Fix and
    any other status a workflow marks finished, so no status name is hardcoded.
    """
    st, d = jira("GET", "issue/%s?fields=status,resolution" % ticket)
    if st != 200:
        return None, None, None
    f = d.get("fields") or {}
    s_ = f.get("status") or {}
    return (s_.get("name"), (s_.get("statusCategory") or {}).get("key"),
            (f.get("resolution") or {}).get("name"))


def promises_followup(text):
    return bool(PROMISE.search(text or ""))


def components_for(g, links):
    """Components of the context story, else of the ticket in the commit subject."""
    L = links.get(g["key"], {})
    for tk in [L.get("ctxTicket"), L.get("ctxInternal"), L.get("_ticket")]:
        if not tk:
            continue
        st, d = jira("GET", "issue/%s?fields=components" % tk)
        comps = (d.get("fields") or {}).get("components") or [] if st == 200 else []
        if comps:
            return [{"id": c["id"], "name": c["name"]} for c in comps], tk
    return [], None


def call_site(key, sites=None):
    """The first real call site, for the description and the CODEOWNERS lookup."""
    import keyfacts
    for s in (keyfacts.call_sites(key) if sites is None else sites):
        path = s.split(":", 1)[0]
        return os.path.relpath(path, config.source_repo()), s.split(":", 2)[1]
    return None, None


def owners_for(rel):
    if not rel:
        return []
    try:
        import owners
        hit = owners.owner_of(rel)
        return hit[1] if hit else []
    except Exception:
        return []


def _p(*runs):
    """An ADF paragraph from (text, marks) runs."""
    content = []
    for text, mark in runs:
        node = {"type": "text", "text": text}
        if mark == "code":
            node["marks"] = [{"type": "code"}]
        elif mark and mark.startswith("http"):
            node["marks"] = [{"type": "link", "attrs": {"href": mark}}]
        content.append(node)
    return {"type": "paragraph", "content": content}


def build(g, links, reply_body, summary, sites=None, crowdin_url=None):
    comps, from_tk = components_for(g, links)
    rel, line = call_site(g["key"], sites)
    owners = owners_for(rel)
    L = links.get(g["key"], {})
    q = " ".join(" ".join((i.get("question") or "").split()) for i in g["issues"])
    q = re.sub(r"@[\w.-]+", "", q).strip()
    langs = ", ".join(sorted({i["lang"] for i in g["issues"]}))
    doc = [
        _p(("A translator working on the ", None), (langs, None),
           (" translation asked about this English string, and the answer depends on a "
            "change to the source rather than on the translation.", None)),
        _p(("Key: ", None), (g["key"], "code")),
        _p(("English: ", None), (g.get("english", ""), None)),
        _p(("Question: ", None), (q, None)),
        _p(("Reply given: ", None), (" ".join(reply_body.split()), None)),
    ]
    if crowdin_url:
        doc.insert(2, _p(("Crowdin string: ", None), (crowdin_url, crowdin_url)))
    if rel:
        doc.append(_p(("Code: ", None), ("%s:%s" % (rel, line), "code")))
    if owners:
        doc.append(_p(("Code owners per CODEOWNERS: ", None), (", ".join(owners), None)))
    if L.get("commitUrl"):
        doc.append(_p(("Key added in: ", None), (L["commitUrl"], L["commitUrl"])))
    if from_tk:
        doc.append(_p(("Routed by the components of ", None),
                      (from_tk, "%s/browse/%s" % (JIRA, from_tk)),
                      (", the ticket the string was built under.", None)))
    fields = {"project": {"key": PROJECT}, "issuetype": {"id": TASK},
              "summary": summary.replace("—", ",")[:250],
              "labels": [LABEL, string_label(g["stringId"])],
              "components": [{"id": c["id"]} for c in comps],
              "description": {"type": "doc", "version": 1,
                              "content": [_strip_dash(p) for p in doc]}}
    return fields, comps, owners, rel


def _strip_dash(node):
    """No em-dashes in published text; rewrite any that arrive in quoted input."""
    for c in node.get("content", []):
        if "text" in c:
            c["text"] = c["text"].replace(" — ", ", ").replace("—", ", ")
    return node


# Translators often quote the mark itself rather than naming it: 'Is "," missing
# before "or select"?' asks about a comma without ever saying the word.
WORDING_Q = re.compile(
    r"typo|misspel|spelling|comma|punctuat|grammar|\bmissing\b|"
    r"should (it|this) be|capitali[sz]|hyphen|plural|singular|ambigu|"
    r"modif(y|ies)|"
    r"[\"'“‘][,;:.!?-][\"'”’]",
    re.I)
SUMMARY_SYSTEM = ("You write one Jira ticket summary line for a product team. Imperative "
                  "mood, at most 90 characters, plain words, no em-dash, no quotes around "
                  "the whole line, nothing else. It names the change to the English source "
                  "string that is needed. The reply you are given has already been checked "
                  "against the code and is correct: your summary must agree with it and must "
                  "never propose anything the reply says is unnecessary or wrong. If the reply "
                  "says the English is correct as written, summarise the clarity problem "
                  "instead of proposing an edit it rules out.")


def _quote(en, n=60):
    en = " ".join((en or "").split())
    return en if len(en) <= n else en[:n].rsplit(" ", 1)[0] + "..."


def classify(g, sites=None):
    """What is wrong with the English, decided from facts rather than guessed.

    Returns (kind, detail). Order matters: an unused key has no wording worth
    fixing, and a placeholder mismatch is a bug whatever else is asked.
    """
    import keyfacts
    key, en = g["key"], g.get("english") or ""
    q = " ".join((i.get("question") or "") for i in g["issues"])
    sites = keyfacts.call_sites(key) if sites is None else sites
    if not sites:
        closed = keyfacts.closed_sites(key)
        if not closed and keyfacts.built_at_runtime(key)[0] is None:
            return "unused", None
    holes = set(re.findall(r"\{\d\}", en))
    if holes and sites:
        path = sites[0].split(":", 1)[0]
        for args in keyfacts.arguments_at(path, key):
            if len(args) != len(holes):
                return "placeholders", (len(holes), len(args))
    labels = keyfacts.embedded_labels(key, en)
    if labels and WORDING_Q.search(q):
        return "label", [v for _, v in labels[:2]]
    if WORDING_Q.search(q):
        return "wording", None
    return "other", None


def default_summary(g, sites=None):
    """A summary that names the fix, so a triager knows the ask from the title.

    Known kinds are templated, which is exact and free. Anything else asks the
    model for one line, since the reviewer will read it and can replace it.
    Returns (summary, kind).
    """
    kind, detail = classify(g, sites)
    q = _quote(g.get("english"))
    if kind == "unused":
        return ('Confirm whether the "%s" language key is still used, and remove it if not'
                % q, kind)
    if kind == "placeholders":
        return ('Fix "%s": the English has %d placeholder(s) but the code passes %d'
                % (_quote(g.get("english"), 44), detail[0], detail[1]), kind)
    if kind == "label":
        return ('Make "%s" read as a UI name in "%s"' % (detail[0], _quote(g.get("english"), 44)),
                kind)
    if kind == "wording":
        return ('Fix the wording of "%s" so it reads unambiguously' % q, kind)
    try:
        import draft
        question = " ".join((i.get("question") or "") for i in g["issues"])
        line, _ = draft.one_line(SUMMARY_SYSTEM,
                                 "English string: %s\nTranslator asked: %s\n"
                                 "Our reply, which is correct: %s"
                                 % (g.get("english"), question, g.get("body") or ""))
        if line:
            return line.replace("\u2014", ",").strip(' "')[:120], "model"
    except Exception:
        pass
    return 'Clarify the English string "%s" for translators' % q, kind


def create(fields):
    st, me = jira("GET", "myself")
    if st != 200:
        return None, "could not identify the reporter (HTTP %s)" % st
    fields = dict(fields, reporter={"id": me["accountId"]})
    st, d = jira("POST", "issue", {"fields": fields})
    if st not in (200, 201):
        return None, "Jira refused it (HTTP %s): %s" % (st, json.dumps(d)[:300])
    return d["key"], "%s/browse/%s" % (JIRA, d["key"])
