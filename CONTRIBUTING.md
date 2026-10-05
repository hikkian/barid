# Contributing

Thanks for looking. Barid is one Python file (`skills/barid/scripts/barid.py`) plus one HTML file (`panel.html`) next to it. The rule that keeps it useful: **no dependencies** (Python 3.9+ standard library only, no build step for the panel).

## Run the tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers the rules (permissions, dependencies, conflicts, outcomes), concurrency (threads and processes racing for one resource), the CLI end to end, the panel server (including its security checks) and the panel's texts.

## Things the tests enforce that are easy to forget

- Every text in the panel exists in **both** languages (English and Russian) with the same placeholders. A message shown after an action must say what happened ("Report accepted"), not repeat the button label.
- Every error that carries a code (`RBError(message, "code")`) needs a translation in the panel.
- Every command shown in the README or the skill must parse. After changing a command, update the docs.
- Output must not crash on non-ASCII text on a narrow Windows console.

## Pull requests

Small and focused is best. Describe the problem, not only the change. If you add a rule to the protocol, update `docs/PROTOCOL.md` and add a test next to the existing ones. CI runs on Linux, macOS and Windows with Python 3.9, 3.12 and 3.13.
