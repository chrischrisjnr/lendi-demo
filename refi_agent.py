#!/usr/bin/env python3
"""Refinance check-in agent.

Reads customers from a CRM, finds the ones coming up on a year since their
last refinance, texts them the gap between their rate and the best rate on
the market, and hands anyone who replies over to their broker.

Commands:
  scan    Show who would be contacted and why (sends nothing)
  run     Draft, check and send the messages
  reply   Feed in a customer's SMS reply:  reply C001 "Yes keen"
  report  Funnel and pipeline numbers

Only the Python standard library is required. If ANTHROPIC_API_KEY is set and
the `anthropic` package is installed, Claude personalises each message;
otherwise a fixed template is used. Every message passes the same checks
either way.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outbox"
AUDIT_LOG = OUT / "audit.jsonl"
SENT_LOG = OUT / "sent.jsonl"
STATE_FILE = OUT / "state.json"
HANDOVERS = OUT / "handovers.jsonl"

OPT_OUT_TEXT = "Reply STOP to opt out."
MAX_SMS_CHARS = 320  # two SMS segments
BANNED_PHRASES = ["guarantee", "guaranteed", "risk-free", "no risk", "approved", "pre-approved", "act now", "urgent"]


# ---------------------------------------------------------------- config / data

def load_config(path: Path) -> dict:
    return json.loads(path.read_text())


@dataclass
class Customer:
    customer_id: str
    first_name: str
    last_name: str
    mobile: str
    email: str
    lender: str
    loan_balance: float
    current_rate: float
    last_refi_date: date
    remaining_years: int
    broker_name: str
    broker_booking_url: str
    sms_consent: bool
    opted_out: bool
    last_contacted: date | None


def _bool(v: str) -> bool:
    return str(v).strip().lower() in {"true", "yes", "1", "y"}


def _date(v: str) -> date | None:
    v = (v or "").strip()
    return date.fromisoformat(v) if v else None


class CsvCRM:
    """CRM adapter for a CSV export. Swap for a HubSpot/Salesforce adapter
    that returns the same Customer objects."""

    def __init__(self, path: Path):
        self.path = path

    def customers(self) -> list[Customer]:
        with self.path.open(newline="") as f:
            rows = list(csv.DictReader(f))
        return [
            Customer(
                customer_id=r["customer_id"],
                first_name=r["first_name"],
                last_name=r["last_name"],
                mobile=r["mobile"],
                email=r["email"],
                lender=r["lender"],
                loan_balance=float(r["loan_balance"]),
                current_rate=float(r["current_rate"]),
                last_refi_date=_date(r["last_refi_date"]),
                remaining_years=int(r["remaining_years"]),
                broker_name=r["broker_name"],
                broker_booking_url=r["broker_booking_url"],
                sms_consent=_bool(r["sms_consent"]),
                opted_out=_bool(r["opted_out"]),
                last_contacted=_date(r.get("last_contacted", "")),
            )
            for r in rows
        ]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"contacted": {}, "opted_out": [], "handed_over": []}


def save_state(state: dict) -> None:
    OUT.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def append_jsonl(path: Path, record: dict) -> None:
    OUT.mkdir(exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ---------------------------------------------------------------- the maths

def months_since(d: date, today: date) -> float:
    return (today - d).days / 30.4375


def monthly_repayment(balance: float, annual_rate_pct: float, years: int) -> float:
    r = annual_rate_pct / 100 / 12
    n = years * 12
    if r == 0:
        return balance / n
    return balance * r / (1 - (1 + r) ** -n)


def monthly_saving(c: Customer, best_rate: float) -> int:
    now = monthly_repayment(c.loan_balance, c.current_rate, c.remaining_years)
    new = monthly_repayment(c.loan_balance, best_rate, c.remaining_years)
    # Round down to the nearest $10 so we never overstate the saving.
    return max(0, int((now - new) // 10 * 10))


# ---------------------------------------------------------------- decisions

@dataclass
class Decision:
    customer: Customer
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    months_since_refi: float = 0.0
    saving: int = 0


def evaluate(c: Customer, cfg: dict, state: dict, today: date) -> Decision:
    d = Decision(customer=c, eligible=True)
    d.months_since_refi = round(months_since(c.last_refi_date, today), 1)
    d.saving = monthly_saving(c, cfg["best_market_rate"])

    def skip(reason: str):
        d.eligible = False
        d.reasons.append(reason)

    if not c.sms_consent:
        skip("no SMS consent on file")
    if c.opted_out or c.customer_id in state["opted_out"]:
        skip("customer opted out")
    if c.customer_id in state["handed_over"]:
        skip("already with a broker")
    if d.months_since_refi < cfg["trigger_months"]:
        skip(f"only {d.months_since_refi} months since last refinance (trigger is {cfg['trigger_months']:g})")
    gap = round(c.current_rate - cfg["best_market_rate"], 2)
    if gap < cfg["min_rate_gap"]:
        skip(f"rate gap {gap:.2f}% is below the {cfg['min_rate_gap']:.2f}% minimum")

    last = c.last_contacted
    if c.customer_id in state["contacted"]:
        s = date.fromisoformat(state["contacted"][c.customer_id])
        last = max(last, s) if last else s
    if last and (today - last).days < cfg["cooldown_days"]:
        skip(f"contacted {(today - last).days} days ago (cooldown {cfg['cooldown_days']} days)")

    if d.eligible:
        d.reasons.append(
            f"{d.months_since_refi} months since refinance, on {c.current_rate:.2f}% vs best {cfg['best_market_rate']:.2f}%"
        )
    return d


# ---------------------------------------------------------------- messages

def template_message(c: Customer, cfg: dict, saving: int) -> str:
    save_line = f" It could save you around ${saving:,} a month." if saving >= 50 else ""
    return (
        f"Hey {c.first_name}, the best rate going around right now is {cfg['best_market_rate']:.2f}%. "
        f"You're currently on {c.current_rate:.2f}%, which is higher. "
        f"Let's talk about a refinance and how it can help you save some money.{save_line} "
        f"Book a chat with {c.broker_name.split()[0]}: {c.broker_booking_url} {OPT_OUT_TEXT}"
    )


def claude_message(c: Customer, cfg: dict, saving: int, months: float) -> str | None:
    if not cfg.get("use_claude") or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic()
    prompt = f"""Write one SMS from a home loan broker team to an existing customer.

Facts (use exactly, do not invent others):
- First name: {c.first_name}
- Best rate available now: {cfg['best_market_rate']:.2f}%
- Their current rate: {c.current_rate:.2f}%
- Months since their last refinance: {int(months)}
- Estimated monthly saving: ${saving:,} (say "around", never more than this)
- Broker first name: {c.broker_name.split()[0]}
- Booking link: {c.broker_booking_url}

Rules:
- Open with "Hey {c.first_name}," then say the best rate going around right now, that they're on a higher rate, and invite a refinance chat to save money.
- Friendly, plain Australian English, no emojis, no hype, no promises of approval.
- End with the booking link, then exactly: "{OPT_OUT_TEXT}"
- Under 300 characters in total.
Reply with the SMS text only."""
    resp = client.messages.create(
        model=cfg.get("claude_model", "claude-haiku-5-5"),
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def check_message(msg: str, c: Customer, cfg: dict, saving: int) -> list[str]:
    """Guardrails every outgoing message must pass. Returns problems found."""
    problems = []
    if c.first_name not in msg:
        problems.append("missing customer's name")
    if f"{cfg['best_market_rate']:.2f}%" not in msg:
        problems.append("best rate missing or wrong")
    if f"{c.current_rate:.2f}%" not in msg:
        problems.append("current rate missing or wrong")
    if c.broker_booking_url not in msg:
        problems.append("booking link missing")
    if not msg.rstrip().endswith(OPT_OUT_TEXT):
        problems.append("opt-out text missing")
    if len(msg) > MAX_SMS_CHARS:
        problems.append(f"too long ({len(msg)} chars)")
    lower = msg.lower()
    for p in BANNED_PHRASES:
        if re.search(rf"\b{re.escape(p)}\b", lower):
            problems.append(f"banned phrase: {p}")
    rates = {f"{float(x):.2f}" for x in re.findall(r"(\d+\.\d+)%", msg)}
    allowed = {f"{cfg['best_market_rate']:.2f}", f"{c.current_rate:.2f}"}
    if rates - allowed:
        problems.append(f"unknown rate(s) quoted: {sorted(rates - allowed)}")
    for amt in re.findall(r"\$([\d,]+)", msg):
        if int(amt.replace(",", "")) > saving:
            problems.append(f"overstated saving ${amt}")
    return problems


def draft(c: Customer, cfg: dict, d: Decision) -> tuple[str, str, list[str]]:
    """Returns (message, source, problems_with_rejected_drafts)."""
    rejected = []
    try:
        ai = claude_message(c, cfg, d.saving, d.months_since_refi)
    except Exception as e:  # network/API error: fall back, don't fail the run
        ai, rejected = None, [f"claude error: {e}"]
    if ai:
        problems = check_message(ai, c, cfg, d.saving)
        if not problems:
            return ai, "claude", []
        rejected = problems
    msg = template_message(c, cfg, d.saving)
    problems = check_message(msg, c, cfg, d.saving)
    if problems:
        raise ValueError(f"template failed checks for {c.customer_id}: {problems}")
    return msg, "template", rejected


# ---------------------------------------------------------------- sending

def in_quiet_hours(cfg: dict) -> bool:
    q = cfg["quiet_hours"]
    now = datetime.now(ZoneInfo(q["timezone"])).time()
    start = datetime.strptime(q["start"], "%H:%M").time()
    end = datetime.strptime(q["end"], "%H:%M").time()
    return now >= start or now < end if start > end else start <= now < end


def send_sms(to: str, body: str, cfg: dict) -> str:
    if cfg["send_mode"] != "twilio":
        return "dry_run"
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    sender = os.environ["TWILIO_FROM"]
    data = urllib.parse.urlencode({"To": to, "From": sender, "Body": body}).encode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data
    )
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["sid"]


# ---------------------------------------------------------------- replies

STOP_WORDS = {"stop", "unsubscribe", "opt out", "optout", "cancel", "remove me"}
YES_WORDS = {"yes", "yep", "yeah", "keen", "sure", "ok", "okay", "call me", "book", "interested", "sounds good", "please"}


def classify_reply(text: str) -> str:
    t = text.strip().lower()
    if any(w in t for w in STOP_WORDS):
        return "opt_out"
    if any(re.search(rf"\b{re.escape(w)}\b", t) for w in YES_WORDS):
        return "interested"
    if t in {"no", "nah", "no thanks", "not now"}:
        return "declined"
    return "question"  # anything else goes to a human; the agent never gives loan advice


# ---------------------------------------------------------------- commands

def get_ctx(args):
    cfg = load_config(ROOT / args.config)
    if args.trigger_months is not None:
        cfg["trigger_months"] = args.trigger_months
    if args.best_rate is not None:
        cfg["best_market_rate"] = args.best_rate
    crm = CsvCRM(ROOT / cfg["crm_source"])
    today = date.fromisoformat(args.today) if args.today else date.today()
    return cfg, crm, load_state(), today


def cmd_scan(args, send=False):
    cfg, crm, state, today = get_ctx(args)
    decisions = [evaluate(c, cfg, state, today) for c in crm.customers()]
    eligible = [d for d in decisions if d.eligible][: cfg["max_per_run"]]
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    print(f"\nTrigger: {cfg['trigger_months']:g}+ months since last refinance | best rate {cfg['best_market_rate']:.2f}% | "
          f"min gap {cfg['min_rate_gap']:.2f}% | mode {cfg['send_mode'] if send else 'preview'}\n")
    for d in decisions:
        c = d.customer
        mark = "SEND" if d.eligible else "skip"
        print(f"[{mark}] {c.customer_id} {c.first_name} {c.last_name:<10} {c.current_rate:.2f}%  "
              f"{d.months_since_refi:>5} mo since refi  saves ~${d.saving:,}/mo  - {'; '.join(d.reasons)}")
        append_jsonl(AUDIT_LOG, {"run": run_id, "at": datetime.now().isoformat(), "customer": c.customer_id,
                                 "eligible": d.eligible, "reasons": d.reasons, "config": {
                                     k: cfg[k] for k in ("trigger_months", "best_market_rate", "min_rate_gap")}})

    if send and cfg["send_mode"] == "twilio" and in_quiet_hours(cfg) and not args.ignore_quiet_hours:
        print("\nInside quiet hours, so nothing was sent. Run again in business hours.")
        return

    print(f"\n{len(eligible)} of {len(decisions)} customers in the refinance window.\n")
    for d in eligible:
        c = d.customer
        msg, source, rejected = draft(c, cfg, d)
        print(f"--> {c.first_name} ({c.mobile}) [{source}, {len(msg)} chars]\n    {msg}\n")
        if rejected:
            print(f"    (a Claude draft was rejected: {'; '.join(rejected)})\n")
        if send:
            sid = send_sms(c.mobile, msg, cfg)
            state["contacted"][c.customer_id] = today.isoformat()
            append_jsonl(SENT_LOG, {"run": run_id, "at": datetime.now().isoformat(), "customer": c.customer_id,
                                    "to": c.mobile, "body": msg, "source": source, "sid": sid,
                                    "saving": d.saving, "rejected_drafts": rejected})
    if send:
        save_state(state)
        print(f"Logged to {SENT_LOG.relative_to(ROOT)} and {AUDIT_LOG.relative_to(ROOT)}")


def cmd_reply(args):
    cfg, crm, state, today = get_ctx(args)
    c = next((x for x in crm.customers() if x.customer_id == args.customer_id), None)
    if not c:
        sys.exit(f"No customer {args.customer_id}")
    intent = classify_reply(args.text)
    saving = monthly_saving(c, cfg["best_market_rate"])
    broker = c.broker_name.split()[0]

    if intent == "opt_out":
        state["opted_out"].append(c.customer_id)
        out = "You're unsubscribed and won't hear from us about rates again."
    elif intent == "interested":
        state["handed_over"].append(c.customer_id)
        out = f"Great, {c.first_name}. {broker} will call you within one business day, or pick a time here: {c.broker_booking_url}"
    elif intent == "declined":
        out = f"No worries, {c.first_name}. We'll check in again down the track."
    else:
        state["handed_over"].append(c.customer_id)
        out = f"Good question, {c.first_name}. I've passed it to {broker}, who'll get back to you today."

    if intent in {"interested", "question"}:
        note = {
            "at": datetime.now().isoformat(), "customer": c.customer_id,
            "name": f"{c.first_name} {c.last_name}", "mobile": c.mobile, "broker": c.broker_name,
            "reason": "wants a refinance chat" if intent == "interested" else "asked a question",
            "customer_said": args.text,
            "context": {
                "lender": c.lender, "balance": c.loan_balance, "current_rate": c.current_rate,
                "best_market_rate": cfg["best_market_rate"], "est_monthly_saving": saving,
                "months_since_refi": round(months_since(c.last_refi_date, today), 1),
                "remaining_years": c.remaining_years,
            },
        }
        append_jsonl(HANDOVERS, note)
        print(f"\nHANDOVER to {c.broker_name}:\n{json.dumps(note, indent=2)}\n")

    send_sms(c.mobile, out, cfg)
    append_jsonl(SENT_LOG, {"at": datetime.now().isoformat(), "customer": c.customer_id, "to": c.mobile,
                            "body": out, "source": "reply", "intent": intent, "inbound": args.text})
    save_state(state)
    print(f"Customer: {args.text}\nIntent:   {intent}\nAgent:    {out}")


def cmd_report(args):
    cfg, crm, state, today = get_ctx(args)
    customers = {c.customer_id: c for c in crm.customers()}
    sent = [json.loads(l) for l in SENT_LOG.read_text().splitlines()] if SENT_LOG.exists() else []
    outreach = [s for s in sent if s.get("source") in {"claude", "template"}]
    replies = [s for s in sent if s.get("source") == "reply"]
    handed = set(state["handed_over"])
    pipeline = sum(monthly_saving(customers[i], cfg["best_market_rate"]) for i in handed if i in customers)
    n = len(outreach) or 1
    print(f"""
Customers in CRM:        {len(customers)}
Messages sent:           {len(outreach)}
Replies:                 {len(replies)}  ({len(replies) / n:.0%})
Handed to a broker:      {len(handed)}  ({len(handed) / n:.0%})
Opted out:               {len(state['opted_out'])}
Claude vs template:      {sum(s['source'] == 'claude' for s in outreach)} / {sum(s['source'] == 'template' for s in outreach)}
Customer savings in pipeline: ~${pipeline:,}/month
""")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.json")
    p.add_argument("--trigger-months", type=float, help="months since last refinance before a customer is texted, e.g. 10")
    p.add_argument("--best-rate", type=float, help="override best market rate, e.g. 5.49")
    p.add_argument("--today", help="pretend today is YYYY-MM-DD (for demos)")
    p.add_argument("--ignore-quiet-hours", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("run")
    r = sub.add_parser("reply")
    r.add_argument("customer_id")
    r.add_argument("text")
    sub.add_parser("report")
    args = p.parse_args()
    {"scan": lambda a: cmd_scan(a), "run": lambda a: cmd_scan(a, send=True),
     "reply": cmd_reply, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
