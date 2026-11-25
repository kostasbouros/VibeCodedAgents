# G4S Telematix AI Agents

A set of small AI helpers for G4S Telematix. Each one does one everyday job: sorting support tickets, watching GPS devices, checking installations, or writing fleet reports. They all share the same code for talking to our systems (Zoho Desk, Zoho CRM, the Mobility GPS platform) and to Claude, the AI model behind them.

## The agents

| Folder | What it does |
|---|---|
| `agents/desk_triage` | Reads new Zoho Desk tickets and suggests a category, priority and first reply. |
| `agents/device_watchdog` | Spots GPS devices that have gone silent or are reporting strange data. |
| `agents/install_verifier` | Checks that a newly installed device is actually sending data correctly. |
| `agents/install_qa` | Reviews completed installations against our quality checklist. |
| `agents/fleet_report` | Writes plain-language fleet reports for customers. |
| `agents/account_signals` | Flags customer accounts in Zoho CRM that need attention (risk or opportunity). |

## What's in each folder

- `shared/`: code every agent uses (settings, logging, calling Claude).
- `connectors/`: code that talks to Zoho Desk, Zoho CRM and Mobility.
- `mock_api/`: a fake version of those systems, so you can try things without real accounts.
- `config/`: settings files (thresholds, schedules).
- `prompts/`: the instructions we give Claude for each agent.
- `guides/`: how-to notes for people using the agents.
- `docs/approved/`: company documents the agents may quote from.
- `evals/`: sample cases for checking that the agents give good answers.
- `tests/`: automated checks for the code.
- `data/`, `reports/`, `logs/`: created while the agents run. Not saved to git.
- `deploy/`: files for running the agents on a server.

## Setting it up

1. Install **Python 3.12** from python.org.
2. Open a terminal in this folder and create a private Python environment:
   ```
   python -m venv .venv
   .venv\Scripts\activate        (Windows)
   source .venv/bin/activate     (Mac/Linux)
   ```
3. Install the project and its tools:
   ```
   pip install -e ".[dev]"
   ```
4. Copy `.env.example` to a new file called `.env` and fill in the values. **Never share or commit `.env`**: it holds passwords and keys.
5. To try things without real Zoho or Mobility accounts, set `USE_MOCKS=true` in `.env`.

## Running the tests

```
python -m pytest
```

## Mock APIs (no real systems needed)

Start the fake systems on your computer:

```
uvicorn mock_api.server:app --port 8099
```

Then open http://localhost:8099/docs in a browser to see and try every endpoint.

- **Mobility (3dtracking WebApi v1.0)** at `http://localhost:8099/api/v1.0/...`.
  Log in with `username=mock&password=mock`, then pass the `UserIdGuid` and `SessionId` you
  get back on every call, exactly like the real API. Field names follow the vendor's published
  spec and a real response. The data has 20 made-up vehicles (`VEH_001`...`VEH_020`) with
  problems planted on purpose (offline units, low battery, jamming, a power cut, deep sleep
  and a carrier outage). Times move with the clock, so those problems always look current.
- To rebuild the mock data: `python -m mock_api.mobility.generate` (always gives the same files).

## Reading from Mobility in code

`connectors/mobility.py` is a read-only client. It logs in, renews the session by itself and
never writes anything to the platform. With `USE_MOCKS=true` it talks to the mock above:

```python
from connectors.mobility import get_mobility_client

with get_mobility_client() as mobility:
    for unit in mobility.latest_positions():
        print(unit.name, unit.last_reported_time_utc)
    positions, next_start_id, is_current = mobility.positions(start_id=0)
```
