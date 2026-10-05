#!/usr/bin/env python3
"""Build the example board used in the README and the screenshots.

    python3 examples/build_example.py [target-dir]      # default: ./example-project

Then:  cd example-project && python3 ../skills/barid/scripts/barid.py open
"""
import importlib.util
import sys
from pathlib import Path

RB = Path(__file__).resolve().parent.parent / "skills" / "barid" / "scripts" / "barid.py"
spec = importlib.util.spec_from_file_location("rb", RB)
rb = importlib.util.module_from_spec(spec)
sys.modules["rb"] = rb
spec.loader.exec_module(rb)

PLANNER = rb.Actor("agent", "planner")
QA = rb.Actor("agent", "qa-agent")
BUILD = rb.Actor("agent", "build-agent")
ME = rb.HUMAN


def main(target: Path) -> Path:
    path = target / ".barid" / "board.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    d = rb.new_board("Ship v1.0 of a CLI tool", [("build", "Build agent"), ("docs", "Docs agent"), ("qa", "QA agent")])
    d["resources"] = {"staging": {"label": "Staging server", "exclusive": True}, "user": {"label": "User present", "exclusive": True}}
    d["policy"]["direct_agents"] = ["planner"]
    d["context"] = ["README.md", "docs/architecture.md"]
    rb.save(path, d)

    def add(actor, iid, lane, title, text="", **kw):
        kw.update(id=iid, lane=lane, title=title, text=text)
        return rb.mutate(path, lambda b: rb.op_add(b, actor, kw))

    add(PLANNER, "T0", "build", "Audit the dependencies", "List outdated and vulnerable dependencies. Write the result to docs/reports/deps-audit.md.", profile="light")
    add(PLANNER, "T1", "build", "Refactor the config loader", "Split the config loader into parse and validate steps.\nDeadline: 90 minutes.\nDo not touch the public CLI flags.\nReport: docs/reports/config-refactor.md.", needs="T0", profile="dev")
    add(PLANNER, "T2", "docs", "Update the README for the new config format", "Rewrite the configuration section of the README for the refactored loader.", needs="T1", profile="light")
    add(PLANNER, "T3", "qa", "Write the regression test suite", "Cover the 12 user-reported bugs with tests. Deadline: 2 hours. Report: docs/reports/regression.md.", profile="dev")
    add(PLANNER, "T4", "build", "Fix the failing tests", "", needs="T3", outline="Fix every test that the regression suite found failing; keep the diff minimal.", profile="dev")
    add(PLANNER, "T5", "qa", "Staging smoke test", "", needs="T4", uses="staging", profile="bench", outline="Install the release candidate on staging and run the smoke checklist.")
    add(PLANNER, "T6", "docs", "Write the release notes", "", needs="T2", outline="Release notes from the merged pull requests since v0.9.", profile="light")
    add(PLANNER, "T7", "build", "Tag and publish v1.0", "", needs="T5,T6", uses="staging,user", profile="light", outline="Tag, publish to the package index, announce.")
    add(QA, "T8", "build", "Add a --dry-run flag", "Several users asked for a --dry-run flag. Suggested scope: print what would change, change nothing.", profile="dev")
    # states
    rb.mutate(path, lambda b: rb.op_claim(b, BUILD, "T0"))
    rb.mutate(path, lambda b: rb.op_finish(b, BUILD, "T0", report="docs/reports/deps-audit.md", note="3 outdated, 0 vulnerable"))
    rb.mutate(path, lambda b: rb.op_accept(b, ME, "T0"))
    rb.mutate(path, lambda b: rb.op_claim(b, BUILD, "T1"))
    rb.mutate(path, lambda b: rb.op_note(b, BUILD, "T1", "Parser is split, validation next."))
    return path


if __name__ == "__main__":
    out = main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / "example-project")
    print(out)
