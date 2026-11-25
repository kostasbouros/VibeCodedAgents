"""Fake external APIs for local development and tests. Never talks to real systems.

    uvicorn mock_api.server:app --port 8099

- Mobility (3dtracking Client WebApi v1.0): /api/v1.0/...  (log in with mock / mock)
- /health: simple check that the server is running
"""

from fastapi import FastAPI

from mock_api.mobility.routes import router as mobility_router

app = FastAPI(title="G4S Telematix mock APIs")
app.include_router(mobility_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Simple check that the mock server is running."""
    return {"status": "ok"}
