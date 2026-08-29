"""Combinatorial sweep of the Indic amount space.

Hand-verification is non-negotiable for anything where a human has to *judge*
what an utterance means. The Indic amount space is not that: it is mechanical.
"sava char lakh" is (4 + 0.25) x 100000 by the same rule that makes "sava do
hazaar" 2250, and a human checking that is checking arithmetic, not exercising
judgement.

So generation is allowed here — with two hard limits.

  1. Only attested composition shapes. "saade ek" is not Hindi; that quantity
     is "dedh". A generator that emits it produces utterances nobody says and
     an eval that measures nothing. The shape whitelist below is the entire
     linguistic claim this module makes, and it is small enough to argue with.

  2. Everything ships `verified: false` until a human reads it and signs off on
     *naturalness* — not on the arithmetic. `python -m hisaab.annotate` produces
     the worksheet. Generated scenarios stay out of headline numbers until then.

Output is deterministic: same input, same file, so a diff means someone changed
the rules rather than reran the dice.
"""

import json
import os

from .amounts import parse_amount, to_paise

SCALES = ("sau", "hazaar", "lakh", "crore")

# saade is not used with 1 or 2 — those quantities are dedh and dhai.
CARDINALS = ("do", "teen", "char", "panch", "chhe", "saat", "aath", "nau",
             "das", "bees", "pachees", "tees", "chalis", "pachas")
_SAADE_MIN = ("teen", "char", "panch", "chhe", "saat", "aath", "nau", "das",
              "bees", "pachees", "tees", "chalis", "pachas")

STANDALONE = ("dedh", "dhai", "adha")

# (carrier, needs_entity). Kept short: the model is being tested on the amount,
# not on parsing an essay.
REFUND = [
    ("{a} ka refund kar do {e} pe.", "hinglish"),
    ("{e} par {a} wapas kar dijiye.", "hinglish"),
    ("{a} ka refund chahiye {e} ke against.", "hinglish"),
    ("{e} se {a} refund karna hai.", "hi"),
]
LINK = [
    ("{a} ka payment link banao.", "hinglish"),
    ("{a} ka link bhej dijiye customer ko.", "hinglish"),
    ("{a} ka payment link chahiye.", "hi"),
]

# Refund targets, largest first: a refund must not exceed the payment.
PAYMENTS = (("pay_VIP", 5000000), ("pay_A1", 249900), ("pay_B1", 125000))


def shapes():
    """Every attested composition, as (phrase, shape-label)."""
    for sc in SCALES:
        for pre in ("sava", "paune"):
            yield ("%s %s" % (pre, sc), "prefix+scale")           # sava lakh
            for c in CARDINALS:
                yield ("%s %s %s" % (pre, c, sc), "prefix+cardinal+scale")
        for c in _SAADE_MIN:
            yield ("saade %s %s" % (c, sc), "saade+cardinal+scale")
        for st in STANDALONE:
            yield ("%s %s" % (st, sc), "standalone+scale")
        for c in CARDINALS:
            yield ("%s %s" % (c, sc), "cardinal+scale")           # control, no fraction
    for big, small in (("lakh", "hazaar"), ("crore", "lakh"), ("hazaar", "sau")):
        for c in ("do", "teen", "panch"):
            for d in ("pachas", "pachhattar", "bees"):
                yield ("%s %s %s %s" % (c, big, d, small), "additive")


def generate(limit=130, stride=2):
    """A deterministic slice of the space. `stride` thins the sweep so the set
    stays reviewable by a human in one sitting — 120 utterances is roughly an
    hour of careful reading, which is the real constraint."""
    out, n = [], 0
    for idx, (phrase, shape) in enumerate(shapes()):
        if idx % stride:
            continue
        paise = to_paise(parse_amount(phrase))
        if paise <= 0 or paise > 10 ** 12:
            continue
        target = next(((p, amt) for p, amt in PAYMENTS if amt >= paise), None)
        if target:
            carrier, lang = REFUND[n % len(REFUND)]
            ent = target[0]
            turn = carrier.format(a=phrase, e=ent)
            sc = {"expect_tool": "create_refund", "intended_entity": ent}
        else:
            carrier, lang = LINK[n % len(LINK)]
            turn = carrier.format(a=phrase)
            sc = {"expect_tool": "create_payment_link"}
        out.append(dict(id="gen-%03d" % n, family="indic", lang=lang,
                        turns=[turn], amount_phrase=phrase,
                        intended_paise=paise, verified=False,
                        note="machine-composed, shape=%s; awaiting naturalness sign-off" % shape,
                        **sc))
        n += 1
        if n >= limit:
            break
    return out


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "scenarios", "generated.jsonl")
    rows = generate()
    with open(path, "w") as f:
        f.write("// MACHINE-COMPOSED. Every row is verified:false until a human signs off on\n"
                "// naturalness via `python -m hisaab.annotate`. Excluded from headline\n"
                "// numbers. Regenerate with `python -m hisaab.generate` — output is\n"
                "// deterministic, so a diff means the rules changed.\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")
    shapes_used = sorted({r["note"].split("shape=")[1].split(";")[0] for r in rows})
    print("%d scenarios -> %s" % (len(rows), path))
    print("shapes: %s" % ", ".join(shapes_used))


if __name__ == "__main__":
    main()
