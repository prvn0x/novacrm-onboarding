"""`novacrm`: run the agents, follow a deal, and the human exits from review.

novacrm run [--once]                 poll the inbox and advance every open deal
novacrm deals                        every deal and its state
novacrm trace D3                     one deal's audit trail, step by step
novacrm resolve D3 --tier growth --by priya
novacrm retry D3 --by priya [--not-duplicate]
novacrm close D3 --by priya --reason "duplicate of D1"
novacrm gmail-auth                   one-time Gmail consent in the browser
novacrm setup-voice                  create the Vapi assistant from config/vapi_assistant.json
novacrm check                        config and credentials, without changing anything
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .agents.base import Services
from .config import Settings, load_team, require_env
from .demo import names as demo_names
from .demo import run as run_demo
from .domain import Tier
from .faults import set_fault
from .orchestrator import Orchestrator, ReviewError
from .plan import load_plan
from .store import Store
from .trace import print_trail


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def _voice() -> Any:
    """VOICE_MODE=phone dials the AE; web opens the same assistant in the browser."""
    from .adapters.vapi import VapiCaller
    from .adapters.webcall import WebCaller

    if os.environ.get("VOICE_MODE", "phone") == "web":
        return WebCaller(require_env("VAPI_API_KEY"), require_env("VAPI_ASSISTANT_ID"))
    return VapiCaller(
        require_env("VAPI_API_KEY"),
        require_env("VAPI_ASSISTANT_ID"),
        require_env("VAPI_PHONE_NUMBER_ID"),
    )


def _services(settings: Settings) -> Services:
    from .adapters.console import ConsoleSlack
    from .adapters.gemini import GeminiLLM
    from .adapters.gmail import GmailInbox
    from .adapters.rocketlane import RocketlaneProjects
    from .adapters.slack import SlackWorkspace
    from .faults import Switchable

    chat: Any = (
        ConsoleSlack()
        if os.environ.get("SLACK_MODE", "real") == "console"
        else SlackWorkspace(require_env("SLACK_BOT_TOKEN"))
    )
    voice = _voice()
    return Services(
        store=Store(settings.db_path),
        inbox=Switchable("gmail", GmailInbox(require_env("GMAIL_CREDENTIALS"))),
        voice=Switchable("voice", voice),
        projects=Switchable(
            "rocketlane",
            RocketlaneProjects(
                require_env("ROCKETLANE_API_KEY"),
                base_url=os.environ.get(
                    "ROCKETLANE_BASE_URL", "https://api.rocketlane.com/api/1.0"
                ),
                app_url=os.environ.get("ROCKETLANE_APP_URL", "https://app.rocketlane.com"),
            ),
        ),
        chat=Switchable("slack", chat),
        llm=GeminiLLM(
            require_env("GEMINI_API_KEY"), os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
        ),
        clock=SystemClock(),
        team=load_team(settings.team_path),
        policy=settings.policy,
        sleep=time.sleep,
        plan=load_plan(),
    )


def _setup_voice() -> None:
    import httpx

    spec = json.loads(Path("config/vapi_assistant.json").read_text(encoding="utf-8"))
    response = httpx.post(
        "https://api.vapi.ai/assistant",
        json=spec,
        headers={"Authorization": f"Bearer {require_env('VAPI_API_KEY')}"},
        timeout=20,
    )
    response.raise_for_status()
    print(f"Created assistant {response.json()['id']}; put it in .env as VAPI_ASSISTANT_ID")


def _test_call(number: str) -> None:
    """Day-1 check: can the voice platform reach this number, and does the decision code read the
    answer right? Places one call, waits for it to end, prints the transcript and the decision."""
    from .adapters.gemini import GeminiLLM
    from .agents.tier import decide

    voice = _voice()
    call_id = voice.place_call(
        number, {"customer_name": "Acme Corp", "ae_first_name": "there", "deal": "test-call"}
    )
    print(f"calling {number} (call {call_id}); answer it and reply as the AE ...", flush=True)
    for _ in range(60):
        time.sleep(5)
        result = voice.get_result(call_id)
        if result is not None:
            break
    else:
        raise SystemExit("no result after 5 minutes")
    print(f"status: {result.status} ({result.detail})")
    for turn in result.turns:
        print(f"  {turn.speaker:>9}: {turn.text}")
    llm = GeminiLLM(
        require_env("GEMINI_API_KEY"), os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
    )
    decision = decide(result, llm)
    print(f"decision: {decision.outcome} {decision.tier or ''} - {decision.reason}")


def _check(settings: Settings) -> None:
    """Read-only checks that each account answers, before a demo run. Changes nothing."""
    team = load_team(settings.team_path)
    print(f"team: {len(team.account_executives)} AEs; config valid")
    services = _services(settings)
    checks: dict[str, Callable[[], object]] = {
        "rocketlane": lambda: services.projects.find_by_name("novacrm-connectivity-check"),
        "slack": lambda: services.chat.find_channel(team.escalation_channel),
        "gmail": lambda: services.inbox.fetch_new(),
        "gemini": lambda: services.llm.complete_text("check", "Reply with OK.", "OK?"),
        "vapi": services.voice.check,
    }
    failed = False
    for name, probe in checks.items():
        try:
            probe()
            print(f"{name}: ok")
        except Exception as exc:  # report every failure, then exit non-zero
            failed = True
            print(f"{name}: FAILED - {exc}")
    if failed:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="novacrm", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="poll the inbox and advance deals")
    run.add_argument("--once", action="store_true")
    sub.add_parser("deals")
    trace = sub.add_parser("trace")
    trace.add_argument("deal")
    resolve = sub.add_parser("resolve", help="a person sets the tier")
    resolve.add_argument("deal")
    resolve.add_argument("--tier", required=True, choices=[t.value for t in Tier])
    resolve.add_argument("--by", required=True)
    retry = sub.add_parser("retry", help="resume a deal after fixing the cause")
    retry.add_argument("deal")
    retry.add_argument("--by", required=True)
    retry.add_argument("--not-duplicate", action="store_true")
    close = sub.add_parser("close")
    close.add_argument("deal")
    close.add_argument("--by", required=True)
    close.add_argument("--reason", required=True)
    sub.add_parser("gmail-auth")
    sub.add_parser("setup-voice")
    sub.add_parser("check")
    vp = sub.add_parser("verify-platform", help="the day-1 Rocketlane checks (design doc §14)")
    vp.add_argument("--contact", required=True, help="an email you own, used as the test customer")
    vp.add_argument("--delete", action="store_true", help="delete the test projects afterwards")
    tc = sub.add_parser("test-call", help="one real call to a number, then the tier decision")
    tc.add_argument("--to", required=True, help="E.164, e.g. +9198XXXXXXXX (your own phone)")
    demo = sub.add_parser("demo", help="every scenario, offline, with no accounts")
    demo.add_argument("scenario", nargs="*", help=", ".join(demo_names()))
    fault = sub.add_parser("fault", help="switch a system down or up for the live demo")
    fault.add_argument("system", choices=["rocketlane", "slack", "voice", "gmail"])
    fault.add_argument("state", choices=["down", "up"])
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command in ("deals", "trace"):
        store = Store(settings.db_path)
        if args.command == "trace":
            print_trail(store, args.deal)
        else:
            for row in store.all_deals():
                customer = row.notification.customer_name if row.notification else row.subject
                print(f"{row.key:<5} {row.state:<16} {row.tier or '':<11} {customer}")
        return
    if args.command == "gmail-auth":
        from .adapters.gmail import authorize

        authorize(require_env("GMAIL_CREDENTIALS"), "var/gmail-token.json")
        print("Gmail authorised; token saved in var/gmail-token.json")
        return
    if args.command == "setup-voice":
        _setup_voice()
        return
    if args.command == "check":
        _check(settings)
        return
    if args.command == "verify-platform":
        from .adapters.rocketlane import RocketlaneProjects
        from .verify import verify

        api = RocketlaneProjects(
            require_env("ROCKETLANE_API_KEY"),
            base_url=os.environ.get("ROCKETLANE_BASE_URL", "https://api.rocketlane.com/api/1.0"),
        )
        verify(api, load_team(settings.team_path), args.contact, args.delete)
        return
    if args.command == "test-call":
        _test_call(args.to)
        return
    if args.command == "demo":
        run_demo(args.scenario)
        return
    if args.command == "fault":
        set_fault(args.system, args.state)
        print(f"{args.system} is {'DOWN' if args.state == 'down' else 'up'} for the running agent")
        return

    orchestrator = Orchestrator(_services(settings))
    try:
        if args.command == "resolve":
            deal: Any = orchestrator.resolve(args.deal, Tier(args.tier), args.by)
        elif args.command == "retry":
            deal = orchestrator.retry(args.deal, args.by, not_duplicate=args.not_duplicate)
        elif args.command == "close":
            deal = orchestrator.close(args.deal, args.by, args.reason)
        else:
            print(f"novacrm: watching the inbox every {settings.poll_seconds}s (Ctrl-C to stop)")
            while True:
                orchestrator.poll()
                if args.once:
                    return
                time.sleep(settings.poll_seconds)
    except ReviewError as exc:
        raise SystemExit(f"novacrm: {exc}") from exc
    except KeyboardInterrupt:
        return
    print(f"{deal.key} is now {deal.state}")


if __name__ == "__main__":
    main(sys.argv[1:])
