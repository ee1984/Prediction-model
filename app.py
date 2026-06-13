"""Streamlit app for SEC filing links and simple credit ratio analysis."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import streamlit as st

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no}/{document}"
RATIOS_CONFIG_PATH = Path(__file__).with_name("ratios_config.json")


@dataclass(frozen=True)
class Company:
    """Basic company information from the SEC ticker mapping."""

    ticker: str
    cik: int
    title: str

    @property
    def padded_cik(self) -> str:
        return str(self.cik).zfill(10)


# SEC data retrieval helpers. These functions only fetch filing metadata and links;
# they do not feed data into the manual ratio engine below.
def sec_headers(contact_email: str) -> dict[str, str]:
    """Build SEC request headers with a descriptive User-Agent."""
    return {
        "User-Agent": f"Prediction-model educational Streamlit app contact:{contact_email}",
        "Accept-Encoding": "gzip, deflate",
    }


@st.cache_data(ttl=24 * 60 * 60)
def load_company_tickers(contact_email: str) -> dict[str, Company]:
    """Load the free SEC ticker-to-CIK mapping."""
    response = requests.get(SEC_TICKERS_URL, headers=sec_headers(contact_email), timeout=20)
    response.raise_for_status()
    raw_companies: dict[str, Any] = response.json()

    companies: dict[str, Company] = {}
    for item in raw_companies.values():
        ticker = str(item["ticker"]).upper()
        companies[ticker] = Company(
            ticker=ticker,
            cik=int(item["cik_str"]),
            title=str(item["title"]),
        )
    return companies


@st.cache_data(ttl=60 * 60)
def load_recent_filings(cik: str, contact_email: str) -> pd.DataFrame:
    """Load recent filings for a company from the free SEC submissions endpoint."""
    response = requests.get(
        SEC_SUBMISSIONS_URL.format(cik=cik),
        headers=sec_headers(contact_email),
        timeout=20,
    )
    response.raise_for_status()
    filings = response.json().get("filings", {}).get("recent", {})
    return pd.DataFrame(filings)


def filing_url(company: Company, accession_number: str, primary_document: str) -> str:
    """Build a direct SEC Archives link for a filing document."""
    accession_no = accession_number.replace("-", "")
    return SEC_ARCHIVES_URL.format(
        cik_int=company.cik,
        accession_no=accession_no,
        document=primary_document,
    )


def latest_filing(filings: pd.DataFrame, form_type: str) -> pd.Series | None:
    """Return the latest filing row for a specific form type, if available."""
    required_columns = {"form", "filingDate", "accessionNumber", "primaryDocument"}
    if filings.empty or not required_columns.issubset(filings.columns):
        return None

    matches = filings[filings["form"] == form_type].copy()
    if matches.empty:
        return None

    matches["filingDate"] = pd.to_datetime(matches["filingDate"], errors="coerce")
    matches = matches.sort_values("filingDate", ascending=False)
    return matches.iloc[0]


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Calculate a ratio while avoiding divide-by-zero and missing values."""
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def optional_positive_input(value: float) -> float | None:
    """Treat zero values as missing for optional manually entered financial data."""
    return value if value > 0 else None


def optional_input(value: float) -> float | None:
    """Treat zero values as missing while allowing negative manual inputs."""
    return value if value != 0 else None


# Manual ratio engine. This stays separate from SEC filing retrieval because the
# app links to filings but does not parse audited statement values from them.
def load_ratio_config() -> list[dict[str, str]]:
    """Load ratio definitions from the local JSON config file."""
    with RATIOS_CONFIG_PATH.open(encoding="utf-8") as config_file:
        return json.load(config_file)["ratios"]


def ratio_values(inputs: dict[str, float | None]) -> dict[str, float | None]:
    """Prepare base and derived values used by configured ratios."""
    debt = inputs.get("total_debt")
    cash = inputs.get("cash") or 0
    operating_cash_flow = inputs.get("operating_cash_flow")
    capital_expenditures = inputs.get("capital_expenditures") or 0

    values = dict(inputs)
    values["net_debt"] = debt - cash if debt is not None else None
    values["free_cash_flow"] = (
        operating_cash_flow - capital_expenditures if operating_cash_flow is not None else None
    )
    return values


def calculate_credit_ratios(inputs: dict[str, float | None]) -> dict[str, tuple[float | None, str]]:
    """Prepare configurable credit rating agency-style ratio analysis metrics."""
    values = ratio_values(inputs)
    ratios: dict[str, tuple[float | None, str]] = {}
    for ratio in load_ratio_config():
        ratios[ratio["name"]] = (
            safe_ratio(values.get(ratio["numerator"]), values.get(ratio["denominator"])),
            ratio.get("display", "multiple"),
        )
    return ratios


def format_ratio(value: float | None, percent: bool = False) -> str:
    """Format ratio output for display."""
    if value is None:
        return "Not available"
    if percent:
        return f"{value:.1%}"
    return f"{value:.2f}x"


def render_latest_filing(company: Company, filing: pd.Series | None, label: str) -> None:
    """Render a latest filing link or a helpful missing-state message."""
    if filing is None:
        st.info(f"No recent {label} filing found in the SEC recent filings feed.")
        return

    accession_number = filing.get("accessionNumber")
    primary_document = filing.get("primaryDocument")
    filing_date = filing.get("filingDate", "Unknown date")
    if not accession_number or not primary_document:
        st.info(f"{label} was found, but the SEC feed did not include a usable document link.")
        return

    url = filing_url(company, str(accession_number), str(primary_document))
    st.markdown(f"**{label}:** [{filing_date} filed document]({url})")


def main() -> None:
    st.set_page_config(page_title="Financial Statement Credit Ratio App", layout="wide")
    st.title("Financial Statement Analysis App")
    st.write(
        "Enter a U.S. public company ticker to retrieve recent SEC filing links and prepare "
        "credit rating agency-style ratio analysis."
    )
    st.caption(
        "This educational app uses free SEC data. It does not reproduce S&P Global Ratings' "
        "or any other rating agency's proprietary methodology."
    )

    contact_email = st.text_input(
        "Contact email for SEC User-Agent",
        help="SEC requests should include a descriptive User-Agent with contact information.",
        placeholder="you@example.com",
    ).strip()
    ticker = st.text_input("Company ticker", value="AAPL", help="Example: AAPL, MSFT, TSLA").strip().upper()

    if ticker and not contact_email:
        st.warning("Enter a contact email before retrieving SEC data so requests include a proper SEC User-Agent.")
    elif ticker:
        try:
            companies = load_company_tickers(contact_email)
            company = companies.get(ticker)
            if company is None:
                st.error("Ticker not found in the SEC company ticker list.")
            else:
                st.subheader(f"{company.title} ({company.ticker})")
                st.write(f"SEC CIK: `{company.padded_cik}`")

                filings = load_recent_filings(company.padded_cik, contact_email)
                latest_10k = latest_filing(filings, "10-K")
                latest_10q = latest_filing(filings, "10-Q")

                st.subheader("Latest SEC filing links")
                render_latest_filing(company, latest_10k, "Latest 10-K")
                render_latest_filing(company, latest_10q, "Latest 10-Q")

                with st.expander("Show recent filings table"):
                    columns = ["filingDate", "reportDate", "form", "accessionNumber", "primaryDocument"]
                    existing_columns = [column for column in columns if column in filings.columns]
                    if existing_columns:
                        st.dataframe(filings[existing_columns].head(20), use_container_width=True)
                    else:
                        st.info("No displayable recent filing columns were returned by the SEC feed.")
        except (KeyError, ValueError, requests.RequestException) as exc:
            st.error(f"Could not retrieve SEC data: {exc}")

    st.divider()
    st.subheader("Simple credit ratio engine")
    st.write(
        "Enter statement values manually to preview credit rating agency-style ratio analysis. "
        "Use the same units for every field, such as USD millions. Values are not pulled from SEC filings."
    )
    st.caption(
        "Leave unknown fields as 0. Enter capital expenditures as a positive cash outflow; "
        "the app subtracts it from operating cash flow."
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        revenue = st.number_input("Revenue", min_value=0.0, value=0.0, step=100.0)
        ebitda = st.number_input("EBITDA", value=0.0, step=100.0)
    with col2:
        total_debt = st.number_input("Total debt", min_value=0.0, value=0.0, step=100.0)
        cash = st.number_input("Cash and equivalents", min_value=0.0, value=0.0, step=100.0)
    with col3:
        interest_expense = st.number_input("Interest expense", min_value=0.0, value=0.0, step=10.0)
        operating_cash_flow = st.number_input("Operating cash flow", value=0.0, step=100.0)
        capital_expenditures = st.number_input("Capital expenditures", min_value=0.0, value=0.0, step=100.0)

    ratios = calculate_credit_ratios(
        {
            "revenue": revenue or None,
            "ebitda": optional_input(ebitda),
            "total_debt": optional_positive_input(total_debt),
            "cash": cash,
            "interest_expense": optional_positive_input(interest_expense),
            "operating_cash_flow": optional_input(operating_cash_flow),
            "capital_expenditures": capital_expenditures,
        }
    )
    ratio_rows = []
    for name, (value, display_type) in ratios.items():
        ratio_rows.append(
            {
                "Ratio": name,
                "Value": format_ratio(value, percent=display_type == "percent"),
            }
        )
    st.dataframe(pd.DataFrame(ratio_rows), hide_index=True, use_container_width=True)


if __name__ == "__main__":
    main()
