"""Amount expressions in English, Hindi and Hinglish -> rupees.

Why this exists: every scenario in the corpus carries a hand-authored
`intended_paise`. This parser is a *corpus tool* — it generates candidate
utterances and catches authoring mistakes in the seed set. It is never the
oracle the model is graded against; the hand-verified value on the scenario is.

The interesting bit is fractional prefixes. Hindi builds amounts with
quarter-steps that have no English equivalent:

    sava  X   = X + 0.25      sava lakh        = 1.25 lakh = 125000
    paune X   = X - 0.25      paune do hazaar  = 1.75 x 1000 = 1750
    saade X   = X + 0.5       saade teen hazaar = 3500

and three standalone fractional numbers:

    dedh = 1.5   dhai = 2.5   adha = 0.5

Stack any of those on Razorpay's paise boundary and a single slip is 100x.
"""

from decimal import Decimal, ROUND_HALF_UP
import re

# Scale words. "sau" behaves like English "hundred": it multiplies whatever
# is pending rather than closing a group ("unnees sau" = 1900).
SCALES = {
    "sau": 100, "so": 100, "hundred": 100,
    "hazaar": 1000, "hazar": 1000, "hajaar": 1000, "hazar": 1000,
    "thousand": 1000, "k": 1000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lakh_": 100_000, "l": 100_000,
    "million": 1_000_000,
    "crore": 10_000_000, "crores": 10_000_000, "karod": 10_000_000, "cr": 10_000_000,
}

# Applied to the NEXT number, or to an implicit 1 if a scale word follows.
FRACTION_PREFIX = {
    "sava": Decimal("0.25"), "savva": Decimal("0.25"), "sawa": Decimal("0.25"),
    "paune": Decimal("-0.25"), "pauna": Decimal("-0.25"), "pone": Decimal("-0.25"),
    "saade": Decimal("0.5"), "sade": Decimal("0.5"), "sadhe": Decimal("0.5"),
}

STANDALONE_FRACTION = {
    "dedh": Decimal("1.5"), "derh": Decimal("1.5"), "dedhh": Decimal("1.5"),
    "dhai": Decimal("2.5"), "dhaai": Decimal("2.5"), "dhayi": Decimal("2.5"),
    "adha": Decimal("0.5"), "aadha": Decimal("0.5"), "aadhaa": Decimal("0.5"),
}

# ponytail: common money numbers only, not all 1-99 Hindi cardinals.
# Extend from the corpus when a scenario needs one that is missing —
# an unknown word raises rather than guessing.
WORDS = {
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "panch": 5,
    "paanch": 5, "chhe": 6, "che": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
    "gyarah": 11, "barah": 12, "terah": 13, "chaudah": 14, "pandrah": 15,
    "solah": 16, "satrah": 17, "atharah": 18, "unnees": 19, "bees": 20,
    "pachees": 25, "tees": 30, "chalis": 40, "chaalis": 40, "pachas": 50,
    "pachaas": 50, "saath": 60, "sattar": 70, "pachhattar": 75, "assi": 80,
    "nabbe": 90,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "twentyfive": 25, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

# Words carrying no numeric weight.
NOISE = {
    "rupaye", "rupay", "rupee", "rupees", "rs", "inr", "ka", "ke", "ki", "aur",
    "and", "only", "total", "amount", "paise", "matlab", "please", "kar", "do_",
    "refund", "hai", "the", "a", "of", "sirf", "bas",
}

# KNOWN HOMOGRAPH: "do" is both the Hindi cardinal 2 and the imperative in
# "kar do" / "refund kar do". Context decides, and this parser has none — it
# reads the cardinal. Scenarios therefore carry `amount_phrase` (the amount
# span alone) for the second-opinion check, and the full utterance is what the
# model sees. The ambiguity is not a parser bug to fix; it is a hazard the
# corpus has to cover, so `indic` includes both readings.


class AmountParseError(ValueError):
    pass


def _tokenize(text):
    text = text.lower().replace("₹", " rs ").replace(",", "")
    # split "5k" / "2.5l" / "10cr" into number + scale
    text = re.sub(r"(\d)\s*(k|l|cr|lakh|lakhs|lac|crore|crores|hazaar|sau)\b", r"\1 \2", text)
    return [t for t in re.split(r"[^a-z0-9.]+", text) if t]


def parse_amount(text):
    """Return rupees as a Decimal. Raises AmountParseError if nothing parses."""
    total = Decimal(0)
    current = Decimal(0)
    pending = None
    seen_number = False

    for tok in _tokenize(text):
        if tok in NOISE:
            continue

        if tok in FRACTION_PREFIX:
            if pending is not None:
                raise AmountParseError("two fractional prefixes in a row: %r" % text)
            pending = FRACTION_PREFIX[tok]
            continue

        value = None
        if tok in STANDALONE_FRACTION:
            value = STANDALONE_FRACTION[tok]
        elif tok in WORDS:
            value = Decimal(WORDS[tok])
        elif re.fullmatch(r"\d+(\.\d+)?", tok):
            value = Decimal(tok)

        if value is not None:
            if pending is not None:
                value += pending
                pending = None
            current += value
            seen_number = True
            continue

        if tok in SCALES:
            scale = Decimal(SCALES[tok])
            base = current if current else Decimal(1)
            if pending is not None:          # "sava lakh" -> prefix applies to implicit 1
                base += pending
                pending = None
            seen_number = True
            if scale >= 1000:
                total += base * scale
                current = Decimal(0)
            else:                             # sau/hundred multiplies in place
                current = base * scale
            continue

        raise AmountParseError("unknown token %r in %r" % (tok, text))

    if pending is not None:
        raise AmountParseError("dangling fractional prefix in %r" % text)
    if not seen_number:
        raise AmountParseError("no amount found in %r" % text)
    return total + current


def to_paise(rupees):
    """Razorpay's wire unit. The single most common 100x error in agent code."""
    return int((Decimal(rupees) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def magnitude_bucket(called_paise, intended_paise):
    """Off-by ratio bucket. 'x100' is the paise trap; 'x0.01' is its inverse."""
    if intended_paise == 0:
        return "exact" if called_paise == 0 else "other"
    ratio = Decimal(called_paise) / Decimal(intended_paise)
    for label, target in (("x100", 100), ("x0.01", Decimal("0.01")),
                          ("x10", 10), ("x0.1", Decimal("0.1"))):
        if ratio == target:
            return label
    if ratio == 1:
        return "exact"
    if ratio > 1:
        return "over"
    return "under"
