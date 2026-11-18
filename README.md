# Refi Check-in Agent

**Live demo:** https://chrischrisjnr.github.io/lendi-demo/

Reads a home loan CRM, finds customers coming up on a year since their last refinance,
texts them how their rate compares with the best on the market, and hands anyone who
replies to their broker with full context.

## Run it

```
python3 refi_agent.py --today 2026-11-18 scan                     # preview who gets texted and why
python3 refi_agent.py --today 2026-11-18 --trigger-months 10 scan # try a 10-month trigger
python3 refi_agent.py --today 2026-11-18 run                      # draft, check and "send"
python3 refi_agent.py reply C001 "Yes keen"                       # customer replies -> broker handover
python3 refi_agent.py report                                      # funnel + savings in pipeline
```

Drop `--today` to use the real date. Delete `outbox/` to start again.

## Settings (`config.json`)

| Setting | Default | What it does |
|---|---|---|
| `trigger_months` | 11 | Months since last refinance before a customer is texted |
| `best_market_rate` | 5.49 | Best rate quoted in the message |
| `min_rate_gap` | 0.25 | Only text if the customer's rate is at least this much higher |
| `cooldown_days` | 90 | Never re-text someone within this window |
| `quiet_hours` | 20:00–09:00 | No live sends overnight (Melbourne time) |
| `use_claude` | true | Personalise with Claude when `ANTHROPIC_API_KEY` is set; otherwise use the template |
| `send_mode` | dry_run | Set to `twilio` with `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` to send real SMS |

## Safeguards

- Only texts customers with SMS consent who haven't opted out; STOP is honoured at once.
- Every message, from Claude or the template, must pass checks before sending: the customer's
  name, both rates matching the CRM, a saving no higher than the calculated figure, the booking
  link, the opt-out line, no pushy claims, and a length under 320 characters. A Claude draft that
  fails is thrown away and the template is used.
- The agent never answers loan questions. Anything other than yes / no / stop goes to a broker.
- Every decision, sent or skipped, is written to `outbox/audit.jsonl` with the reason.

## CRM

`data/crm.csv` is sample data. To connect a real CRM, write an adapter like `CsvCRM` that
returns `Customer` records (HubSpot, Salesforce, or a broker platform's export).

## Demo pages

- **CRM Scanner** (`demo/`): adjust the trigger, scan the CRM, send texts, hand replies to a broker.
- **Conversation agent** (`v2/`): one customer's full chat. The opening text, a finance health check
  through open banking, three option cards, then the agent books a call with the broker using
  calendar free/busy access. Tap the replies in the phone, press Next, or turn on Auto-play.
  Live at https://chrischrisjnr.github.io/lendi-demo/v2/


`demo/index.html` is a self-contained visual version for screen recording. Serve it with
`python3 -m http.server -d demo` and open http://localhost:8000. The best rate is shown as
auto-filled from Lendi's rate database; in the demo it's a sample figure.

This is a concept demo made for a job application. It is not an official Lendi product and
isn't affiliated with or endorsed by Lendi Group. All customers, numbers and links are sample data.
