#!/usr/bin/env python3
"""Mechanically collect the facts a translator's question usually turns on.

Almost every question is one of: what replaces {0}, is this seen or only heard,
and is this string even used. Those are answerable by reading the source, not by
judgement, so they belong here rather than in a person's head. What is left over
is the prose, which is the only part worth spending a model on.
"""
import json, os, re, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

ROOT = config.ROOT
REPO = config.source_repo()
LANG = config.get("languageFile")
# An optional second checkout, for products whose strings are public but whose
# code that uses some of them lives in a separate, private repository.
CLOSED = config.get("extraRepo") or ""
CLOSED_REF = config.get("extraRef") or "HEAD"

# Anything reached through these is announced, never drawn.
SPOKEN = re.compile(r"announce\s*\(|onAnnounce\s*\(|aria-label|aria-live|"
                    r'role=["\']status|AriaLabel', re.I)


def sh(*a, **kw):
    return subprocess.run(a, capture_output=True, text=True, **kw).stdout


RG = shutil.which("rg")


def search(literals, root, files_only=False):
    """Fixed-string search of a whole checkout, with ripgrep when it is there.

    grep had to read every file to prove a key unused, which on this checkout
    took long enough that [f] sat silent for over a minute. ripgrep does the
    same scan in about seven seconds.
    """
    excl_dirs = ["build", "node_modules", ".git"]
    if RG:
        cmd = [RG, "--no-messages", "-F", "--no-heading"]
        cmd += ["-l"] if files_only else ["-n"]
        for d in excl_dirs:
            cmd += ["-g", "!**/%s/**" % d]
        cmd += ["-g", "!Language*.properties"]
        for lit in literals:
            cmd += ["-e", lit]
        return sh(*(cmd + [root]))
    cmd = ["grep", "-r", "--binary-files=without-match",
           "--exclude=Language*.properties"] + ["--exclude-dir=%s" % d for d in excl_dirs]
    cmd += ["-l"] if files_only else ["-n"]
    for lit in literals:
        cmd += ["-F", "-e", lit]
    return sh(*(cmd + [root]))


def english(key):
    for ln in open(os.path.join(REPO, LANG), encoding="utf-8", errors="replace"):
        if ln.startswith(key + "="):
            return ln.split("=", 1)[1].rstrip("\n")
    return ""


def call_sites(key):
    """Every reference to the key outside the language files and build output."""
    out = search(['"%s"' % key, "'%s'" % key], REPO)
    # Tests pass mocks and matchers, not real arguments, so they describe the
    # shape of the call without saying what a translator would ever see.
    return [l for l in out.splitlines()
            if l.strip() and "/src/test/" not in l and "/src/testIntegration/" not in l]


def closed_sites(key):
    if not CLOSED or not os.path.isdir(os.path.join(CLOSED, ".git")):
        return None
    out = sh("git", "-C", CLOSED, "grep", "-n", "-F", key, CLOSED_REF)
    return [l for l in out.splitlines() if l.strip()]


def split_args(text):
    """Top-level comma split, ignoring commas inside nested parens or quotes."""
    parts, depth, cur, quote = [], 0, "", None
    for ch in text:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote, cur = ch, cur + ch
        elif ch in "([{":
            depth += 1; cur += ch
        elif ch in ")]}":
            depth -= 1; cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _jsx(arg):
    """A JSX element as what it shows: <a ...>{x.title}</a> is a link showing x.title."""
    m = re.match(r"^<(\w+)\b[^>]*>\s*(.*?)\s*</\1>$", arg, re.S)
    if not m:
        return arg
    what = {"a": "a link", "b": "bold text", "strong": "bold text",
            "code": "code text"}.get(m.group(1), "a <%s> element" % m.group(1))
    return "%s showing %s" % (what, m.group(2).strip("{} "))


def arguments_at(path, key, window=14):
    """What the code substitutes into the placeholders, read from the call.

    The key literal often sits on its own continuation line because of the 80
    column wrap, so the arguments are found by reading a window around it and
    balancing parentheses rather than by matching the one line that grepped.
    """
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except IOError:
        return []
    found = []
    # Quoted, whole: 'x-files' is also the tail of 'search-to-find-any-of-the-x-files'.
    for m in re.finditer(r"""(?<=['"])%s(?=['"])""" % re.escape(key), src):
        head = src.rfind("sub(", max(0, m.start() - 600), m.start())
        if head < 0:
            head = src.rfind("format(", max(0, m.start() - 600), m.start())
        if head < 0:
            continue
        open_paren = src.index("(", head)
        depth, i = 0, open_paren
        while i < len(src) and i < open_paren + 2000:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        # The call found by searching backwards must actually contain the key.
        # Otherwise it is a neighbouring message's call, and its arguments would
        # be reported against a string they have nothing to do with.
        if not (open_paren < m.start() < i):
            continue
        inner = src[open_paren + 1:i]
        args = split_args(inner)
        # drop the leading pattern argument, and Java's leading request/locale
        args = [a for a in args if not re.search(r"""['"]%s['"]""" % re.escape(key), a)]
        args = [a for a in args
                if not re.match(r"^(request|httpServletRequest|resourceBundle|locale|"
                                r"pageContext|acceptLanguage)", a)]
        args = [" ".join(a.split()) for a in args]
        # Java passes several values as one array literal; unpack so each shows
        # against the placeholder it actually fills.
        if len(args) == 1:
            arr = (re.match(r"^new String\[\]\s*\{(.*)\}$", args[0])
                   or re.match(r"^\[(.*?),?\s*\]$", args[0]))
            if arr:
                args = [" ".join(x.split()) for x in split_args(arr.group(1))]
        args = [_jsx(a) for a in args]
        if args:
            found.append(args)
    return found


def spoken_at(site, window=8):
    """Decide seen-or-heard from the lines AROUND the match, not the match.

    The grep line is the wrong evidence. Keys wrap onto their own continuation
    line, and a live region is declared on the enclosing element, so announce()
    or role="status" usually sits several lines away. Reading only the matched
    line got this exactly backwards during manual research.
    """
    try:
        path, num = site.split(":", 2)[0], int(site.split(":", 2)[1])
    except (ValueError, IndexError):
        return False
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except IOError:
        return False
    lo, hi = max(0, num - 1 - window), min(len(lines), num + window)
    return bool(SPOKEN.search("\n".join(lines[lo:hi])))


def built_at_runtime(key):
    """Find the literal prefix a key is assembled from, if it is assembled.

    A key concatenated in code has no literal to grep, so a plain search reports
    it as unused. Saying a live string is dead is the worst answer this tool can
    give a translator, so before calling anything an orphan, look for the stem.
    Returns (prefix, where) or (None, None).
    """
    parts = key.split("-")
    for cut in range(len(parts) - 1, 1, -1):
        stem = "-".join(parts[:cut]) + "-"
        if len(stem) < 10:
            break
        hit = search(['"%s"' % stem], REPO, files_only=True).splitlines()
        if not hit and CLOSED and os.path.isdir(os.path.join(CLOSED, ".git")):
            hit = sh("git", "-C", CLOSED, "grep", "-l", "-F", '"%s"' % stem,
                     CLOSED_REF).splitlines()
        if hit:
            return stem, hit[0]
    return None, None


_LABELS = None
# Labels built only from these ("Is Not", "Or Else", "At Least") are filter
# operators and the like. Inside a sentence they are grammar, not a name.
_GLUE = {"a", "an", "and", "any", "are", "as", "at", "be", "by", "do", "does",
         "else", "for", "from", "has", "have", "if", "in", "is", "it", "least",
         "less", "more", "no", "not", "of", "on", "or", "than", "that", "the",
         "then", "this", "to", "was", "with", "yes"}


def embedded_labels(key, en):
    """Phrases inside this string that are themselves UI labels elsewhere.

    A string can name a button, a field type or a menu item in running prose,
    lowercased, where it reads as ordinary words. "select related content" is a
    field type called Select Related Content, but in a sentence "select" reads
    as a verb, and a model given no other fact will parse it that way and explain
    the misreading with confidence. Matching against every other label finds
    those names mechanically.
    """
    global _LABELS
    if _LABELS is None:
        _LABELS = {}
        for ln in open(os.path.join(REPO, LANG), encoding="utf-8", errors="replace"):
            if "=" not in ln or ln.startswith("#"):
                continue
            k, v = ln.rstrip("\n").split("=", 1)
            v = v.strip()
            # Short title-cased labels only: two to five words, no placeholders.
            words = v.split()
            if 2 <= len(words) <= 5 and "{" not in v and v[:1].isupper() \
                    and not v.endswith((".", "?", ":")) \
                    and not all(w.lower() in _GLUE for w in words):
                _LABELS.setdefault(v.lower(), (k, v))
    low = " %s " % re.sub(r"[^a-z0-9 ]", " ", en.lower())
    hits = []
    for phrase, (k, v) in _LABELS.items():
        if k == key:
            continue
        if " %s " % phrase in low:
            hits.append((k, v))
    # Longest first, and drop any label wholly contained in a longer one found.
    hits.sort(key=lambda kv: -len(kv[1]))
    kept = []
    for k, v in hits:
        if not any(v.lower() in kv.lower() for _, kv in kept):
            kept.append((k, v))
    return kept



# Called as bare functions all the time, and never worth following.
_BUILTINS = {"sub", "Boolean", "Number", "String", "parseInt", "parseFloat",
             "Array", "Object", "Date", "Math", "JSON", "Set", "Map", "Error",
             "require", "import", "if", "for", "while", "switch", "return",
             "catch", "function", "typeof", "new", "super", "useState",
             "useEffect", "useMemo", "useCallback", "useRef", "format", "get",
             "escapeHTML", "announce", "onAnnounce", "dispatch", "isArray"}
_CONTEXT_BUDGET = 70   # lines of source handed to the model, in total


def module_root(path):
    """The directory above src/, so a helper is looked for where it is used."""
    parts = path.split(os.sep)
    if "src" in parts:
        return os.sep.join(parts[:parts.index("src")])
    return os.path.dirname(path)


def find_definition(name, near):
    """Where a helper is defined, preferring the module that calls it."""
    pat = (r"(function\s+%s\s*[(<]|(const|let)\s+%s\s*=|"
           r"(public|private|protected|static)[^=;(]*\s%s\s*\()") % (name, name, name)
    for where in (module_root(near), REPO):
        out = sh("grep", "-rnE", "--include=*.ts", "--include=*.tsx", "--include=*.js",
                 "--include=*.java", "--exclude-dir=build", "--exclude-dir=node_modules",
                 "--exclude-dir=test", "--exclude-dir=testIntegration", pat, where)
        for line in out.splitlines():
            if "/test/" in line or ".spec." in line or ".test." in line:
                continue
            f, n = line.split(":", 2)[:2]
            return f, int(n)
    return None, None


def body_of(path, line, cap=30):
    """A definition from its first line to its closing brace, capped."""
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except IOError:
        return []
    out, depth, opened = [], 0, False
    for ln in lines[line - 1:line - 1 + 200]:
        out.append(ln)
        depth += ln.count("{") - ln.count("}")
        opened = opened or "{" in ln
        if opened and depth <= 0:
            break
        # a one-line arrow function has no braces to balance
        if not opened and ln.rstrip().endswith(";"):
            break
    return out[:cap]


def resolve_labels(text):
    """Replace Language.get('k') with the English it produces, for readability."""
    def rep(m):
        v = english(m.group(2))
        return '"%s"' % v if v else m.group(0)
    return re.sub(r"""(I18n\.get|LanguageUtil\.get)\(\s*(?:[^,()]*,\s*)?['"]([a-z0-9.\-]+)['"]\s*\)""",
                  rep, text)


def called_helpers(text):
    """Bare identifiers called as functions, in order, without repeats."""
    seen, out = set(), []
    for m in re.finditer(r"(?<![\w.$])([a-z_$][\w$]*)\s*\(", text):
        n = m.group(1)
        if n in _BUILTINS or n in seen or len(n) < 4:
            continue
        seen.add(n); out.append(n)
    return out


def follow(names, near, depth, budget, shown):
    """Show each helper's definition, then the helpers it calls, to `depth`."""
    for name in names:
        if budget[0] <= 0 or name in shown:
            continue
        f, n = find_definition(name, near)
        if not f:
            continue
        shown.add(name)
        body = body_of(f, n)
        body = body[:max(0, budget[0])]
        budget[0] -= len(body) + 1
        print("HELPER     %s  (%s:%d)" % (name, os.path.relpath(f, REPO), n))
        for ln in body:
            print("           | " + resolve_labels(ln.rstrip())[:100])
        if depth > 1:
            follow(called_helpers("\n".join(body[1:])), f, depth - 1, budget, shown)


def selecting_condition(path, num, window=6):
    """For a message returned under `if (x === 'lit')`, the literal that picks it."""
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except IOError:
        return None
    chunk = "\n".join(lines[max(0, num - 1 - window):num])
    m = re.findall(r"""===\s*['"]([\w-]+)['"]""", chunk)
    return m[-1] if m else None


def trace_condition(lit, path, budget, shown):
    """Where the selecting value is produced, and what decides it."""
    out = sh("grep", "-rn", "--include=*.ts", "--include=*.tsx", "--include=*.js",
             "--exclude-dir=build", "--exclude-dir=node_modules",
             "'%s'" % lit, module_root(path))
    setters = [l for l in out.splitlines()
               if "===" not in l and "| '%s'" % lit not in l and "/test" not in l
               and ".spec." not in l]
    for line in setters[:2]:
        f, n = line.split(":", 2)[:2]
        n = int(n)
        if ("decided", f, n) in shown:
            continue
        shown.add(("decided", f, n))
        try:
            src = open(f, encoding="utf-8", errors="replace").read().splitlines()
        except IOError:
            continue
        seg = src[max(0, n - 6):n + 1]
        budget[0] -= len(seg) + 1
        print("DECIDED    '%s' is set at %s:%d" % (lit, os.path.relpath(f, REPO), n))
        for ln in seg:
            print("           | " + ln.rstrip()[:100])
        follow(called_helpers("\n".join(seg)), f, 2, budget, shown)



_RENDER_NOTE = re.compile(r"`<code>\{argument\}</code>`")


def recorded_arguments(key):
    """Placeholder values written down in reference/, for keys whose arguments
    are filled outside the product repository and so cannot be read from it.

    A table row names a key by its suffix (`...-missing-h1-tag`), and a key's
    -description and -fix-hint siblings share the row. Returns
    (file, ["{0} = `<h1>`", ...], rendering note or None), or None.
    """
    ref = os.path.join(ROOT, "reference")
    if not os.path.isdir(ref):
        return None
    base = re.sub(r"-(description|fix-hint)$", "", key)
    for name in sorted(os.listdir(ref)):
        if not name.endswith(".md"):
            continue
        text = open(os.path.join(ref, name)).read()
        for line in text.splitlines():
            m = re.match(r"\|\s*`(\.\.\.-)?([\w.-]+)`.*?\|\s*(.+?)\s*\|\s*$", line)
            if m and (base.endswith("-" + m.group(2)) if m.group(1) else base == m.group(2)):
                # "`{0}` = `<h1>`, `{1}` = `<title>`" -> ["{0} = `<h1>`", "{1} = `<title>`"]
                cells = [c.strip().replace("`{", "{", 1).replace("}`", "}", 1)
                         for c in re.split(r",\s*(?=`\{)", m.group(3))]
                note = ("each argument is shown inside a code element, as the literal "
                        "characters, never as markup; it stays in English"
                        if _RENDER_NOTE.search(text) else None)
                return "reference/" + name, cells, note
    return None


def introduced(key):
    """(subject, date, [(sibling key, English)], {files touched}) for the commit
    that added the key, or None.

    The commit that adds a key nearly always adds its first use and its sibling
    keys too, and its subject says what the feature is. That is the context a
    key used in forty places, or in none yet, otherwise lacks.
    """
    import links
    intro = links.introducing_commit(key)
    m = re.match(r"([0-9a-f]+)\|([\d-]+)\|(.+?)\|(.+)", intro or "")
    if not m:
        return None
    sha, date, _author, subject = m.groups()
    subject = re.sub(r"\s*\[.*$", "", subject)
    diff = links.git("show", "--format=", "-U0", sha, "--", links.LANG_REL)
    siblings = []
    for line in diff.splitlines():
        mm = re.match(r"\+([^\s=#]+)=(.*)", line)
        if mm and mm.group(1) != key:
            siblings.append((mm.group(1), mm.group(2)))
    files = set(links.git("show", "--format=", "--name-only", sha).splitlines())
    return subject, date, siblings, files


def code_window(path, num, before=16, after=8, cap=20):
    """The lines around a use, without blanks, short enough to read in a glance."""
    try:
        lines = open(path, errors="replace").read().splitlines()
    except OSError:
        return []
    out = []
    for i in range(max(0, num - 1 - before), min(len(lines), num + after)):
        t = lines[i].strip()
        if re.search(r"[A-Za-z]", t):
            out.append(("> " if i == num - 1 else "  ") + t[:100])
    return out[-cap:] if len(out) > cap else out


def report(key):
    en = english(key)
    holes = sorted(set(re.findall(r"\{\d\}", en)))
    sites = call_sites(key)
    closed = closed_sites(key)
    intro = introduced(key)
    recent = bool(intro) and intro[1] >= time.strftime(
        "%Y-%m-%d", time.localtime(time.time() - 120 * 86400))
    if intro and sites:
        # The commit's own use first: with a common word like "clean" the other
        # matches are unrelated, and they used to be all the reader saw.
        def rank(s_):
            rel = os.path.relpath(s_.split(":", 1)[0], REPO)
            body = s_.split(":", 2)[-1]
            return (rel not in intro[3], "/test/" in rel or ".test." in rel,
                    not re.search(r"I18n|ui:message|LanguageUtil|\bt\(", body))
        sites = sorted(sites, key=rank)
        mine = [s_ for s_ in sites if not any(rank(s_)[:2])]
        intro = intro + (bool(mine),)
    elif intro:
        intro = intro + (False,)
    spoken = [s for s in sites if spoken_at(s)]

    print("=" * 74)
    print("KEY        %s" % key)
    print("EN         %s" % (en or "(not in the language file)"))
    print("HOLES      %s" % (", ".join(holes) if holes else "none"))
    for k, v in embedded_labels(key, en)[:4]:
        print("LABEL      %r in this string is also a UI label (%s)," % (v.lower(), k))
        print("           so it names something rather than being ordinary words")

    if not sites and not closed:
        stem, where = built_at_runtime(key)
        if stem:
            print("USED       no literal reference, but the stem %r appears in" % stem)
            print("           %s" % where.split(":")[-1][:88])
            print("           The key is most likely assembled at runtime. NOT an orphan.")
        elif recent:
            print("USED       not in either repository YET: the change below added it, "
                  "and the screen that uses it has not landed")
        else:
            print("USED       NOWHERE in either repository -> orphan, hold rather than answer")
    else:
        print("USED       %d site(s)%s" % (len(sites), "" if closed is None else
                                             ", %d in the second repository" % len(closed)))

    if intro:
        subject, date, siblings, _files, own = intro
        print("ADDED      %s, in a change titled %r" % (date, subject[:90]))
        if siblings:
            print("WITH       %d other string(s) added in the same change:" % len(siblings))
            for k_, v_ in siblings[:8]:
                print("             %s = %s" % (k_[:48], v_[:60]))
        if own:
            print("PRIMARY    the first use below was added by that same change; "
                  "prefer it over the others")
        if not sites and not closed and recent:
            print("NOTE       describe it from the change and its sibling strings; "
                  "it is not a leftover")
    if sites:
        try:
            p0, n0 = sites[0].split(":", 2)[:2]
            win = code_window(p0, int(n0))
        except ValueError:
            win = []
        if win:
            print("CODE       %s:%s" % (os.path.relpath(p0, REPO), n0))
            for w in win:
                print("             " + w)

    if spoken:
        print("SURFACE    announced, not drawn (screen reader only)")
    elif sites:
        print("SURFACE    visible text (no announce or aria-label at the call)")

    seen = set()
    for s in sites[:6]:
        path = s.split(":", 1)[0]
        rel = os.path.relpath(path, REPO)
        for args in arguments_at(path, key):
            sig = tuple(args)
            if sig in seen:
                continue
            seen.add(sig)
            print("ARGS       %s" % rel)
            for n, a in enumerate(args):
                print("             {%d} <- %s" % (n, a[:88]))
    known = recorded_arguments(key) if holes and not seen else None
    if known:
        src, cells, note = known
        print("ARGS       recorded in %s (the filling code is not in this repo)" % src)
        for c in cells:
            print("             %s" % c)
        if note:
            print("RENDER     %s" % note)
    elif holes and not seen:
        print("ARGS       not extracted automatically, read the call site by hand")

    # Follow what the call site leans on, two levels down, within a budget, so
    # the deciding fact reaches the reader even when it is not in the sentence.
    budget, shown = [_CONTEXT_BUDGET], set()
    for s_ in sites[:3]:
        path = s_.split(":", 1)[0]
        try:
            num = int(s_.split(":", 2)[1])
        except (ValueError, IndexError):
            continue
        for args in arguments_at(path, key):
            follow(called_helpers(" ".join(args)), path, 2, budget, shown)
        lit = selecting_condition(path, num)
        if lit:
            trace_condition(lit, path, budget, shown)
    if closed:
        for c in closed[:3]:
            print("EXTRA      %s" % c.split(":", 2)[1][:88])

    n_args = max([len(a) for a in seen], default=0)
    if holes and n_args and n_args != len(holes):
        print("MISMATCH   English uses %d placeholder(s), the call passes %d"
              % (len(holes), n_args))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: keyfacts.py <language-key> [...]")
    for k in sys.argv[1:]:
        report(k)
