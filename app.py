"""Streamlit app for SEC filing links and simple credit ratio analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass
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


@st.cache_data
def load_ratio_definitions() -> list[dict[str, Any]]:
    """Load transparent ratio definitions from the local JSON config file."""
    with RATIOS_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        ratio_definitions = json.load(config_file)
    if not isinstance(ratio_definitions, list):
        raise ValueError("ratios_config.json must contain a list of ratio definitions.")
    required_keys = {"name", "formula", "numerator", "denominator", "format"}
    for index, definition in enumerate(ratio_definitions, start=1):
        if not isinstance(definition, dict):
            raise ValueError(f"Ratio definition {index} must be an object.")
        missing_keys = required_keys - set(definition)
        if missing_keys:
            missing = ", ".join(sorted(missing_keys))
            raise ValueError(f"Ratio definition {index} is missing: {missing}.")
    return ratio_definitions


def term_inputs(terms: list[dict[str, Any]]) -> list[str]:
    """Return the input names referenced by a list of ratio terms."""
    return [str(term["input"]) for term in terms]


def calculate_terms(terms: list[dict[str, Any]], inputs: dict[str, float | None]) -> tuple[float | None, list[str]]:
    """Calculate a transparent sum of configured terms and report missing inputs."""
    total = 0.0
    missing_inputs: list[str] = []
    for term in terms:
        input_name = str(term["input"])
        multiplier = float(term.get("multiplier", 1))
        value = inputs.get(input_name)
        if value is None:
            missing_inputs.append(input_name)
        else:
            total += multiplier * value

    if missing_inputs:
        return None, missing_inputs
    return total, []


# Manual ratio engine. This stays separate from SEC filing retrieval because the
# app links to filings but does not parse audited statement values from them.
def calculate_configured_ratios(
    ratio_definitions: list[dict[str, Any]],
    inputs: dict[str, float | None],
) -> list[dict[str, Any]]:
    """Calculate only the ratios listed in ratios_config.json."""
    results: list[dict[str, Any]] = []
    for definition in ratio_definitions:
        numerator_terms = definition.get("numerator", [])
        denominator_terms = definition.get("denominator", [])
        numerator, missing_numerator = calculate_terms(numerator_terms, inputs)
        denominator, missing_denominator = calculate_terms(denominator_terms, inputs)
        missing_inputs = sorted(set(missing_numerator + missing_denominator))

        value = None if missing_inputs else safe_ratio(numerator, denominator)
        if denominator == 0:
            missing_inputs.append("non-zero denominator")

        used_input_names = sorted(set(term_inputs(numerator_terms) + term_inputs(denominator_terms)))
        results.append(
            {
                "name": definition.get("name", "Unnamed ratio"),
                "formula": definition.get("formula", "Not provided"),
                "value": value,
                "format": definition.get("format", "multiple"),
                "inputs_used": {name: inputs.get(name) for name in used_input_names},
                "missing_inputs": missing_inputs,
            }
        )
    return results


def format_ratio(value: float | None, percent: bool = False) -> str:
    """Format ratio output for display."""
    if value is None:
        return "Not available"
    if percent:
        return f"{value:.1%}"
    return f"{value:.2f}x"


def format_inputs_used(inputs_used: dict[str, float | None]) -> str:
    """Format configured input names and values for transparent display."""
    parts = []
    for name, value in inputs_used.items():
        display_value = "missing" if value is None else f"{value:,.2f}"
        parts.append(f"{name}={display_value}")
    return "; ".join(parts)


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
    st.caption("Leave unknown fields as 0. The app calculates only the ratios listed in ratios_config.json.")

    col1, col2, col3 = st.columns(3)
    with col1:
        revenue = st.number_input("Revenue", min_value=0.0, value=0.0, step=100.0)
        ebitda = st.number_input("EBITDA", value=0.0, step=100.0)
    with col2:
        total_debt = st.number_input("Total debt", min_value=0.0, value=0.0, step=100.0)
        cash = st.number_input("Cash and equivalents", min_value=0.0, value=0.0, step=100.0)
    with col3:
        interest_expense = st.number_input("Interest expense", min_value=0.0, value=0.0, step=10.0)

    manual_inputs = {
        "revenue": revenue or None,
        "ebitda": optional_input(ebitda),
        "total_debt": optional_positive_input(total_debt),
        "cash": cash,
        "interest_expense": optional_positive_input(interest_expense),
    }

    try:
        ratio_definitions = load_ratio_definitions()
        configured_ratios = calculate_configured_ratios(ratio_definitions, manual_inputs)
        ratio_rows = []
        for ratio in configured_ratios:
            ratio_rows.append(
                {
                    "Ratio": ratio["name"],
                    "Formula": ratio["formula"],
                    "Value": format_ratio(ratio["value"], percent=ratio["format"] == "percent"),
                    "Inputs Used": format_inputs_used(ratio["inputs_used"]),
                    "Missing Inputs": ", ".join(ratio["missing_inputs"]) or "None",
                }
            )
        st.dataframe(pd.DataFrame(ratio_rows), hide_index=True, use_container_width=True)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        st.error(
            "Could not calculate ratios from ratios_config.json. "
            f"Check the ratio definitions file for missing or invalid fields: {exc}"
        )


if __name__ == "__main__":
    main()
