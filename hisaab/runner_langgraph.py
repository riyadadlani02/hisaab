"""The same eval, orchestrated by LangGraph instead of a hand-written loop.

Its only job is to answer one objection: *"you found that because of how you
built your loop."* Same tools, same system prompt, same scenarios, same guard,
same Action records — the graph is the only thing that changes. If the unit and
Indic failures show up here too, they are a property of the boundary, not of
`runner.py`.

The guard sits inside the tool node rather than wrapping the graph. That is
where it has to sit in a real integration too: by the time a framework hands
you a finished tool call, the model has already decided, and anything checking
after execution is a log, not a guard.
"""

import argparse
import json
import os
import sys

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph

from . import sim as simmod
from .guard import ALLOW, Session, wrap
from .metrics import Action, summarize, table
from .runner import MODEL, SYSTEM, TOOLS
from .scenarios import load
from .taxonomy import Tier, tier


# The eval's claim is about the boundary, not about one vendor. A second
# provider is the cleanest way to show that: if the same paise and Indic
# failures appear on both, they are a property of the interface between natural
# language and an API that counts in paise, not a quirk of one model family.
PROVIDERS = {"anthropic": MODEL, "openai": "gpt-4.1"}


def _model(model=None, provider="anthropic"):
    model = model or PROVIDERS[provider]
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, max_tokens=4096,
                          temperature=0).bind_tools(TOOLS)
    from langchain_anthropic import ChatAnthropic
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return ChatAnthropic(
        model=model, max_tokens=4096, thinking={"type": "adaptive"},
        default_headers={"anthropic-workspace-id": ws} if ws else None,
    ).bind_tools(TOOLS)


def build(llm, call, scenario, actions, turn_no):
    """A two-node graph: think, act, repeat until the model stops calling tools."""

    def agent(state):
        return {"messages": state["messages"] + [llm.invoke(state["messages"])]}

    def act(state):
        last = state["messages"][-1]
        out = []
        for tc in last.tool_calls:
            args = json.loads(json.dumps(tc["args"]))
            result, verdict = call(tc["name"], args)
            blocked = isinstance(result, dict) and str(
                result.get("error", {}).get("code", "")).startswith("HISAAB")
            actions.append(Action(
                scenario_id=scenario.id, tool=tc["name"], args=args, turn=turn_no,
                intended_paise=scenario.intended_paise,
                intended_entity=scenario.intended_entity,
                reversible_alternative=scenario.reversible_alternative,
                injected=scenario.injected and tier(tc["name"]) >= Tier.SEMI,
                guard_decision=(verdict.decision if verdict is not None else ALLOW),
                executed=not blocked, expect_tool=scenario.expect_tool))
            out.append(ToolMessage(content=json.dumps(result)[:4000],
                                   tool_call_id=tc["id"], status="error" if blocked else "success"))
        return {"messages": state["messages"] + out}

    g = StateGraph(dict)
    g.add_node("agent", agent)
    g.add_node("act", act)
    g.set_entry_point("agent")
    g.add_conditional_edges(
        "agent", lambda s: "act" if getattr(s["messages"][-1], "tool_calls", None) else END,
        {"act": "act", END: END})
    g.add_edge("act", "agent")
    return g.compile()


def run(scenario, llm, guard_on=True, approve_confirms=True, max_steps=8,
        backend=None):
    s = backend() if backend else getattr(simmod, scenario.fixture)()
    sess = Session()
    call = (wrap(s, sess, on_confirm=lambda v, t, a: approve_confirms)
            if guard_on else (lambda t, a: (s.call(t, a), None)))

    actions = []
    messages = [SystemMessage(content=SYSTEM)]
    for turn_no, utterance in enumerate(scenario.turns):
        sess.note_human(utterance)
        messages.append(HumanMessage(content=utterance))
        graph = build(llm, call, scenario, actions, turn_no)
        messages = graph.invoke({"messages": messages},
                                {"recursion_limit": max_steps * 2})["messages"]
        if not guard_on:
            for m in messages:
                if isinstance(m, ToolMessage):
                    try:
                        sess.note_tool_result(json.loads(m.content))
                    except ValueError:
                        pass
    return actions, messages



def _load_env():
    """Read ~/hisaab/.env if present. Keeps secrets off the command line and out
    of shell history — the harness never wants a key pasted anywhere it is
    logged. `.env` is gitignored."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

def main(argv=None):
    _load_env()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(prog="hisaab.runner_langgraph")
    ap.add_argument("--scenarios", default=os.path.join(root, "scenarios", "seed.jsonl"))
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default="anthropic")
    ap.add_argument("--model", default=None, help="override the provider default")
    ap.add_argument("--family")
    ap.add_argument("--out", default="runs/langgraph.json")
    ap.add_argument("--backend", choices=("sim", "sandbox"), default="sim",
                    help="sandbox = Razorpay Test Mode. unit/indic families only, "
                         "payment links and orders only, never a refund or payout.")
    a = ap.parse_args(argv)

    scenarios = [s for s in load(a.scenarios) if not a.family or s.family == a.family]
    backend = None
    if a.backend == "sandbox":
        from .rzp_sandbox import RzpSandbox, runnable
        scenarios = runnable(scenarios)
        live = RzpSandbox()                   # raises on a non-test key
        backend = lambda: live
        print("sandbox run %s: %d scenarios, links+orders only"
              % (live.run_id, len(scenarios)), file=sys.stderr)
    llm = _model(a.model, a.provider)
    out, raw = {}, {}
    for label, guard_on, approve in (("no_guard", False, True), ("hisaab", True, True),
                                     ("hisaab_unattended", True, False)):
        acts = []
        for sc in scenarios:
            acts.extend(run(sc, llm, guard_on=guard_on, approve_confirms=approve,
                            backend=backend)[0])
            print("  %-18s %-10s %d calls" % (label, sc.id, len(acts)), file=sys.stderr)
        out[label] = summarize(acts)
        raw[label] = [{"scenario": x.scenario_id, "tool": x.tool, "args": x.args,
                       "guard": x.guard_decision, "executed": x.executed,
                       "correct": x.correct} for x in acts]

    if backend is not None:
        cancelled = backend().cleanup()
        print("cancelled %d payment links created by this run" % len(cancelled),
              file=sys.stderr)

    print(table(out["no_guard"], out["hisaab"]))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        json.dump({"runner": "langgraph", "provider": a.provider,
                   "model": a.model or PROVIDERS[a.provider],
                   "n_scenarios": len(scenarios), "summary": out, "actions": raw},
                  f, indent=2)
    print("wrote %s" % a.out)


if __name__ == "__main__":
    main()
