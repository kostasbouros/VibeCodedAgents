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
