#!/usr/bin/env python3
"""Draft one answer from facts already gathered, and record what it cost.

The facts come from keyfacts, which reads them out of the source. The model is
given those facts and the translator's question and asked only to write the
prose, because that is the part that cannot be looked up. Usage comes back on
every response, so each answer can say what it cost to produce.
"""
import io, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, keyfacts

API = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"

SETUP = """
Drafting needs a way to reach a model. Either works:

  The claude CLI (no key needed). If you use Claude Code, you already have it,
  and tq drafts on the same login. Check with: claude --version

  An Anthropic API key, billed per token to an API account:
    1. Create one at https://console.anthropic.com under API Keys.
    2. export ANTHROPIC_API_KEY=...
       tq sources your credentials file and never prints the value.

  With both present, the API key is used.

Drafting is the only part of tq that spends money. Everything else, including
tq facts, is local and free.
"""

STYLE = """You write short answers to professional translators who have asked a \
question about one English string in a software product, on Crowdin.

Your reader is a linguist, not an engineer. They are deciding how to translate a \
string and need exactly enough context to choose their words.

Rules, all of them firm:

- Two or three sentences. Never four. They are answering dozens of these.
- Answer the question that was asked, first, in the first sentence.
- When a placeholder is involved, say the exact value the code substitutes. Not \
"a variable" but the literal thing, quoted.
- If the string is announced by a screen reader rather than displayed, say so, \
because it changes how it should read.
- If the placeholder arrives already translated, say so, because agreement in \
the target language depends on it.
- Plain prose. No bullet points, no headings, no markdown.
- Never use an em-dash. Use a comma, a colon, or a full stop.
- American spelling.
- Open with "Actually," ONLY when the translator's question contains a wrong \
assumption you are correcting. Never on a neutral question.
- Do not greet, do not sign off, do not at-mention anyone. That is added \
separately.
- Do not invent. If the facts given do not settle something, say plainly that it \
could not be determined.
- If the translator asks for a term to be added to or changed in the glossary, \
we maintain the glossary and the change is made before your reply is sent: \
say it has been added (or corrected) and how it is marked, for example as do \
not translate. Never send them to anyone else for it.
- No engineering vocabulary. Never write "orphaned key", "codebase", "call site", \
"repository", "runtime" or "language key". Say what a linguist would: the string \
is not shown anywhere in the product.
"""

HOLD_RULE = """

This reply is INTERIM. Whether the string is still in use, or what surrounds it, \
has not been confirmed yet, and the product team is being asked. So the reply \
must ask the translator to hold off on this string for now, and must say that we \
will come back to them once it is confirmed. Do not present the question as \
settled.
"""


def backend():
    """Which way to reach a model, in order of preference.

    An API key, if one is set, because it bills per token and supports a
    temperature. Otherwise the claude CLI, which runs on the same login as a
    Claude Code session, so a user who already drafts answers in conversation
    needs nothing new.
    """
    if os.environ.get("TQ_DEMO"):
        return "demo"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if shutil.which("claude"):
        return "cli"
    return None


def api_key():
    k = os.environ.get("ANTHROPIC_API_KEY")
    if not k:
        sys.exit("tq: ANTHROPIC_API_KEY is not set.\n%s" % SETUP)
    return k


def facts_for(key):
    """keyfacts prints a report; capture it as the model's evidence."""
    buf = io.StringIO()
    stdout, sys.stdout = sys.stdout, buf
    try:
        keyfacts.report(key)
    finally:
        sys.stdout = stdout
    return buf.getvalue()


def ask_variations(key, question, english, n=3, model=None, timeout=180, hold=False):
    """Three independent answers, one call each, run at the same time.

    Asking one call for three costs less, but the second and third are written in
    the shadow of the first, so they drift toward paraphrase. Separate calls are
    genuinely independent samples, which is the point of asking for a choice at
    all. That costs the facts n times over, so the price of the whole fanout is
    reported rather than buried.

    Returns (variants, usage) where usage is the SUM across every call made,
    including any that came back empty, because those were paid for too.
    """
    if backend() is None:  # fail once, here, rather than inside each thread
        sys.exit("tq: no way to reach a model.\n%s" % SETUP)
    # Gather the facts ONCE, before any thread starts. They are identical for
    # every call, and gathering them captures stdout by swapping sys.stdout,
    # which is process-wide: three threads doing it at once raced, leaked
    # fragments to the terminal, and left stdout pointing at another thread's
    # buffer so the rest of the output vanished.
    facts = facts_for(key)
    results = [None] * n
    usages = [None] * n

    def one(i):
        try:
            results[i], usages[i] = ask(key, question, english, model=model,
                                        timeout=timeout, temperature=1.0,
                                        facts=facts, extra=HOLD_RULE if hold else "")
        except BaseException as e:            # a thread must not take the run down
            results[i], usages[i] = None, {"error": repr(e)[:120]}

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 30)

    total = {"model": model or os.environ.get("TQ_MODEL") or DEFAULT_MODEL,
             "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
             "calls": 0, "failed": 0, "via": backend(),
             "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "per_call": []}
    variants = []
    for i in range(n):
        u = usages[i] or {"error": "no response"}
        total["calls"] += 1
        if u.get("error") or not results[i]:
            total["failed"] += 1
            total["per_call"].append({"error": u.get("error", "empty")})
            continue
        total["input_tokens"] += u.get("input_tokens", 0)
        total["output_tokens"] += u.get("output_tokens", 0)
        total["cost_usd"] += u.get("cost_usd", 0.0)
        total["model"] = u.get("model", total["model"])
        total["per_call"].append({"input_tokens": u.get("input_tokens", 0),
                                  "output_tokens": u.get("output_tokens", 0),
                                  "cost_usd": u.get("cost_usd", 0.0)})
        variants.append(results[i].strip())

    # Identical samples are a real outcome worth seeing, not a bug to hide.
    seen, deduped = set(), []
    for v in variants:
        if v in seen:
            continue
        seen.add(v); deduped.append(v)
    total["duplicates"] = len(variants) - len(deduped)
    return deduped, total


def build_prompt(key, question, english, facts=None):
    return ("Language key: %s\nEnglish string: %s\n\n"
            "What the source says about it:\n%s\n"
            "The translator asked:\n%s\n\n"
            "Write the answer. Prose only, nothing else."
            % (key, english, facts if facts is not None else facts_for(key), question))


def ask_cli(key, question, english, model=None, timeout=180, facts=None, extra=""):
    """Draft through `claude -p`, on the user's own Claude Code login.

    Left to its defaults, a headless call carries Claude Code's whole harness,
    its system prompt and every tool definition, which measured about 20K input
    tokens to answer one word. Replacing the system prompt with ours, passing no
    tools and loading no settings brings that to about 470. --bare would go
    further but skips the keychain read, so it cannot log in.

    It runs from a scratch directory so a project CLAUDE.md is not read in as
    context, and the prompt goes over stdin so its length is not bounded by the
    argument list.
    """
    model = model or os.environ.get("TQ_MODEL") or DEFAULT_MODEL
    # --setting-sources "" is the biggest single saving: it stops the user's own
    # settings, and the global CLAUDE.md they pull in, riding along on every
    # call. Measured on a one-word reply, 2932 input tokens became 470. Nothing
    # is lost, since the rules that matter here are already in STYLE.
    cmd = ["claude", "-p", "--output-format", "json", "--no-session-persistence",
           "--system-prompt", STYLE + extra, "--tools", "", "--setting-sources", "",
           "--model", model]
    try:
        r = subprocess.run(cmd, input=build_prompt(key, question, english, facts),
                           capture_output=True, text=True, timeout=timeout,
                           cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired:
        return None, {"error": "claude CLI timed out after %ds" % timeout}
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return None, {"error": "claude CLI said: %s" % (r.stdout or r.stderr)[:200]}
    if d.get("is_error"):
        return None, {"error": (d.get("result") or "claude CLI error")[:200]}
    u = d.get("usage") or {}
    # Cached input is still input: the model read it. Count all three kinds.
    tin = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
           + u.get("cache_read_input_tokens", 0))
    used = list((d.get("modelUsage") or {}).keys())
    return (d.get("result") or "").strip(), {
        "model": used[0] if used else model,
        "input_tokens": tin, "output_tokens": u.get("output_tokens", 0),
        "cost_usd": d.get("total_cost_usd") or 0.0, "via": "cli",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def ask(key, question, english, model=None, timeout=120, extra="",
        max_tokens=600, temperature=None, facts=None):
    """Return (text, usage). usage carries the real token counts."""
    if backend() == "demo":
        import demo_backend
        return demo_backend.draft(key, question, english,
                                  facts if facts is not None else facts_for(key), temperature)
    if backend() == "cli":
        return ask_cli(key, question, english, model=model, timeout=max(timeout, 180),
                       facts=facts, extra=extra)
    model = model or os.environ.get("TQ_MODEL") or DEFAULT_MODEL
    prompt = build_prompt(key, question, english, facts)
    body = {"model": model, "max_tokens": max_tokens,
            "system": STYLE + extra,
            "messages": [{"role": "user", "content": prompt}]}
    if temperature is not None:
        body["temperature"] = temperature
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": api_key(), "anthropic-version": VERSION,
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        if e.code == 401:
            sys.exit("tq: the Anthropic API rejected the key (401).\n%s" % SETUP)
        return None, {"error": "HTTP %s %s" % (e.code, detail)}
    text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
    u = res.get("usage") or {}
    usage = {"model": res.get("model", model),
             "input_tokens": u.get("input_tokens", 0),
             "output_tokens": u.get("output_tokens", 0),
             "via": "api", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return text.strip(), usage


USAGE_PATH = os.path.join(config.DATA, "usage.json")


def record(key, usage):
    """Accumulate, never overwrite: a redraft costs more, it does not cost instead."""
    try:
        data = json.load(open(USAGE_PATH))
    except (IOError, ValueError):
        data = {}
    prev = data.get(key, {})
    data[key] = {
        "model": usage.get("model"),
        "input_tokens": prev.get("input_tokens", 0) + usage.get("input_tokens", 0),
        "output_tokens": prev.get("output_tokens", 0) + usage.get("output_tokens", 0),
        "cost_usd": round(prev.get("cost_usd", 0.0) + usage.get("cost_usd", 0.0), 6),
        "drafts": prev.get("drafts", 0) + 1,
        # A fanout is one round of several calls. Rounds written before calls
        # were counted are assumed to be the three-call kind.
        "calls": prev.get("calls", 3 * prev.get("drafts", 0)) + usage.get("calls", 1),
        "last": usage.get("at"),
    }
    json.dump(data, open(USAGE_PATH, "w"), indent=1, sort_keys=True)
    return data[key]


def mark_accepted(key):
    """Record that the text now in answers/ came from a model draft."""
    data = load_usage()
    if key in data:
        data[key]["accepted"] = True
        json.dump(data, open(USAGE_PATH, "w"), indent=1, sort_keys=True)


def one_line(system, prompt, timeout=90):
    """A single short line from the model, for things like a ticket summary.

    Returns (text, usage), or (None, usage) when no backend is available.
    """
    via = backend()
    if via is None:
        return None, {"error": "no backend"}
    if via == "demo":
        import demo_backend
        return demo_backend.one_line(system, prompt)
    if via == "cli":
        cmd = ["claude", "-p", "--output-format", "json", "--no-session-persistence",
               "--system-prompt", system, "--tools", "", "--setting-sources", "",
               "--model", os.environ.get("TQ_MODEL") or DEFAULT_MODEL]
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               timeout=timeout, cwd=tempfile.gettempdir())
            d = json.loads(r.stdout)
        except (subprocess.TimeoutExpired, ValueError):
            return None, {"error": "claude CLI gave no usable answer"}
        if d.get("is_error"):
            return None, {"error": (d.get("result") or "")[:120]}
        u = d.get("usage") or {}
        return (d.get("result") or "").strip().splitlines()[0] if d.get("result") else None, {
            "input_tokens": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                            + u.get("cache_read_input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0), "cost_usd": d.get("total_cost_usd") or 0.0}
    body = {"model": os.environ.get("TQ_MODEL") or DEFAULT_MODEL, "max_tokens": 80,
            "system": system, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST",
                                 headers={"x-api-key": api_key(), "anthropic-version": VERSION,
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        return None, {"error": "HTTP %s" % e.code}
    text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
    u = res.get("usage") or {}
    return (text.strip().splitlines() or [None])[0], {
        "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}


def load_usage():
    try:
        return json.load(open(USAGE_PATH))
    except (IOError, ValueError):
        return {}
