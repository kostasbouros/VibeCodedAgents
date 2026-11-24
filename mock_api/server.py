"""Fake Zoho Desk / Zoho CRM / Mobility APIs for local testing. Run:
uvicorn mock_api.server:app --port 8099
"""

from fastapi import FastAPI

app = FastAPI(title="G4S Telematix mock APIs")


@app.get("/health")
def health() -> dict[str, str]:
    """Simple check that the mock server is running."""
    return {"status": "ok"}
