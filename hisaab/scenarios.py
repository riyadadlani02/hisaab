"""Scenario schema and loader.

Hand-verification is the slow part and it is non-negotiable. Every scenario
carries `intended_paise` written by a human who read the utterance, not a value
produced by `amounts.parse_amount` — otherwise the corpus grades the model
against the same parser twice and proves nothing. `verify()` runs the parser as
a *second opinion* and shouts when the two disagree, which is how authoring
mistakes get caught.
"""

from dataclasses import dataclass, field, asdict
import json
import os

from .amounts import parse_amount, to_paise, AmountParseError

FAMILIES = ("unit", "indic", "entity", "reversibility", "injection", "drift")
LANGS = ("en", "hi", "hinglish")


@dataclass
class Scenario:
    id: str
    family: str
    lang: str
    turns: list                      # list of user utterances, in order
    intended_paise: int = None       # HAND-AUTHORED. Never generated.
    intended_entity: str = None
    amount_phrase: str = None   # the amount span alone; "" = derived, no spoken span
    unit: str = "rupees"        # "paise" when the merchant states the API's unit outright
    expect_tool: str = None          # the correct tool, if there is exactly one
    must_not_call: list = field(default_factory=list)
    reversible_alternative: str = None
    injected: bool = False
    fixture: str = "demo_merchant"
    verified: bool = True       # False = machine-composed, awaiting a human naturalness pass
    note: str = ""

    def verify(self):
        """Return a list of problems. Empty list = the scenario is well-formed."""
        problems = []
        if self.family not in FAMILIES:
            problems.append("unknown family %r" % self.family)
        if self.lang not in LANGS:
            problems.append("unknown lang %r" % self.lang)
        if not self.turns:
            problems.append("no turns")
        if self.family in ("unit", "indic") and self.intended_paise is None:
            problems.append("amount family with no intended_paise")
        if self.injected and self.family != "injection":
            problems.append("injected=True outside the injection family")
        if self.intended_paise is not None:
            if self.amount_phrase == "":
                return problems     # deliberate: the value is derived, not spoken
            for t in ([self.amount_phrase] if self.amount_phrase else self.turns):
                try:
                    second = to_paise(parse_amount(t))
                except AmountParseError:
                    continue
                if self.unit == "paise":
                    second = second // 100
                if second != self.intended_paise:
                    problems.append(
                        "parser disagrees with hand value on %r: %d vs %d"
                        % (t, second, self.intended_paise))
                break
        return problems


def load(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("//"):
                out.append(Scenario(**json.loads(line)))
        return out


def verify_all(path):
    bad = 0
    for s in load(path):
        for p in s.verify():
            print("%-22s %s" % (s.id, p))
            bad += 1
    print("%d scenarios, %d problems" % (len(load(path)), bad))
    return bad


if __name__ == "__main__":
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.exit(1 if verify_all(sys.argv[1] if len(sys.argv) > 1
                             else os.path.join(root, "scenarios", "seed.jsonl")) else 0)
