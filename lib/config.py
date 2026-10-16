"""Configuration, in two layers.

team.json is committed and shared: the Crowdin project, who is on the team, and
who is never at-mentioned. Everyone running tq must agree on these, since a
teammate missing from one person's list makes their replies invisible to that
person's tq, so they live in one file that git keeps in step.

config.json is gitignored and personal: the path to this machine's product
checkout, and any override. A scalar set there replaces the shared value; a
list set there is added to the shared one. Each value can also be overridden
from the environment, which is what CI or a one-off run should use.
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.environ.get("TQ_TEAM_FILE") or os.path.join(ROOT, "team.json")
_PATH = os.environ.get("TQ_CONFIG") or os.path.join(ROOT, "config.json")
# Where state, drafts and caches live. The checkout by default; the demo points
# it at a scratch directory so a demo run never touches a real queue.
DATA = os.environ.get("TQ_DATA") or ROOT


def _load(path, required):
    try:
        return json.load(open(path))
    except IOError:
        if required:
            sys.exit("tq: %s is missing. Copy config.example.json to config.json and "
                     "set sourceRepo." % path)
        return {}
    except ValueError as e:
        sys.exit("tq: %s is not valid JSON (%s)." % (path, e))


_CFG = _load(_SHARED, False)
for _k, _v in _load(_PATH, True).items():
    if isinstance(_v, list) and isinstance(_CFG.get(_k), list):
        _CFG[_k] = sorted(set(_CFG[_k]) | set(_v))
    else:
        _CFG[_k] = _v


def project_id():
    v = os.environ.get("TQ_CROWDIN_PROJECT_ID") or _CFG.get("crowdinProjectId")
    if not v:
        sys.exit("tq: crowdinProjectId is not set in config.json.")
    return int(v)


def source_repo():
    v = os.path.expanduser(os.environ.get("TQ_SOURCE_REPO") or _CFG.get("sourceRepo") or "")
    if not os.path.isdir(v):
        sys.exit("tq: sourceRepo %r is not a directory. Set it in config.json." % v)
    return v


DEFAULTS = {
    "crowdinApi": "https://api.crowdin.com/api/v2",
    "crowdinEditor": "https://crowdin.com/editor",
    "crowdinProjectSlug": "my-product",
    "languageFile": "i18n/Language.properties",
    "extraRepo": "",
    "extraRef": "",
    "canonicalRemote": "",
    "commitUrlBase": "",
    "jiraHost": "",
    "jiraProject": "",
    "jiraTaskTypeId": "",
    "productName": "the product",
}


def get(name):
    """One setting: the environment (TQ_<NAME>), then config, then the default."""
    env = "TQ_" + re.sub(r"(?<!^)([A-Z])", r"_\1", name).upper()
    v = os.environ.get(env)
    if v is None:
        v = _CFG.get(name, DEFAULTS.get(name, ""))
    return os.path.expanduser(v) if isinstance(v, str) and v.startswith("~") else v


def skip_mention():
    """Usernames to leave out of the at-mention line.

    Ourselves, and the localization manager the translators already address, so
    a reply never pings the people who wrote it.
    """
    v = os.environ.get("TQ_SKIP_MENTION")
    if v:
        return {s.strip() for s in v.split(",") if s.strip()}
    return set(_CFG.get("skipMention") or [])


def team():
    """Crowdin usernames whose replies count as ours.

    tq treats a thread as answered when someone on this list has replied, so it
    must name everyone who answers translators, not only the person running it.
    """
    v = os.environ.get("TQ_TEAM")
    if v:
        return {s.strip() for s in v.split(",") if s.strip()}
    return set(_CFG.get("team") or [])
