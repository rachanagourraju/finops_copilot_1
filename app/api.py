import logging
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pathlib import Path

from fastapi.responses import FileResponse, Response
from plotly.offline import get_plotlyjs
from pydantic import BaseModel, StringConstraints

from .agent import FinanceResponse
from .main import open_copilot

logger = logging.getLogger(__name__)


class AskRequest(BaseModel):
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    with open_copilot() as copilot:
        app.state.copilot = copilot
        yield


app = FastAPI(title="Finance Copilot", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}



@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(
        Path(__file__).with_name("dashboard.html"),
        media_type="text/html",
    )


@app.get("/assets/plotly.min.js", include_in_schema=False)
def plotly_bundle() -> Response:
    return Response(
        get_plotlyjs(),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.post("/api/v1/analytics/ask", response_model=FinanceResponse)
def ask(payload: AskRequest, request: Request) -> FinanceResponse:
    request_id = uuid4().hex
    try:
        result = request.app.state.copilot.ask(payload.question)
    except Exception as exc:
        logger.error("Copilot failed request=%s error=%s", request_id, type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail={"message": "Finance request failed.", "request_id": request_id},
        ) from None

    logger.info("Copilot completed request=%s status=%s", request_id, result.status)
    return result