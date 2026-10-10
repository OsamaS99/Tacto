"""`arena` command line: register, list cases, play one case or a whole set, compare agents on
local cases (`eval`), show the board, run the MCP server. `run --set final` reads only the case
list, sessions and threads - never the board, results, events or screen (they are for people)."""

import argparse
import functools
import io
import json
import logging
import os
import statistics
import zipfile
from pathlib import Path

from .client import ArenaClient, ArenaError
from .play import RemoteTable, hosted, play, play_all, play_waves


def load_env(path=".env"):
    """Read KEY=VALUE lines from .env into the environment (real env vars win)."""
    p = Path(path)
    if p.is_file():
        for line in p.read_text().splitlines():
            key, sep, value = line.partition("=")
            value = value.strip().strip("\"'")
            if sep and value and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value)


AGENTS = ["naive", "llm", "buyer"]


def agent_factory(name, data, llm=True):
    from .agents import LLMAgent, NaiveAgent, llm_from_env

    if name == "naive":
        return NaiveAgent
    if name == "buyer":
        from .buyer import make_buyer_factory

        return make_buyer_factory(data, llm)
    if not llm:
        return NaiveAgent  # the legacy model baseline's deterministic fallback; no provider calls
    package = None
    if data:
        from .data import DataPackage

        package = DataPackage(data)
        package.lookup("")  # build the ID index once, before agents share it
    complete = llm_from_env()
    return lambda: LLMAgent(complete, package)


def table_factory(name):
    """`new_table(client, case=None)`: the buyer writes each move down before sending it (one Store
    for the run), so a restart re-sends it with its id; other agents use the plain table."""
    if name != "buyer":
        return RemoteTable
    from .buyer.store import Store
    from .buyer.transport import DurableTable

    return functools.partial(DurableTable, store=Store())


def summary(result):
    keep = ("deal", "supplier_id", "terms", "c_open", "c_deal", "c_best", "score")
    return {k: result.get(k) for k in keep if k in result}


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(prog="arena")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log", metavar="FILE", help="also write the log (INFO and up) to this file")
    verbose = argparse.ArgumentParser(add_help=False)  # -v and --log also after the command
    verbose.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    verbose.add_argument("--log", metavar="FILE", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser = functools.partial(sub.add_parser, parents=[verbose])
    r = sub.add_parser(
        "register", help="arena-sim only: get a simulator key (the arena's key comes from the organisers)"
    )
    r.add_argument("team_name")
    r.add_argument("event_code")
    data = {"default": os.environ.get("ARENA_DATA"), "help": "data package folder"}
    sets = {"default": "practice", "help": "practice, final or sandbox"}
    c = sub.add_parser("cases", help="list the cases you can play")
    c.add_argument("--set", **sets)
    p = sub.add_parser("play", help="play one case")
    p.add_argument("case_id")
    p.add_argument("--agent", default="naive", choices=AGENTS)
    p.add_argument("--data", **data)
    a = sub.add_parser("run", help="play every case of a set in parallel")
    a.add_argument("--set", **sets)
    a.add_argument("--agent", default="naive", choices=AGENTS)
    a.add_argument("--data", **data)
    a.add_argument("--parallel", type=int, default=8, help="cases at once (at most 10)")
    a.add_argument("--waves", type=int, default=5, help="final: stop after this many waves have closed")
    d = sub.add_parser("data", help="download and unpack the data (package, Sunday release once public)")
    d.add_argument("--out", default="data", help="folder to unpack into")
    e = sub.add_parser("eval", help="compare agents on local cases in an in-process arena-sim")
    e.add_argument("--agents", default="naive,buyer", help=f"comma-separated, of {', '.join(AGENTS)}")
    e.add_argument("--cases", default="eval_cases", help="folder of case files")
    e.add_argument("--repeat", type=int, default=1, help="plays per agent and case")
    e.add_argument("--clock", action="store_true", help="the 60-second rule and a wave's cap, as in the final")
    e.add_argument("--parallel", type=int, default=3, help="cases at once")
    e.add_argument("--data", **data)
    e.add_argument("--llm", action="store_true", help="alias for --buyer-llm")
    e.add_argument("--buyer-llm", action="store_true", help="enable buyer model calls")
    e.add_argument("--supplier-llm", action="store_true", help="enable persona model calls independently")
    e.add_argument("--supplier", default="script", choices=["script", "persona"], help="the simulated supplier")
    e.add_argument("--seed", type=int, default=0, help="persona: its hidden economics and dice")
    e.add_argument("--seeds", help="comma-separated persona seeds (overrides --seed)")
    z = sub.add_parser("analyze", help="read buyer run logs (runs/...) and name what went wrong")
    z.add_argument("paths", nargs="+", help="events/session files or folders")
    sub.add_parser("me", help="your team: practice quota used and left, best score per case")
    b = sub.add_parser("board", help="show the leaderboard")
    b.add_argument("--set", **sets)
    m = sub.add_parser("mcp", help="run the read-only MCP server over the data (stdio)")
    m.add_argument("--data", **data)
    args = ap.parse_args(argv)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO if args.verbose else logging.WARNING)
    handlers = [console]
    if args.log:  # the full INFO log to a file, whatever the console shows
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO if args.verbose or args.log else logging.WARNING,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    if args.cmd == "mcp":
        from .mcp_server import main as serve

        return serve(args.data)
    if args.cmd == "eval":  # local only: an in-process arena-sim, never the arena
        from .evaluate import cases_folder, evaluate, report

        names = [n.strip() for n in args.agents.split(",") if n.strip()]
        if unknown := set(names) - set(AGENTS):
            raise SystemExit(f"unknown agents: {sorted(unknown)}")
        buyer_llm = args.buyer_llm or args.llm
        factories = {n: agent_factory(n, args.data, buyer_llm) for n in names}
        facts, supplier = None, None
        if args.supplier == "persona":  # its economics come from the data; cited IDs are checked against it
            from .data import DataPackage
            from .persona import PersonaSupplier, persona_model

            package = DataPackage(args.data) if args.data else None
            facts = package.facts if package else None
            supplier = PersonaSupplier(package, args.seed, persona_model() if args.supplier_llm else None)
        folder = cases_folder(args.cases)
        try:
            seeds = [int(seed.strip()) for seed in args.seeds.split(",")] if args.seeds else [args.seed]
        except ValueError:
            raise SystemExit("--seeds must contain comma-separated integers") from None
        if not seeds:
            raise SystemExit("--seeds must contain at least one integer")
        out = evaluate(
            factories,
            folder,
            args.repeat,
            args.clock,
            max(1, args.parallel),
            facts,
            supplier=supplier,
            seeds=seeds,
            metadata={"buyer_llm": buyer_llm, "supplier_llm": args.supplier_llm},
        )
        print(report(out))
        return
    if args.cmd == "analyze":  # offline: the run's files, never the arena
        from .buyer.model import model_from_env
        from .buyer.replay import analyze

        print(json.dumps(analyze(args.paths, model_from_env()), indent=2, ensure_ascii=False))
        return
    client = ArenaClient()
    if args.cmd == "register":
        out = client.register(args.team_name, args.event_code)
        print(json.dumps(out, indent=2))
        print("\nA simulator key: put it into .env as ARENA_API_KEY=... while you point ARENA_URL at arena-sim.")
    elif args.cmd == "cases":
        cases = client.cases(args.set)
        playable = {c["case_id"] for c in hosted(cases)}
        for case in cases:
            mark = "" if case["case_id"] in playable else "  (not hosted: data only)"
            print(case["case_id"], case.get("type", ""), case.get("title", "") + mark, sep="  ")
    elif args.cmd == "me":
        try:
            print(json.dumps(client.me(), indent=2))
        except ArenaError as e:
            print(f"no team view on this arena ({e})" if e.status == 404 else e)
    elif args.cmd == "board":
        print(json.dumps(client.leaderboard(args.set), indent=2))
    elif args.cmd == "data":
        for name, info in client.downloads().items():
            if info.get("url"):
                zipfile.ZipFile(io.BytesIO(client.download(name))).extractall(args.out)
                print(f"{name}: {Path(args.out) / name}")
            else:
                print(f"{name}: public from {info.get('at')}")
    elif args.cmd == "play":
        set_name = "sandbox" if args.case_id.startswith("case:D") else "practice"
        case = next((c for c in client.cases(set_name) if c["case_id"] == args.case_id), None)
        table = table_factory(args.agent)(client, case)
        try:
            result = play(table, args.case_id, agent_factory(args.agent, args.data)())
        except ArenaError as e:  # e.g. not hosted, the quota, a paused key: the arena says why
            raise SystemExit(str(e)) from None
        print(json.dumps(summary(result), indent=2))
        replay = client.session(table.session_id).get("replay_url")
        if replay:  # the supplier's reasons, move by move: read it, improve, play again
            print(f"replay: {os.environ['ARENA_URL'].rstrip('/')}{replay}")
    elif args.set == "final":  # waits for each wave and plays its cases within the cap
        new_agent, new_table = agent_factory(args.agent, args.data), table_factory(args.agent)
        out = play_waves(client, new_agent, args.waves, min(args.parallel, 10), new_table=new_table)
    else:
        cases = hosted(client.cases(args.set))
        new_table = table_factory(args.agent)
        out = play_all(
            lambda: new_table(client),
            cases,
            agent_factory(args.agent, args.data),
            min(args.parallel, 10),
        )
    if args.cmd == "run":
        for case_id, result in out.items():
            print(case_id, result if isinstance(result, Exception) else summary(result))
        scores = {c: r.get("score") for c, r in out.items() if isinstance(r, dict)}
        if args.set == "final" and scores:  # points so far: each wave 0-100, their sum, as the board counts
            waves = {}
            for case in client.cases("final"):  # your own case list: which wave each case was in
                waves.setdefault(case["wave"], []).append(scores.get(case["case_id"]) or 0)
            points = {n: round(100 * statistics.mean(v), 1) for n, v in sorted(waves.items())}
            print(f"waves: {points}  points so far: {round(sum(points.values()), 1)} (the board is the record)")
        elif scores and all(v is not None for v in scores.values()):
            print(f"mean score: {100 * statistics.mean(scores.values()):.1f}")
    if args.cmd in ("play", "run") and client.practice_remaining is not None:
        print(f"practice sessions left today: {client.practice_remaining}")


if __name__ == "__main__":
    main()
