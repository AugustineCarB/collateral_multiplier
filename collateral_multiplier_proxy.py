"""
Collateral Multiplier Proxy — daily US repo-velocity build.

Theoretical framework:
  Singh (IMF) defines the "collateral multiplier" as the ratio of pledged
  collateral that can be re-used to primary-source collateral. Direct dealer
  data is quarterly and lagged. Infante & Saravay (FEDS Note, 2018) show that
  collateral re-use happens predominantly through secured-financing
  transactions (repo, securities lending, prime-brokerage rehypothecation).

  This script builds a high-frequency velocity proxy:

      velocity_t = SFT_volume_t / free_float_eligible_collateral_t

  where:
    - SFT_volume   = daily Treasury repo volume (SOFR / TGCR / BGCR)
    - free_float   = US Treasury debt held by the public minus the Federal
                     Reserve's SOMA Treasury holdings (i.e., the slice of
                     Treasuries actually available to be pledged in markets)

  Higher velocity ⇒ higher implied collateral multiplier.

Sources (all keyless, public):
  • NY Fed Reference Rates API   markets.newyorkfed.org/api/rates/secured/...
  • Treasury Fiscal Data API     api.fiscaldata.treasury.gov/.../debt_to_penny
  • FRED CSV (no key)            fred.stlouisfed.org/graph/fredgraph.csv?id=TREAST

Outputs (in ./data/):
  • collateral_multiplier_proxy.csv         full daily series
  • collateral_multiplier_proxy_latest.json compact recent + metadata
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)

USER_AGENT = "collateral-multiplier-proxy/1.0 (research; github-actions)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
HTTP_TIMEOUT = 60


# ---------------------------------------------------------------------------
# HTTP helper with retry
# ---------------------------------------------------------------------------
def _get(url: str, params: dict | None = None, accept: str = "application/json"):
    headers = {**HEADERS, "Accept": accept}
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last_exc = e
            sleep = 2 ** attempt
            print(f"  [retry {attempt+1}/4] {url} -> {e}; sleeping {sleep}s",
                  file=sys.stderr)
            time.sleep(sleep)
    raise RuntimeError(f"GET failed after retries: {url}") from last_exc


# ---------------------------------------------------------------------------
# 1. NY Fed reference rates: SOFR, TGCR, BGCR daily volumes
# ---------------------------------------------------------------------------
# NB: the /last/{N} endpoint rejects very large N (returns 400). The /search
# endpoint accepts an unbounded date range, so we use that and pull the full
# history from before SOFR's first publication date (2018-04-03).
NYFED_RATE_URL = "https://markets.newyorkfed.org/api/rates/secured/{name}/search.json"
NYFED_HISTORY_START = "2014-08-22"  # earliest TGCR/BGCR publication date


def fetch_ref_rate(name: str, start_date: str = NYFED_HISTORY_START) -> pd.DataFrame:
    """Fetch a NY Fed secured reference rate series with daily volume."""
    url = NYFED_RATE_URL.format(name=name)
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    params = {"startDate": start_date, "endDate": end_date}
    payload = _get(url, params=params).json()
    rows = payload.get("refRates", [])
    if not rows:
        raise RuntimeError(f"No data returned for {name}")
    df = pd.DataFrame(rows)
    df["effectiveDate"] = pd.to_datetime(df["effectiveDate"])
    df = df.set_index("effectiveDate").sort_index()
    df = df.rename(
        columns={
            "percentRate": f"{name}_rate_pct",
            "volumeInBillions": f"{name}_volume_bn",
        }
    )
    return df[[f"{name}_rate_pct", f"{name}_volume_bn"]].astype(float)


# ---------------------------------------------------------------------------
# 2. Treasury Fiscal Data: debt to the penny (daily)
# ---------------------------------------------------------------------------
DEBT_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v2/accounting/od/debt_to_penny"
)


def fetch_debt_to_penny() -> pd.DataFrame:
    """Daily total public debt outstanding and debt held by the public."""
    frames = []
    page = 1
    while True:
        params = {
            "sort": "-record_date",
            "page[size]": "10000",
            "page[number]": str(page),
        }
        payload = _get(DEBT_URL, params=params).json()
        data = payload.get("data", [])
        if not data:
            break
        frames.append(pd.DataFrame(data))
        meta = payload.get("meta", {}).get("pagination", {})
        if page >= meta.get("total-pages", page):
            break
        page += 1

    df = pd.concat(frames, ignore_index=True)
    df["record_date"] = pd.to_datetime(df["record_date"])
    # amounts are reported in dollars; convert to $ billions ("null" strings → NaN, then drop)
    df["debt_held_public_bn"] = pd.to_numeric(df["debt_held_public_amt"], errors="coerce") / 1e9
    df["total_public_debt_bn"] = pd.to_numeric(df["tot_pub_debt_out_amt"], errors="coerce") / 1e9
    df = df.set_index("record_date").sort_index()
    df = df[["debt_held_public_bn", "total_public_debt_bn"]].dropna(subset=["debt_held_public_bn"])
    return df


# ---------------------------------------------------------------------------
# 3. Fed SOMA Treasury holdings via FRED public CSV (TREAST, weekly Wed)
# ---------------------------------------------------------------------------
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"


def fetch_fred_series(series_id: str, value_col: str) -> pd.DataFrame:
    """Pull a FRED series via the public CSV endpoint (no API key needed)."""
    r = _get(FRED_CSV_URL.format(series=series_id), accept="text/csv")
    df = pd.read_csv(io.StringIO(r.text))
    # FRED CSVs vary: older format has "DATE,<SERIES>"; newer has "observation_date,<SERIES>"
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.rename(columns={date_col: "date", series_id: value_col})
    df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
    return df.set_index("date")[[value_col]].dropna()


# ---------------------------------------------------------------------------
# Build proxy
# ---------------------------------------------------------------------------
def build_proxy() -> pd.DataFrame:
    print("Fetching NY Fed SOFR ...")
    sofr = fetch_ref_rate("sofr")
    print(f"  {len(sofr)} rows  ({sofr.index.min().date()} → {sofr.index.max().date()})")

    print("Fetching NY Fed TGCR ...")
    tgcr = fetch_ref_rate("tgcr")
    print(f"  {len(tgcr)} rows")

    print("Fetching NY Fed BGCR ...")
    bgcr = fetch_ref_rate("bgcr")
    print(f"  {len(bgcr)} rows")

    print("Fetching Treasury debt-to-the-penny ...")
    debt = fetch_debt_to_penny()
    print(f"  {len(debt)} rows  ({debt.index.min().date()} → {debt.index.max().date()})")

    print("Fetching FRED TREAST (Fed SOMA Treasury holdings) ...")
    # TREAST is reported in $ millions
    treast_raw = fetch_fred_series("TREAST", "soma_treasuries_mn")
    treast = (treast_raw["soma_treasuries_mn"] / 1e3).to_frame("soma_treasuries_bn")
    print(f"  {len(treast)} rows  ({treast.index.min().date()} → {treast.index.max().date()})")

    # ----- Align on a daily business-day index --------------------------------
    start = max(sofr.index.min(), tgcr.index.min(), bgcr.index.min(), debt.index.min())
    end = max(sofr.index.max(), tgcr.index.max(), bgcr.index.max(), debt.index.max())
    idx = pd.bdate_range(start=start, end=end)

    df = pd.DataFrame(index=idx)
    df.index.name = "date"
    df = df.join(sofr).join(tgcr).join(bgcr).join(debt).join(treast)

    # Forward-fill the lower-frequency stocks
    df["soma_treasuries_bn"] = df["soma_treasuries_bn"].ffill()
    df[["debt_held_public_bn", "total_public_debt_bn"]] = (
        df[["debt_held_public_bn", "total_public_debt_bn"]].ffill()
    )

    # ----- Compute proxies ----------------------------------------------------
    df["free_float_treasuries_bn"] = (
        df["debt_held_public_bn"] - df["soma_treasuries_bn"]
    )

    df["velocity_sofr"]      = df["sofr_volume_bn"] / df["free_float_treasuries_bn"]
    df["velocity_tgcr"]      = df["tgcr_volume_bn"] / df["free_float_treasuries_bn"]
    df["velocity_bgcr"]      = df["bgcr_volume_bn"] / df["free_float_treasuries_bn"]

    # Composite multiplier proxy = mean of the three velocity series
    df["collateral_multiplier_proxy"] = df[
        ["velocity_sofr", "velocity_tgcr", "velocity_bgcr"]
    ].mean(axis=1)

    # Smoothed series (good for charting)
    df["collateral_multiplier_proxy_30d"] = (
        df["collateral_multiplier_proxy"].rolling(30, min_periods=10).mean()
    )
    df["collateral_multiplier_proxy_90d"] = (
        df["collateral_multiplier_proxy"].rolling(90, min_periods=30).mean()
    )

    return df


def write_outputs(df: pd.DataFrame) -> None:
    csv_path = OUT_DIR / "collateral_multiplier_proxy.csv"
    df.round(6).to_csv(csv_path, index_label="date")
    print(f"Wrote {csv_path}  ({len(df):,} rows)")

    # Compact JSON: last 365 business days + metadata header
    recent = df.tail(365).round(6).reset_index()
    recent["date"] = recent["date"].dt.strftime("%Y-%m-%d")

    payload = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "description": (
                "US Treasury repo-velocity proxy for the Singh collateral multiplier. "
                "velocity = SFT_volume / (debt held by public - Fed SOMA Treasury holdings)."
            ),
            "columns": {
                "sofr_volume_bn": "SOFR daily volume, $ billions (NY Fed)",
                "tgcr_volume_bn": "Tri-party GC volume, $ billions (NY Fed)",
                "bgcr_volume_bn": "Broad GC volume, $ billions (NY Fed)",
                "debt_held_public_bn": "US Treasury debt held by public, $ billions (Fiscal Data)",
                "soma_treasuries_bn": "Fed SOMA Treasury holdings, $ billions (FRED TREAST)",
                "free_float_treasuries_bn": "debt_held_public_bn - soma_treasuries_bn",
                "velocity_sofr": "sofr_volume_bn / free_float_treasuries_bn",
                "velocity_tgcr": "tgcr_volume_bn / free_float_treasuries_bn",
                "velocity_bgcr": "bgcr_volume_bn / free_float_treasuries_bn",
                "collateral_multiplier_proxy": "mean of three velocity series",
                "collateral_multiplier_proxy_30d": "30-day rolling mean of proxy",
                "collateral_multiplier_proxy_90d": "90-day rolling mean of proxy",
            },
            "latest_date": str(df.index.max().date()),
        },
        "series": recent.to_dict(orient="records"),
    }

    json_path = OUT_DIR / "collateral_multiplier_proxy_latest.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Wrote {json_path}")


def main() -> None:
    df = build_proxy()
    write_outputs(df)
    print("\n--- tail ---")
    cols = [
        "sofr_volume_bn",
        "free_float_treasuries_bn",
        "velocity_sofr",
        "collateral_multiplier_proxy",
        "collateral_multiplier_proxy_30d",
    ]
    print(df[cols].tail(10).round(4).to_string())


if __name__ == "__main__":
    main()
