import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SAMPLE = os.path.join(ROOT, "demo", "sample-product")

_work = tempfile.mkdtemp(prefix="tq-test-")
json.dump({"sourceRepo": SAMPLE, "crowdinProjectId": 1, "team": ["demo.reviewer"],
           "skipMention": ["demo.reviewer"]}, open(os.path.join(_work, "config.json"), "w"))
json.dump({}, open(os.path.join(_work, "team.json"), "w"))
os.environ.update(TQ_CONFIG=os.path.join(_work, "config.json"),
                  TQ_TEAM_FILE=os.path.join(_work, "team.json"), TQ_DATA=_work)
sys.path.insert(0, os.path.join(ROOT, "lib"))
