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
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no}/{document}"
RATIOS_CONFIG_PATH = Path(__file__).with_name("ratios_config.json")


@dataclass(frozen=True)
class SecFactSuggestion:
    """Read-only SEC Company Facts value shown as a manual-input suggestion."""

    field: str
    value: float | None
    tag: str | None
    form: str | None
    fiscal_year: int | None
    end_date: str | None
    unit: str | None = None
    candidate_tags: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def source_label(self) -> str:
        if self.form == "10-K":
            return "Annual 10-K data"
        if self.form:
            return f"Other form: {self.form}"
        return "Not available"

    @property
    def data_quality_label(self) -> str:
        if self.form == "10-K":
            return "Annual 10-K data"
        if self.form:
            return "Fallback form used"
        return "No value available"


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



@st.cache_data(ttl=60 * 60)
def load_company_facts(cik: str, contact_email: str) -> dict[str, Any]:
    """Load free SEC Company Facts data for a company."""
    response = requests.get(
        SEC_COMPANY_FACTS_URL.format(cik=cik),
        headers=sec_headers(contact_email),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


SEC_FACT_TAGS: dict[str, list[str]] = {
    "revenue": ["Revenues", "SalesRevenueNet"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "total_debt": [
        "LongTermDebtAndFinanceLeaseObligations",
        "LongTermDebtAndFinanceLeaseObligationsCurrent",
        "LongTermDebt",
    ],
    "interest_expense": ["InterestExpenseNonOperating", "InterestExpense"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}


def _available_units(fact: dict[str, Any]) -> list[str]:
    """Return Company Facts units that contain at least one observation."""
    return [unit for unit, observations in fact.get("units", {}).items() if observations]


def _latest_fact_unit(fact: dict[str, Any], unit: str) -> dict[str, Any] | None:
    """Pick the latest fact for a unit, preferring annual 10-K observations."""
    unit_facts = fact.get("units", {}).get(unit, [])
    if not unit_facts:
        return None

    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return (str(item.get("end", "")), str(item.get("filed", "")))

    annual_facts = [
        item
        for item in unit_facts
        if item.get("form") == "10-K" and item.get("fp") == "FY" and item.get("val") is not None
    ]
    if annual_facts:
        return max(annual_facts, key=sort_key)

    usable_facts = [item for item in unit_facts if item.get("val") is not None]
    if usable_facts:
        return max(usable_facts, key=sort_key)
    return None


def _candidate_tag_names(us_gaap_facts: dict[str, Any], tags: list[str]) -> tuple[str, ...]:
    """Return configured tags with usable SEC values."""
    candidates = []
    for tag in tags:
        fact = us_gaap_facts.get(tag, {})
        if any(_latest_fact_unit(fact, unit) is not None for unit in _available_units(fact)):
            candidates.append(tag)
    return tuple(candidates)


def _warning_flags(field: str, suggestion: SecFactSuggestion) -> tuple[str, ...]:
    """Build conservative data-quality warnings for SEC suggestions."""
    warnings: list[str] = []
    if suggestion.value is None:
        warnings.append("No value available")
    elif suggestion.form != "10-K":
        warnings.append("Fallback form used")
    if len(suggestion.candidate_tags) > 1:
        warnings.append("Multiple candidate tags available")
    if field == "total_debt" and suggestion.tag:
        debt_tag_words = suggestion.tag.lower()
        if "current" in debt_tag_words or "longterm" in debt_tag_words or "long_term" in debt_tag_words:
            warnings.append("Debt tag may not represent total debt")
    if field == "cash" and suggestion.tag and "restrictedcash" in suggestion.tag.lower():
        warnings.append("Cash tag may include restricted cash")
    if field == "capex":
        warnings.append("Capex shown as positive cash outflow")
        if suggestion.value is not None and suggestion.value < 0:
            warnings.append("Capex SEC value uses an unusual negative sign convention")
    return tuple(warnings)


def sec_fact_suggestions(company_facts: dict[str, Any]) -> dict[str, SecFactSuggestion]:
    """Extract read-only suggestions from SEC Company Facts without changing ratio inputs."""
    us_gaap_facts = company_facts.get("facts", {}).get("us-gaap", {})
    suggestions: dict[str, SecFactSuggestion] = {}
    for field, tags in SEC_FACT_TAGS.items():
        candidate_tags = _candidate_tag_names(us_gaap_facts, tags)
        suggestion = SecFactSuggestion(
            field, None, None, None, None, None, None, candidate_tags, ("No value available",)
        )
        for tag in tags:
            fact = us_gaap_facts.get(tag, {})
            available_units = _available_units(fact)
            selected_unit = "USD" if "USD" in available_units else next(iter(available_units), None)
            latest_fact = _latest_fact_unit(fact, selected_unit) if selected_unit else None
            if latest_fact is not None:
                suggestion = SecFactSuggestion(
                    field=field,
                    value=float(latest_fact["val"]),
                    tag=tag,
                    form=str(latest_fact.get("form", "")) or None,
                    fiscal_year=latest_fact.get("fy"),
                    end_date=str(latest_fact.get("end", "")) or None,
                    unit=selected_unit,
                    candidate_tags=candidate_tags,
                )
                break
        suggestion = SecFactSuggestion(
            field=suggestion.field,
            value=abs(suggestion.value) if field == "capex" and suggestion.value is not None else suggestion.value,
            tag=suggestion.tag,
            form=suggestion.form,
            fiscal_year=suggestion.fiscal_year,
            end_date=suggestion.end_date,
            unit=suggestion.unit,
            candidate_tags=suggestion.candidate_tags,
            warnings=_warning_flags(field, suggestion),
        )
        suggestions[field] = suggestion
    return suggestions


def format_sec_value(value: float | None) -> str:
    """Format SEC Company Facts values for read-only suggestion display."""
    if value is None:
        return "Not available"
    return f"{value:,.0f}"


def render_sec_fact_suggestions(suggestions: dict[str, SecFactSuggestion]) -> None:
    """Render read-only SEC Company Facts suggestions with tags and source forms."""
    st.subheader("SEC Company Facts suggestions")
    st.caption(
        "Read-only suggestions from free SEC Company Facts endpoints. These values do not populate "
        "the editable manual inputs and are not final audited outputs. EBITDA and total debt remain "
        "manual for the ratio worksheet."
    )
    labels = {
        "revenue": "Revenue",
        "cash": "Cash",
        "total_debt": "Total debt",
        "interest_expense": "Interest expense",
        "operating_cash_flow": "Operating cash flow",
        "capex": "Capex",
    }
    rows = []
    for field, label in labels.items():
        suggestion = suggestions.get(
            field,
            SecFactSuggestion(field, None, None, None, None, None, None, (), ("No value available",)),
        )
        rows.append(
            {
                "Internal field": field,
                "Label": label,
                "Suggested value": format_sec_value(suggestion.value),
                "SEC tag": suggestion.tag or "Not available",
                "Source form": suggestion.form or "Not available",
                "Fiscal year": suggestion.fiscal_year or "Not available",
                "Period end": suggestion.end_date or "Not available",
                "Unit": suggestion.unit or "Not available",
                "Annual/fallback": suggestion.data_quality_label,
                "Quality flags": "; ".join(suggestion.warnings) or "Annual 10-K data",
                "Candidate tags": ", ".join(suggestion.candidate_tags) or "Not available",
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


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
    """Prepare configurable basic credit ratio analysis metrics."""
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
        "a credit-analysis-style ratio worksheet."
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

                company_facts = load_company_facts(company.padded_cik, contact_email)
                render_sec_fact_suggestions(sec_fact_suggestions(company_facts))

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
        "Enter statement values manually to preview basic credit ratio analysis. "
        "Use the same units for every field, such as USD millions. SEC extracted values are suggestions only."
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
