"""Research HTTP surface; Agent and CLI use the same store and engine."""

from fastapi import APIRouter, HTTPException, Query

from trade_compass_agent.config import load_app_config
from trade_compass_agent.evaluation.research import (
    ResearchRequest, ResearchStore, StrategyDefinition, run_strategy_research,
)
from trade_compass_agent.runtime.market_stack import MarketStack

router = APIRouter(prefix="/research", tags=["research"])


def _store() -> ResearchStore:
    return ResearchStore(load_app_config().data_dir)


@router.get("/strategies")
def list_strategies():
    return {"strategies": _store().list_strategies()}


@router.post("/strategies")
def save_strategy(definition: StrategyDefinition):
    return _store().save_strategy(definition)


@router.get("/runs")
def list_runs(limit: int = Query(50, ge=1, le=100)):
    return {"runs": _store().list_runs(limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    try:
        return _store().get_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs")
def run_research(request: ResearchRequest):
    from trade_compass_agent.data.providers import ProviderError
    try:
        return run_strategy_research(MarketStack.from_config(), request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
