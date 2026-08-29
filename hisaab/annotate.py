"""Blind annotation and inter-annotator agreement.

Two jobs, deliberately kept apart:

  agreement   a second person, who did not write the corpus, reads each
              utterance with the answer hidden and writes down the rupee value
              they think it means. Exact-match agreement against the author is
              the number. A corpus one person verified is one person's opinion,
              and the eval inherits every assumption they made.

  naturalness a single rater judges whether a generated utterance is something
              a merchant would actually say. This is a filter, not an agreement
              statistic — there is no second opinion to compare it to, and
              reporting a kappa for one rater would be dressing up a yes/no as
              a measurement.

The author cannot be the second annotator. Whoever wrote the values already
knows them, and an "agreement" score computed that way measures nothing. This
module produces the worksheet and scores what comes back; it does not fill it in.
"""

import argparse
import json
import os
import sys

from .scenarios import load


def blind(path, out):
    """Worksheet with the answers stripped. Hand this to someone else."""
    n = 0
    with open(out, "w") as f:
        f.write("// Fill intended_paise for every row you can. Leave it null if the\n"
                "// utterance genuinely does not state an amount. `natural` is your\n"
                "// judgement on whether a merchant would say this out loud.\n"
                "// Do not open scenarios/*.jsonl before you finish.\n")
        for s in load(path):
            f.write(json.dumps({"id": s.id, "lang": s.lang, "turns": s.turns,
                                "intended_paise": None, "natural": None}) + "\n")
            n += 1
    return n


def _rows(path):
    out = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("//"):
            r = json.loads(line)
            out[r["id"]] = r
    return out


def score(worksheet, path):
    truth = {s.id: s for s in load(path)}
    got = _rows(worksheet)
    scored = [(i, truth[i].intended_paise, got[i]["intended_paise"])
              for i in got if i in truth and truth[i].intended_paise is not None]
    blank = [i for i, _, b in scored if b is None]
    judged = [(i, a, b) for i, a, b in scored if b is not None]
    agree = [(i, a, b) for i, a, b in judged if a == b]

    out = ["AMOUNT AGREEMENT (author vs. annotator)", "-" * 58,
           "  scenarios with an amount : %d" % len(scored),
           "  annotated                : %d  (%d left blank)" % (len(judged), len(blank)),
           "  exact agreement          : %d / %d  (%.1f%%)"
           % (len(agree), len(judged), 100.0 * len(agree) / max(len(judged), 1))]
    dis = [(i, a, b) for i, a, b in judged if a != b]
    if dis:
        out += ["", "  DISAGREEMENTS — resolve each by hand before trusting the corpus:"]
        for i, a, b in dis:
            out.append("    %-12s author %-12d annotator %-12d  %s"
                       % (i, a, b, truth[i].turns[0][:44]))

    nat = [(i, r["natural"]) for i, r in got.items() if r.get("natural") is not None]
    if nat:
        bad = [i for i, v in nat if not v]
        out += ["", "NATURALNESS (single rater — a filter, not a statistic)", "-" * 58,
                "  judged   : %d" % len(nat),
                "  rejected : %d  %s" % (len(bad), bad[:12])]
    return "\n".join(out), len(dis)


def main(argv=None):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    seed = os.path.join(root, "scenarios", "seed.jsonl")
    ap = argparse.ArgumentParser(prog="hisaab.annotate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("blind", help="emit a worksheet with the answers stripped")
    b.add_argument("--scenarios", default=seed)
    b.add_argument("--out", default=os.path.join(root, "scenarios", "worksheet.jsonl"))
    c = sub.add_parser("score", help="score a filled worksheet against the corpus")
    c.add_argument("worksheet")
    c.add_argument("--scenarios", default=seed)
    a = ap.parse_args(argv)

    if a.cmd == "blind":
        print("%d rows -> %s" % (blind(a.scenarios, a.out), a.out))
    else:
        text, dis = score(a.worksheet, a.scenarios)
        print(text)
        return 1 if dis else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
