#!/usr/bin/env python3
"""Run tq end to end against a sample product, offline.

Builds demo/sample-product into a real git repository with a backdated history,
points tq at it with a fake Crowdin and Jira (lib/demo_backend.py), and walks
the whole flow: doctor, facts, a review session with scripted answers, a filed
follow-up, and the release once that follow-up is done.

Nothing touches the network, and nothing outside the scratch directory changes.
"""
import argparse, datetime, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TQ = os.path.join(HERE, "lib", "tq.py")


def build_repo(dest):
    src = os.path.join(HERE, "demo", "sample-product")
    plan = json.load(open(os.path.join(HERE, "demo", "commits.json")))
    lang = dict(line.rstrip("\n").split("=", 1)
                for line in open(os.path.join(src, "i18n", "Language.properties")))
    have = {}
    os.makedirs(dest)
    git = lambda *a, **kw: subprocess.run(["git", "-C", dest] + list(a), check=True,
                                          capture_output=True, **kw)
    git("init", "-q", "-b", "main")
    for step in plan:
        for rel in step["files"]:
            os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
            shutil.copy(os.path.join(src, rel), os.path.join(dest, rel))
        for k in step["keys"]:
            have[k] = lang[k]
        os.makedirs(os.path.join(dest, "i18n"), exist_ok=True)
        with open(os.path.join(dest, "i18n", "Language.properties"), "w") as f:
            for k in sorted(have):
                f.write("%s=%s\n" % (k, have[k]))
        when = step.get("date") or (datetime.datetime.now()
                                    - datetime.timedelta(days=step["daysAgo"])).strftime(
                                        "%Y-%m-%dT%H:%M:%S")
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when,
                   GIT_AUTHOR_NAME="Demo Dev", GIT_AUTHOR_EMAIL="dev@example.com",
                   GIT_COMMITTER_NAME="Demo Dev", GIT_COMMITTER_EMAIL="dev@example.com")
        git("add", "-A")
        git("commit", "-q", "--no-gpg-sign", "-m", step["subject"], env=env)


def run(env, title, args, answers=None):
    print("\n" + "#" * 78)
    print("# $ tq %s" % " ".join(args) + ("      (scripted answers: %s)" % " ".join(
        repr(a) for a in answers) if answers else ""))
    print("# " + title)
    print("#" * 78, flush=True)
    r = subprocess.run([sys.executable, TQ] + args, env=env, text=True,
                       input="\n".join(answers or []) + "\n", capture_output=True)
    sys.stdout.write(r.stdout)
    if r.returncode not in (0, None):
        sys.stdout.write(r.stderr)
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    a = ap.parse_args()

    work = tempfile.mkdtemp(prefix="tq-demo-")
    repo = os.path.join(work, "acme-studio")
    data = os.path.join(work, "data")
    os.makedirs(os.path.join(data, "answers"))
    build_repo(repo)

    cfg = {"sourceRepo": repo, "crowdinProjectId": 1, "crowdinProjectSlug": "acme-studio",
           "languageFile": "i18n/Language.properties",
           "commitUrlBase": "https://git.example.com/acme/studio/commit/",
           "jiraHost": "https://jira.example.com", "jiraProject": "SHOP",
           "jiraTaskTypeId": "10002", "productName": "Acme Studio",
           "team": ["demo.reviewer"], "skipMention": []}
    json.dump(cfg, open(os.path.join(work, "config.json"), "w"), indent=1)
    json.dump({}, open(os.path.join(work, "team.json"), "w"))
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("CROWDIN_", "JIRA_", "ANTHROPIC_", "TQ_"))}
    env = dict(base, TQ_DEMO=os.path.join(HERE, "demo", "fixtures"), TQ_DATA=data,
               TQ_CONFIG=os.path.join(work, "config.json"),
               TQ_TEAM_FILE=os.path.join(work, "team.json"),
               PYTHONPATH=os.path.join(HERE, "lib"))

    subprocess.run(["bash", os.path.join(HERE, "lib", "build-history-index.sh"),
                    os.path.join(data, "lang-history.tsv")], env=env, check=True)
    run(env, "Check the setup. In a real run this proves the token can write.", ["doctor"])
    run(env, "The facts the drafter is given, gathered from the source alone.",
        ["facts", "clean,x-sent-you-x-for-x,export-size"])
    run(env, "The control room: pull, draft every new question, review one at a time. "
             "Send four, send the leftover string's hold and file its follow-up, skip one.",
        ["review"], ["s", "s", "s", "f", "", "y", "s", "k"])
    run(env, "Next run: answered threads are gone, the follow-up is tracked in Jira.",
        ["followups"])

    sys.path.insert(0, os.path.join(HERE, "lib"))
    os.environ.update(env)
    import demo_backend
    filed = [k for k, i in demo_backend._world()["jira"]["issues"].items()
             if "tq-followup" in i.get("labels", [])]
    for k in filed:
        demo_backend.finish(k)
    print("\n(the product team marks %s Done)" % ", ".join(filed))
    run(env, "Once the Task is done, tq offers to release the hold it was guarding.",
        ["review", "--only", "legacy-sync-label"], ["y"])

    if a.keep:
        print("\nscratch kept at %s" % work)
    else:
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
