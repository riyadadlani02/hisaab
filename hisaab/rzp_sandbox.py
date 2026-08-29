"""Razorpay Test Mode backend — same call interface as `Sim`, real API behind it.

Why this exists: the paise boundary is the finding, and the strongest evidence
for it is the *real* endpoint accepting a wrong number. A simulator can always
be accused of encoding the bug it claims to find.

Why it is heavily fenced: a test key is still a key, and an agent under test is
by definition doing the wrong thing. So the fence is structural, not
procedural — this class cannot express a refund, a payout, or a settlement.
There is no code path to one. The guard is a second line here, not the only one.

  ALLOWED   create_payment_link, fetch_payment_link, fetch_all_payment_links,
            update_payment_link (cancel), create_order, fetch_order,
            fetch_all_orders
  REFUSED   everything else, including every Tier.TERMINAL tool, by whitelist
            rather than blacklist — a tool added later is refused by default

Three more locks:

  * the key must start with `rzp_test_`. A live key raises before any request.
  * only `unit` and `indic` scenarios may run here. The injection family stays
    on the simulator, permanently — see DISCLOSURE.md.
  * every object created is tagged with the run id and cancelled on the way out.

Payment links are the right surface for this. They carry an `amount` in paise —
the exact boundary under test — and an unpaid link expires on its own, which is
what REVERSIBLE means in the taxonomy.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.razorpay.com/v1"

ALLOWED = {
    "create_payment_link": ("POST", "/payment_links"),
    "fetch_payment_link": ("GET", "/payment_links/{id}"),
    "fetch_all_payment_links": ("GET", "/payment_links"),
    "update_payment_link": ("POST", "/payment_links/{id}/cancel"),
    "create_order": ("POST", "/orders"),
    "fetch_order": ("GET", "/orders/{id}"),
    "fetch_all_orders": ("GET", "/orders"),
}

RUNNABLE_FAMILIES = ("unit", "indic")


class LiveKeyRefused(RuntimeError):
    pass


class RzpSandbox:
    """Duck-types `Sim`: `.call(tool, args)`, `.calls`, `.payments`, `.orders`."""

    def __init__(self, key_id=None, key_secret=None, dry_run=False):
        self.key_id = key_id or os.environ.get("RAZORPAY_KEY_ID", "")
        self.key_secret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET", "")
        self.dry_run = dry_run
        if not self.dry_run:
            if not self.key_id.startswith("rzp_test_"):
                raise LiveKeyRefused(
                    "RAZORPAY_KEY_ID must be a test key (rzp_test_...). Refusing to "
                    "run an agent eval against anything else.")
            if not self.key_secret:
                raise LiveKeyRefused("RAZORPAY_KEY_SECRET is not set.")
        self.run_id = "hisaab-%d" % int(time.time())
        self.calls = []
        self.created = []          # payment_link ids, for cleanup
        self.payments = {}         # always empty: no captured payments exist here
        self.orders = {}
        self.disputes = {}
        self.customers = {}
        self.refunds = {}

    # ---- transport ---------------------------------------------------
    def _request(self, method, path, body=None):
        if self.dry_run:
            return {"_dry_run": True, "method": method, "path": path, "body": body}
        token = base64.b64encode(
            ("%s:%s" % (self.key_id, self.key_secret)).encode()).decode()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            API + path, data=data, method=method,
            headers={"Authorization": "Basic " + token,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return {"error": json.loads(e.read().decode() or "{}").get(
                "error", {"description": str(e)})}
        except urllib.error.URLError as e:
            return {"error": {"code": "NETWORK", "description": str(e)}}

    # ---- the Sim interface -------------------------------------------
    def call(self, tool, args):
        self.calls.append({"tool": tool, "args": dict(args)})
        spec = ALLOWED.get(tool)
        if spec is None:
            # Whitelist, not blacklist: a tool shipped next quarter is refused
            # here without anyone remembering to add it to a deny list.
            return {"error": {
                "code": "SANDBOX_REFUSED",
                "description": "%s is not permitted against a Razorpay sandbox from "
                               "this harness. Allowed: %s"
                               % (tool, ", ".join(sorted(ALLOWED)))}}
        method, path = spec
        body = dict(args)
        if "{id}" in path:
            ident = (body.pop("payment_link_id", None) or body.pop("order_id", None)
                     or body.pop("id", None))
            if not ident:
                return {"error": {"code": "BAD_ARGS", "description": "no id given"}}
            path = path.replace("{id}", str(ident))
            body = body or None
        if method == "POST" and body is not None:
            body.setdefault("currency", "INR")
            notes = dict(body.get("notes") or {})
            notes["hisaab_run"] = self.run_id
            body["notes"] = notes
            body.pop("status", None)          # cancel takes no body fields
        if method == "GET":
            body = None

        out = self._request(method, path, body)
        if tool == "create_payment_link" and isinstance(out, dict) and out.get("id"):
            self.created.append(out["id"])
        if tool == "create_order" and isinstance(out, dict) and out.get("id"):
            self.orders[out["id"]] = out
        return out

    def cleanup(self):
        """Cancel every link this run created. Unpaid links expire anyway; not
        relying on that is the difference between tidy and lucky."""
        done = []
        for pid in self.created:
            done.append((pid, self._request("POST", "/payment_links/%s/cancel" % pid)))
        return done


def runnable(scenarios):
    """Only the families that carry no irreversible intent. The injection family
    never runs here, in any configuration."""
    return [s for s in scenarios
            if s.family in RUNNABLE_FAMILIES
            and s.expect_tool in ALLOWED
            and not s.injected]
