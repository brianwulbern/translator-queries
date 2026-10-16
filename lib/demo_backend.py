"""Offline stand-ins for Crowdin, Jira and the model, used when TQ_DEMO is set.

TQ_DEMO points at a fixtures directory. Its crowdin.json and jira.json seed a
small world that is saved to TQ_DATA/demo-world.json, so a reply posted or a
Task filed in one command is still there for the next, exactly as with the
real services.

The drafter does not call a model. It writes the reply straight from the facts
keyfacts gathered, which shows how much of each answer is decided before any
prose is written: the model's job in a real run is only to say the same facts
well.
"""
import itertools, json, os, re, time, urllib.parse

import config

_WORLD = None
_COUNTER = itertools.count()


def _path():
    return os.path.join(config.DATA, "demo-world.json")


def _world():
    global _WORLD
    if _WORLD is None:
        if os.path.exists(_path()):
            _WORLD = json.load(open(_path()))
        else:
            fx = os.environ["TQ_DEMO"]
            _WORLD = {"crowdin": json.load(open(os.path.join(fx, "crowdin.json"))),
                      "jira": json.load(open(os.path.join(fx, "jira.json")))}
            _save()
    return _WORLD


def _save():
    os.makedirs(config.DATA, exist_ok=True)
    json.dump(_WORLD, open(_path(), "w"), indent=1)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _comment(c, strings):
    s = strings[c["stringId"]]
    return {"id": c["id"], "text": c["text"], "type": c["type"],
            "issueType": c.get("issueType"), "issueStatus": c.get("issueStatus"),
            "languageId": c.get("languageId"), "createdAt": c["createdAt"],
            "user": {"username": c["user"], "fullName": c["user"]},
            "string": {"id": s["id"], "context": s["context"], "text": s["text"],
                       "fileId": s["fileId"]}}


def crowdin(method, url, body=None):
    w = _world()["crowdin"]
    strings = {s["id"]: s for s in w["strings"]}
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query))
    path = re.sub(r"^.*?/api/v2", "", u.path)
    if path == "/user":
        return 200, {"data": {"username": w["me"]}}
    m = re.match(r"^/projects/(\d+)(/.*)?$", path)
    if not m:
        return 404, {"error": {"message": "not found"}}
    rest = m.group(2) or ""
    if rest == "" and method == "GET":
        return 200, {"data": w["project"]}
    if rest == "/comments" and method == "GET":
        rows = w["comments"]
        if q.get("type"):
            rows = [c for c in rows if c["type"] == q["type"]]
        if q.get("issueStatus"):
            rows = [c for c in rows if c.get("issueStatus") == q["issueStatus"]]
        if q.get("stringId"):
            rows = [c for c in rows if c["stringId"] == int(q["stringId"])]
        if int(q.get("offset", 0)):
            rows = []
        return 200, {"data": [{"data": _comment(c, strings)} for c in rows]}
    if rest == "/comments" and method == "POST":
        if body.get("stringId") not in strings:
            return 400, {"errors": [{"error": {"key": "stringId", "errors": [{"code": "notFound"}]}}]}
        c = {"id": 600 + len(w["comments"]), "stringId": body["stringId"], "type": "comment",
             "languageId": body.get("targetLanguageId"), "user": w["me"],
             "createdAt": _now(), "text": body["text"]}
        w["comments"].append(c)
        _save()
        return 201, {"data": _comment(c, strings)}
    m2 = re.match(r"^/comments/(\d+)$", rest)
    if m2 and method == "PATCH":
        for c in w["comments"]:
            if c["id"] == int(m2.group(1)):
                c["issueStatus"] = "resolved"
                _save()
                return 200, {"data": _comment(c, strings)}
        return 404, {"error": {"message": "no such comment"}}
    if rest == "/strings" and method == "GET":
        f = q.get("filter", "")
        rows = [s for s in w["strings"] if s["identifier"] == f]
        return 200, {"data": [{"data": s} for s in rows]}
    return 404, {"error": {"message": "not in the demo: %s %s" % (method, rest)}}


def _issue_json(key, i):
    done = i.get("status") == "Done"
    return {"key": key, "fields": {
        "summary": i.get("summary", ""),
        "status": {"name": i.get("status", "Open"),
                   "statusCategory": {"key": "done" if done else "new"}},
        "resolution": {"name": "Done"} if done else None,
        "labels": i.get("labels", []),
        "description": i.get("description"),
        "created": i.get("created", "2026-01-01T00:00:00"),
        "components": i.get("components", []),
        "issuetype": {"name": i.get("issuetype", "Task")},
        "parent": ({"key": i["parent"], "fields": {
            "summary": _world()["jira"]["issues"].get(i["parent"], {}).get("summary", "")}}
                   if i.get("parent") else None)}}


def jira(method, path, body=None):
    w = _world()["jira"]
    issues = w["issues"]
    if path == "myself":
        return 200, w["me"]
    if path.startswith("search/jql"):
        jql = urllib.parse.unquote(path)
        label = re.search(r"labels=([\w-]+)", jql)
        rows = [_issue_json(k, i) for k, i in issues.items()
                if label and label.group(1) in i.get("labels", [])]
        return 200, {"issues": rows, "isLast": True}
    if path == "issue" and method == "POST":
        f = body["fields"]
        key = "%s-%d" % (config.get("jiraProject") or "DEMO", 300 + len(issues))
        issues[key] = {"summary": f["summary"], "issuetype": "Task", "labels": f.get("labels", []),
                       "description": f.get("description"), "status": "Open",
                       "components": f.get("components", []), "created": _now()[:19]}
        _save()
        return 201, {"key": key}
    m = re.match(r"^issue/([A-Z]+-\d+)(\?.*)?$", path)
    if m and m.group(1) in issues:
        if method == "PUT":
            for op in (body.get("update") or {}).get("labels", []):
                if "add" in op and op["add"] not in issues[m.group(1)].setdefault("labels", []):
                    issues[m.group(1)]["labels"].append(op["add"])
            _save()
            return 204, {}
        return 200, _issue_json(m.group(1), issues[m.group(1)])
    return 404, {"errorMessages": ["Issue does not exist"]}


def jira_issue(key):
    """Anonymous read of a ticket, as links.py makes it."""
    st, d = jira("GET", "issue/%s" % key)
    return d if st == 200 else None


def finish(key):
    """Mark a filed Task done, as a product team eventually would."""
    issues = _world()["jira"]["issues"]
    issues[key]["status"] = "Done"
    _save()



def _words(expr):
    """task.senderName -> "the sender name"; selection.size -> a count."""
    if expr.startswith("a link showing"):
        return "a link to the item, showing its %s" % _words(expr.split()[-1])[4:]
    last = re.split(r"[.\[\]()]", expr.strip())
    last = [x for x in last if x and x != "length"] or [expr]
    name = last[-1]
    if name in ("size", "count") or expr.endswith(".length"):
        owner = re.sub(r"(?<!^)([A-Z])", r" \1", last[-2] if len(last) > 1 else "items").lower()
        return "a number, how many %s there are" % ("items" if owner in ("selection", "items") else owner)
    return "the " + re.sub(r"(?<!^)([A-Z])", r" \1", name).lower()


def draft(key, question, english, facts, temperature=None):
    lines = facts.splitlines()
    text = "\n".join(lines)
    out = []
    holes = re.findall(r"^\s+\{(\d)\} (<-|=) (.*)$", text, re.M)
    if holes:
        parts = []
        for n, how, what in holes:
            parts.append("{%s} is %s" % (n, _words(what) if how == "<-" else what.replace("`", "")))
        out.append("; ".join(parts) + ".")
    if "USED       NOWHERE" in text:
        out.append("This string is not used anywhere in the product right now, so we will "
                   "confirm with the product team whether it should be removed. Please hold "
                   "off on it for now.")
    elif "not in either repository YET" in text:
        subject = re.search(r"^ADDED .*?titled '(?:[A-Z]+-\d+ )?(.*)'$", text, re.M)
        sib = re.findall(r"^\s{13}[\w.-]+ = (.+)$", text, re.M)
        what = re.sub(r"^(?:add )?(?:language )?(?:keys|strings) for ", "",
                      subject.group(1), flags=re.I) if subject else "a new feature"
        out.append('It belongs to a screen that has not been released yet, %s, alongside '
                   'strings such as %s.'
                   % (what[0].lower() + what[1:], ", ".join('"%s"' % s.rstrip(".") for s in sib[:3])))
    if "PRIMARY" in text and not holes:
        code = text[text.find("CODE "):]
        code = code[:code.find("\nSURFACE")] if "\nSURFACE" in code else code
        labels = list(dict.fromkeys(re.findall(r"I18n\.get\('([\w.-]+)'\)", code)))
        sib = dict(re.findall(r"^\s{13}([\w.-]+) = (.+)$", text, re.M))
        others = [sib.get(k, k) for k in labels if k != key]
        if "options" in code and others:
            out.append('It is not a button: it is one of the options in the "%s" menu, next '
                       'to "%s".' % (others[0], '", "'.join(others[1:]) or others[0]))
    if "SURFACE    announced" in text:
        out.append("It is read aloud by a screen reader rather than shown on screen.")
    if not out:
        out.append("This could not be determined from the source.")
    if temperature and len(out) > 1:
        n = next(_COUNTER) % len(out)
        out = out[n:] + out[:n]
    reply = " ".join(out)
    return reply, {"input_tokens": len(facts) // 4, "output_tokens": len(reply) // 4,
                   "cost_usd": 0.0, "model": "demo-drafter", "via": "demo"}


def one_line(system, prompt):
    return "Clarify the English source string", {"input_tokens": len(prompt) // 4,
                                                  "output_tokens": 6, "cost_usd": 0.0}
