import { ApiError } from "@/lib/agent-api";

export type ForecastQualityStatus = "experimental";

export interface ForecastBar {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface ForecastPayload {
  symbol: string;
  model: string;
  forecast_bars: ForecastBar[];
  confidence_band: { upper: number[]; lower: number[] };
  quality_status: ForecastQualityStatus;
  parameters: {
    horizon: number;
    model_size: string;
    sample_count: number;
    lookback: number;
  };
}

export interface ForecastOverlay {
  bars: ForecastBar[];
  upperBand: number[];
  lowerBand: number[];
  model: string;
  qualityStatus: ForecastQualityStatus;
}

const NON_RETRYABLE_FORECAST_CODES = new Set([
  "forecast_unavailable",
  "insufficient_history",
  "invalid_forecast_response",
]);

export function shouldRetryForecast(
  failureCount: number,
  error: unknown,
): boolean {
  if (
    error instanceof ApiError &&
    error.code &&
    NON_RETRYABLE_FORECAST_CODES.has(error.code)
  ) {
    return false;
  }
  return failureCount < 1;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isFiniteNumberArray(value: unknown): value is number[] {
  return (
    Array.isArray(value) &&
    value.every((item) => typeof item === "number" && Number.isFinite(item))
  );
}

function isForecastBar(value: unknown): value is ForecastBar {
  if (!isRecord(value) || typeof value.timestamp !== "string") return false;
  return ["open", "high", "low", "close", "volume"].every(
    (key) => typeof value[key] === "number" && Number.isFinite(value[key]),
  );
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

export function isForecastPayload(value: unknown): value is ForecastPayload {
  if (!isRecord(value) || !Array.isArray(value.forecast_bars)) return false;
  if (!isRecord(value.confidence_band) || !isRecord(value.parameters)) return false;

  const upper = value.confidence_band.upper;
  const lower = value.confidence_band.lower;
  const parameters = value.parameters;

  return (
    typeof value.symbol === "string" &&
    value.symbol.length > 0 &&
    typeof value.model === "string" &&
    value.model.length > 0 &&
    value.quality_status === "experimental" &&
    value.forecast_bars.length > 0 &&
    value.forecast_bars.every(isForecastBar) &&
    isFiniteNumberArray(upper) &&
    isFiniteNumberArray(lower) &&
    value.forecast_bars.length === upper.length &&
    upper.length === lower.length &&
    isPositiveInteger(parameters.horizon) &&
    typeof parameters.model_size === "string" &&
    parameters.model_size.length > 0 &&
    isPositiveInteger(parameters.sample_count) &&
    isPositiveInteger(parameters.lookback)
  );
}

export function toForecastOverlay(payload: ForecastPayload): ForecastOverlay {
  return {
    bars: payload.forecast_bars,
    upperBand: payload.confidence_band.upper,
    lowerBand: payload.confidence_band.lower,
    model: payload.model,
    qualityStatus: payload.quality_status,
  };
}
