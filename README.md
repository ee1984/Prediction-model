# Prediction-model

A simple Python Streamlit app for financial statement analysis and credit rating agency-style ratio analysis.

## What the app does

- Accepts a U.S. public company ticker.
- Uses free SEC endpoints to find the company's CIK.
- Retrieves recent SEC filing metadata.
- Shows the latest 10-K and 10-Q filing links when available.
- Provides a simple manual credit ratio engine for exploratory analysis.

This project does **not** use paid APIs. It does **not** reproduce S&P Global Ratings' or any other rating agency's proprietary methodology.

## Files

- `app.py` - Streamlit app.
- `requirements.txt` - Python dependencies.
- `README.md` - Setup and usage instructions.

## Configurable ratios

Ratio definitions live in `ratios_config.json`, where each ratio names the input fields to divide and whether the result displays as a multiple or percent. This supports configurable credit rating agency-style ratio analysis without changing the SEC filing link functionality.

## How to run

1. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Start the Streamlit app:

   ```bash
   streamlit run app.py
   ```

4. Open the local URL shown by Streamlit, usually <http://localhost:8501>.

5. Enter a contact email in the app before requesting SEC filing data. The app uses it in the SEC `User-Agent` header.

## Notes and limitations

- SEC requests require internet access.
- The ratio engine is intentionally simple and uses manually entered financial statement values; it does not parse statement values from SEC filings.
- Leave unknown ratio fields as `0` in the app. Enter capital expenditures as a positive cash outflow.
- Outputs are educational and should not be treated as investment advice, credit ratings, or a substitute for professional analysis.
