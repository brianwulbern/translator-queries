# tq: answer translators from the source code

`tq` is a command-line tool for the person who answers translators' questions on
a Crowdin project. Translators open an issue on a string ("what replaces {0}?",
"is this a button?", "is 'set' a noun here?"), and someone who can read the code
has to answer it. `tq` does the reading, drafts the reply, and keeps every
promise the reply makes tracked until it is kept.

> This is a generalized, sanitized extract of a tool used in real work on a
> large product's localization. The product, people, tickets and data in it are
> invented. Crowdin, Jira and the model are replaced by offline stand-ins in
> demo mode, so the whole flow runs with no network and no accounts.

## Try the demo

```bash
python3 demo.py          # builds a sample product, then runs tq end to end
python3 -m unittest discover -s tests -t .
```

The demo builds an invented product, "Acme Studio", into a real git repository
with backdated history, seeds a fake Crowdin with six translator questions and a
fake Jira with the tickets behind them, and then runs `tq` the way a person
would: it checks the setup, prints the facts for three keys, reviews the queue
with scripted answers, files a follow-up, and releases the hold once the
follow-up is marked done. Nothing outside a scratch directory changes.

## How it works

```
 Crowdin issues ──► pull ──► group by string ──► facts (deterministic) ──► draft (model)
                                                     │                        │
                                   source repo ──────┤                        ▼
                                   git history ──────┤                  review: send,
                                   reference tables ─┘                  redraft x3, file,
                                                                        skip, quit
                                                                              │
                                                    Jira (labels) ◄── follow-up Task
                                                          │
                                         next run: Done? ─┴─► offer to release the hold
```

### Facts first, prose last

Almost every translator question turns on a fact that is in the code: what
fills a placeholder, whether the string is drawn or only read aloud by a screen
reader, whether it is used at all. `lib/keyfacts.py` gathers those facts
mechanically, and the model is only asked to write two or three sentences from
them. Moving work out of the model took a draft from about 20,000 input tokens
to under 2,000, and made the answers checkable.

The facts it extracts:

- **Arguments**: the expression that fills each placeholder, read from the call
  with balanced parentheses rather than from the one line that matched. Java
  arrays and JavaScript arrays are unpacked, and a JSX argument is described by
  what it shows ("a link showing task.itemTitle").
- **Surface**: whether the string is announced (`announce`, `aria-label`,
  `aria-live`) or drawn, read from the lines around the use, since the live
  region is usually declared on an enclosing element.
- **The change that added it**: the commit subject and the sibling strings added
  with it. That is what explains a string whose screen has not shipped yet,
  instead of calling it unused.
- **The primary use**: a common word like "Clean" matches dozens of unrelated
  places. Uses added by the same change come first, ahead of tests and of
  literals that are not translation calls, with a short window of the code
  around it.
- **Component-filled placeholders**: when a key is handed to a shared component
  that fills the placeholder in its own package, the value cannot be read from
  the product code. Those are recorded once in `reference/` tables and joined
  in by key.
- **Labels inside a sentence**: words in a string that are themselves UI labels,
  so the reply can say they name something rather than being ordinary words.

### Review is a control room

Running `tq` with no arguments pulls the queue, checks every filed follow-up in
Jira, resolves each new key's commit and ticket, drafts every new question with
one call each in parallel, and then walks the queue. At each string: `s` send,
`f` send and file a follow-up, `r` three independent redrafts to choose from,
`rf` both, `k` or Enter to skip, `q` to quit.

### Promises are tracked, not hoped for

A reply that says "we will confirm with the product team" is only true if
something makes it happen. `f` files a Jira Task routed by the components of the
ticket the string was built under, and sends the reply only after the Task
exists, so a refused ticket never leaves a promise pointing at nothing. The
Crowdin issue stays open, because an open issue is the only thing anyone is
prompted to look at. When the Task reaches Done, the next run offers to resolve
it.

### More than one person

Jira is the shared record of what has been filed: every Task carries the labels
`tq-followup` and `crowdin-<string id>`, and every user's `tq` finds Tasks by
searching for them, so nobody files a duplicate. A reply from anyone on the team
counts as answered, so nobody answers a thread twice.

## Design notes worth keeping

- **Comments are writes to the source.** A Crowdin token scoped to read sources
  and write translations reads the whole queue and then fails with 403 on the
  first reply. `tq doctor` probes the write scope by aiming a comment at a string
  id that cannot exist: 403 means the scope is missing, 400 means it is fine,
  and nothing is created either way.
- **Whole words only at the prompt.** An early version matched prefixes, so
  typing "skip" sent the reply ("s") and "abort" sent the whole queue ("a").
- **Gather facts once before a fanout.** Facts are captured by swapping
  `sys.stdout`, which is process-wide; three threads doing it at once scrambled
  each other's output.
- **Bill on the call, not on acceptance.** A redraft that is declined still
  cost what it cost, so usage is recorded when the call returns.
- **A new key is not an orphan.** A string whose screen has not landed looks
  exactly like a leftover. Only a key with no use and an old introducing commit
  is treated as unused.

## Layout

| Path | What it is |
| --- | --- |
| `bin/tq` | Launcher; sources a credentials file if the token is not set. |
| `lib/tq.py` | Pull, review loop, sending, filing, doctor. |
| `lib/keyfacts.py` | The deterministic facts engine. |
| `lib/draft.py` | Drafting through the Claude CLI or API, with usage accounting. |
| `lib/followup.py` | Jira Tasks: routing, summaries, labels, status. |
| `lib/links.py` | A key's introducing commit and context ticket. |
| `lib/demo_backend.py` | Offline Crowdin, Jira and drafter for the demo and tests. |
| `reference/` | Placeholder values filled outside the product code. |
| `demo/` | The sample product, its commit plan, and the fixtures. |

## Using it for real

Copy `config.example.json` to `config.json`, point `sourceRepo` at the product
checkout, set the Crowdin project and Jira details, and run `tq doctor`. It
needs a Crowdin personal access token with "Source files & strings" set to Read
and Write, and either the Claude CLI or an API key for drafting.
