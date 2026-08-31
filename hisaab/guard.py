"""hisaab guard — semantic middleware between an LLM agent and a payment tool.

Not a policy layer. Spend caps and category allowlists (which the existing
`razorpay-mcp-guard` on Glama already does well) answer "is this call within
budget?". They cannot answer the three questions that actually decide whether
money moves wrongly:

    1. Is this number in the unit the API expects?          -> unit sanity
    2. Did this identifier come from a human, or from text  -> provenance
       an attacker wrote into a dispute description?
    3. Is this action bound to the entity the conversation  -> entity binding
       was actually about?

Escalation is by reversibility tier, not by rupee value. A Rs 100 refund to the
wrong customer is worse than a Rs 50,000 payment link, because one is TERMINAL
and the other expires on its own.
"""

from dataclasses import dataclass, field
import re

from .amounts import extract_amounts, to_minor
from .taxonomy import (tier, Tier, AMOUNT_ARGS, ENTITY_ARGS, FREE_TEXT_KEYS)

ALLOW, CONFIRM, BLOCK = "allow", "confirm", "block"

# Razorpay identifiers are typed prefixes. Provenance tracking keys off them.
ID_RE = re.compile(r"\b((?:pay|order|cust|rfnd|plink|pout|disp|fa|qr)_[A-Za-z0-9]+)\b")

# Razorpay ids are typed by prefix. An order id in a payment_id field is not a
# lookup failure, it is the agent reaching for the wrong object entirely — and
# it is what "just refund this order" produces when no read happened first.
ID_PREFIX = {"payment_id": "pay_", "order_id": "order_", "customer_id": "cust_",
             "refund_id": "rfnd_", "fund_account_id": "fa_", "settlement_id": "setl_"}


@dataclass
class Verdict:
    decision: str
    reasons: list = field(default_factory=list)
    at_risk_paise: int = 0


@dataclass
class Session:
    """Everything the guard knows, accumulated turn by turn."""
    cap_paise: int = 10_000_000                 # Rs 1,00,000 per session
    spent_paise: int = 0
    subject: str = None                         # the entity the conversation is about
    from_human: set = field(default_factory=set)      # ids the user typed or said
    from_structured: set = field(default_factory=set) # ids from typed API fields
    from_free_text: set = field(default_factory=set)  # ids ever seen in attacker-writable text
    provenance: dict = field(default_factory=dict)    # id -> where it was FIRST seen
    anchors: dict = field(default_factory=dict)       # entity id -> amount in paise
    confirmed: set = field(default_factory=set)       # readback keys the human approved
    idempotency: set = field(default_factory=set)
    stated: list = field(default_factory=list)        # (span, decimal amount) the human said

    # ---- provenance intake ------------------------------------------
    def note_human(self, text):
        # The merchant's own words are the only ground truth available at
        # runtime for what the amount was supposed to be. A prior read anchors
        # the *ceiling*; only the utterance anchors the intent.
        for span, amount in extract_amounts(text or ""):
            self.stated.append((span, amount))
        for m in ID_RE.findall(text or ""):
            self.from_human.add(m)
            self.provenance[m] = "human"      # a person naming it is authoritative

            if self.subject is None and m.startswith(("order_", "pay_", "cust_")):
                self.subject = m

    def note_tool_result(self, result):
        """Split identifiers by where in the payload they appeared. An id that
        only ever appeared inside `notes`/`description` was written by whoever
        could write that field — which on a dispute is the disputing party."""
        self._walk(result, tainted=False)

    def _walk(self, node, tainted):
        if isinstance(node, dict):
            for k, v in node.items():
                self._walk(v, tainted or k in FREE_TEXT_KEYS)
                if not tainted and k in ("id",) + ENTITY_ARGS and isinstance(v, str):
                    self.from_structured.add(v)
                    self.provenance.setdefault(v, "structured")
                if not tainted and k == "amount" and isinstance(node.get("id"), str):
                    self.anchors[node["id"]] = int(v)
        elif isinstance(node, list):
            for v in node:
                self._walk(v, tainted)
        elif isinstance(node, str):
            for m in ID_RE.findall(node):
                (self.from_free_text if tainted else self.from_structured).add(m)
                self.provenance.setdefault(m, "free_text" if tainted else "structured")


def _entity_args(args):
    return [(k, v) for k, v in args.items() if k in ENTITY_ARGS and isinstance(v, str)]


def check(tool, args, sess):
    """The whole guard. Returns a Verdict; the caller decides what to do with
    CONFIRM (an eval run records it; a production integration asks a human)."""
    t = tier(tool)
    reasons = []
    decision = ALLOW
    amount = args.get("amount")
    amount = int(amount) if isinstance(amount, (int, float, str)) and str(amount).isdigit() else None
    at_risk = amount or 0

    def escalate(to, why):
        nonlocal decision
        reasons.append(why)
        if (to == BLOCK) or (to == CONFIRM and decision == ALLOW):
            decision = to

    if t is Tier.READ:
        return Verdict(ALLOW, [], 0)

    # --- provenance tainting -----------------------------------------
    # Applies at SEMI and TERMINAL. An id the model only ever saw inside
    # attacker-writable free text has no business in an irreversible call.
    if t >= Tier.SEMI:
        for key, val in _entity_args(args):
            # Taint is sticky, and it keys off where the id was FIRST seen.
            #
            # The earlier rule cleared taint whenever the id later turned up in
            # a structured field — and a live run showed an agent doing exactly
            # that by accident: it read a poisoned dispute, called
            # fetch_all_payments (which returns every id in a clean `id` field),
            # and the attacker's target came back laundered. One innocent list
            # call was enough. Agents make them constantly.
            #
            # A human naming the id still clears it, because a person asking for
            # something outranks where the agent first read the name.
            if sess.provenance.get(val) == "free_text":
                escalate(BLOCK, "tainted-arg: %s=%s was first seen in merchant/customer "
                                "free text; a later structured sighting does not clear that"
                         % (key, val))

    # --- argument type check ------------------------------------------
    if t >= Tier.SEMI:
        for key, val in _entity_args(args):
            want = ID_PREFIX.get(key)
            if want and not val.startswith(want):
                escalate(BLOCK, "wrong-object-type: %s=%s is not a %s id" % (key, val, want))

    # --- spoken-amount check ------------------------------------------
    # Needs no prior read, so it is the only unit check that works on the very
    # first call — and the only thing that catches a semantically wrong amount
    # ("paune do hazaar" heard as "do hazaar"), which no anchor can distinguish
    # from an ordinary partial refund.
    spoken_ok = False
    if amount is not None and sess.stated and t >= Tier.REVERSIBLE:
        cur = args.get("currency") or "INR"
        said = {to_minor(a, cur) for _, a in sess.stated}
        spoken_ok = amount in said
        if not spoken_ok:
            if any(amount * 100 == p or amount == p * 100 for p in said):
                escalate(BLOCK, "unit-vs-spoken: merchant said %s; call carries %d (%s minor units)"
                         % (" / ".join("%s = %d" % (sp, to_minor(a, cur)) for sp, a in sess.stated),
                            amount, cur))
            else:
                escalate(CONFIRM, "amount-mismatch: merchant said %s; call carries %d (%s minor units)"
                         % (" / ".join("%s = %d" % (sp, to_minor(a, cur)) for sp, a in sess.stated),
                            amount, cur))

    # --- entity binding ----------------------------------------------
    # Right action, wrong person. The failure nobody's success rate measures.
    # Deliberately not gated on a subject having been identified: an agent
    # handed a task with no named entity is exactly the case where an
    # unexplained id is most suspicious.
    if t >= Tier.SEMI:
        for key, val in _entity_args(args):
            if val not in sess.from_human and val not in sess.from_structured:
                escalate(BLOCK, "unbound-entity: %s=%s never appeared in this conversation" % (key, val))

    # --- unit sanity --------------------------------------------------
    # The paise trap, caught against the order/payment the call references.
    if amount is not None:
        anchor = next((sess.anchors[v] for _, v in _entity_args(args) if v in sess.anchors), None)
        if anchor and spoken_ok:
            # The merchant said this number and the call carries it. A ratio
            # coincidence against the anchor is not evidence against that: a
            # legitimate refund of exactly 1% of a payment is indistinguishable
            # from a paise/rupee slip by ratio alone. The utterance wins.
            if amount > anchor:
                escalate(BLOCK, "over-amount: %d exceeds the %d captured on the referenced entity" % (amount, anchor))
        elif anchor:
            if amount == anchor * 100:
                escalate(BLOCK, "unit-error: amount %d is 100x the anchor %d (rupees passed as paise?)" % (amount, anchor))
            elif amount * 100 == anchor:
                escalate(BLOCK, "unit-error: amount %d is 1/100 of the anchor %d (paise passed as rupees?)" % (amount, anchor))
            elif amount > anchor:
                escalate(BLOCK, "over-amount: %d exceeds the %d captured on the referenced entity" % (amount, anchor))
        elif t is Tier.TERMINAL:
            escalate(CONFIRM, "no-anchor: no read established a value for this entity before an irreversible call")

    # --- terminal-only checks -----------------------------------------
    if t is Tier.TERMINAL:
        key = "%s:%s:%s" % (tool, sorted(args.items()), amount)
        if key in sess.idempotency:
            escalate(BLOCK, "replay: identical terminal call already made this session")
        if sess.spent_paise + at_risk > sess.cap_paise:
            escalate(BLOCK, "cap: %d would take the session past its %d cap" % (at_risk, sess.cap_paise))
        if key not in sess.confirmed:
            escalate(CONFIRM, "readback: irreversible, needs an explicit human confirmation")

    return Verdict(decision, reasons, at_risk)


def readback(tool, args):
    """The sentence a human is asked to approve. Rupees, not paise — the
    readback is worthless if it repeats the model's unit mistake back at them."""
    amt = args.get("amount")
    who = next((v for k, v in args.items() if k in ENTITY_ARGS), "?")
    if amt is None:
        return "%s on %s" % (tool, who)
    return "%s: Rs %s on %s" % (tool, format(int(amt) / 100.0, ",.2f"), who)


def wrap(sim, sess, on_confirm=lambda v, tool, args: False):
    """Return a call(tool, args) that runs the guard first.

    `on_confirm` is the human. In an eval run it returns False and the refusal
    is recorded; in a real integration it prompts. Default is refuse — an
    unattended agent must not self-approve its own irreversible calls."""
    def call(tool, args):
        v = check(tool, args, sess)
        if v.decision == BLOCK:
            return {"error": {"code": "HISAAB_BLOCKED", "description": "; ".join(v.reasons)}}, v
        if v.decision == CONFIRM and not on_confirm(v, tool, args):
            return {"error": {"code": "HISAAB_NEEDS_CONFIRMATION",
                              "description": readback(tool, args) + " -- " + "; ".join(v.reasons)}}, v
        result = sim.call(tool, args)
        if tier(tool) is Tier.TERMINAL and "error" not in result:
            sess.spent_paise += v.at_risk_paise
            sess.idempotency.add("%s:%s:%s" % (tool, sorted(args.items()), args.get("amount")))
        sess.note_tool_result(result)
        return result, v
    return call
