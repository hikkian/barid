"""Checks a translation of a Markdown document against its English original. Not a unittest: run it by hand or from a task.

    python3 tests/translation_check.py docs/PROTOCOL.md docs/PROTOCOL.ru.md ru

It cannot judge the language, only that nothing was lost or broken: the same headings, the same code blocks (a comment after `#` may be
translated), every inline `code` span of the original still present, the same link targets, a similar length, and the right alphabet.
Exit code 0 = fine, 1 = problems (listed).
"""
import re
import sys
from collections import Counter
from pathlib import Path

FENCE = re.compile(r"^```.*?^```", re.S | re.M)
SPAN = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEAD = re.compile(r"^(#{1,6})\s", re.M)
KK_LETTERS = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")


def blocks(text):
    out = []
    for b in FENCE.findall(text):
        out.append([ln.split(" #")[0].rstrip() for ln in b.splitlines()])
    return out


def check(en_path, tr_path, lang):
    en, tr = Path(en_path).read_text("utf-8"), Path(tr_path).read_text("utf-8")
    bad = []
    if Counter(HEAD.findall(en)) != Counter(HEAD.findall(tr)):
        bad.append(f"headings differ: original {dict(Counter(HEAD.findall(en)))}, translation {dict(Counter(HEAD.findall(tr)))}")
    a, b = blocks(en), blocks(tr)
    if len(a) != len(b):
        bad.append(f"{len(a)} code blocks in the original, {len(b)} in the translation")
    else:
        for i, (x, y) in enumerate(zip(a, b), 1):
            if x != y:
                first = next((f"original {p!r} / translation {q!r}" for p, q in zip(x, y) if p != q), "different number of lines")
                bad.append(f"code block {i} differs: {first}")
    plain_en, plain_tr = FENCE.sub("", en), FENCE.sub("", tr)
    missing = Counter(SPAN.findall(plain_en)) - Counter(SPAN.findall(plain_tr))
    if missing:
        bad.append("inline code that is missing or changed in the translation: " + ", ".join(sorted(missing)[:12]))
    sibling = re.compile(r"^[A-Za-z_]+(\.(ru|kk|kz))?\.md$")        # a link to a sibling document may point at its translation
    links = lambda t: Counter(u for u in LINK.findall(t) if not sibling.match(u))
    if links(en) != links(tr):
        bad.append("link targets differ: " + ", ".join(sorted((links(en) - links(tr)) | (links(tr) - links(en)))[:6]))
    ratio = len(tr.splitlines()) / max(1, len(en.splitlines()))
    if not 0.7 <= ratio <= 1.5:
        bad.append(f"the translation has {ratio:.2f} times the lines of the original")
    letters = [c for c in plain_tr if c.isalpha()]
    cyr = sum("Ѐ" <= c <= "ӿ" for c in letters) / max(1, len(letters))
    if cyr < 0.35:
        bad.append(f"only {cyr:.0%} of the letters are Cyrillic: is it translated?")
    if lang == "kk" and sum(c in KK_LETTERS for c in plain_tr) < 20:
        bad.append("almost no Kazakh letters (ә ғ қ ң ө ұ ү һ і): this looks like Russian, not Kazakh")
    if re.search(r"TODO|FIXME|\.\.\.\s*\(translat", tr):
        bad.append("a TODO or an unfinished passage is left")
    return bad


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[3] not in ("ru", "kk"):
        sys.exit(__doc__)
    problems = check(*sys.argv[1:])
    print("OK" if not problems else "PROBLEMS:\n- " + "\n- ".join(problems))
    sys.exit(1 if problems else 0)
