# AI Infrastructure Investment Project

This project automatically extracts financial data from SEC 10-K filings (annual reports) for S&P 1500 companies between 2013 and 2024.

The goal is to study how companies report the **useful life of their IT assets** (servers, computers, software) and whether they mention **AI infrastructure investments** (GPUs, data centers, machine learning).

This work follows the *AI Infrastructure Data Construction Handbook* provided by the instructor.

---

## What this project does

Each annual report (10-K filing) is processed by a Python script that automatically extracts:

- **How long** a company depreciates its IT equipment (e.g. "3 to 10 years")
- **Whether** that duration changed from one year to the next
- **Whether** the company mentions AI infrastructure (GPUs, data centers, etc.)
- **Which specific hardware** is mentioned (NVIDIA H100, AMD Instinct, etc.)
- **Segment-level** mentions of cloud or AI business units

---

## Project structure

```
AI-infrastructure-investment-project/
│
├── script/                         ← All Python scripts
│   ├── extract_10k_v5.py           ← Main extraction script (reads 10-K HTML files)
│   ├── synthesize_10k.py           ← Synthesis script (analysis + Gemini AI classification)
│   └── download_10K.py             ← Script to download filings from SEC EDGAR
│
├── results/                        ← All extraction outputs
│   ├── extraction_results_global.csv     ← Main dataset (all firms, all years)
│   ├── extraction_results_batch_X.csv    ← One file per batch (checkpoint system)
│   ├── replication_log_global.csv        ← Log of ambiguous or failed extractions
│   ├── source_log.csv                    ← Traceability: which filing → which result
│   ├── verbatim_global.txt               ← All verbatim excerpts in one file
│   └── verbatims/                        ← One .txt file per firm-year
│       ├── 0000001750_AIR_FY2013_10K.txt
│       ├── 0000002488_AMD_FY2024_10K.txt
│       └── ...
│
├── synthesis/                      ← Synthesis outputs (after running synthesize_10k.py)
│   ├── synthesis_results.csv             ← Final analytical dataset
│   ├── rapport_global.txt                ← Global trends report
│   ├── llm_checkpoint.json               ← Gemini API checkpoint (auto-resume)
│   └── firm_reports/                     ← One report per company
│       ├── 0000001750_AIR.txt
│       └── ...
│
├── rendu/                          ← Final deliverables for submission
│   ├── extraction_results_global.csv
│   ├── replication_log_global.csv
│   ├── source_log.csv
│   └── verbatim_global.txt
│
├── logs_and_tracking/              ← Tracking files (download progress, missing filings)
├── missing10K.csv                  ← List of filings that could not be found
├── missingCIK.csv                  ← List of CIKs with no filings available
├── rapport.pdf                     ← Project report
└── README.md                       ← This file
```

---

## Requirements

### Python dependencies

```bash
pip install pandas beautifulsoup4 lxml python-dotenv requests
```

### Rclone (for Google Drive access)

The extraction script downloads ZIP files directly from Google Drive using `rclone`.

Install it:
```bash
sudo apt install rclone
```

Configure it once (follow the prompts, choose Google Drive):
```bash
rclone config
```

Use `gdrive` as the remote name when prompted.

### API key for Gemini (optional, only for synthesis)

The synthesis script uses Google Gemini (free) to classify AI disclosures.

1. Go to [aistudio.google.com](https://aistudio.google.com) and click **Get API Key**
2. Create a `.env` file at the root of the project:

```
GEMINI_API_KEY=AIzaSyD_your_key_here
```

---

## How to run the project

### Step 1 — Extract data from 10-K filings

This reads the ZIP files from Google Drive, extracts data from each HTML filing, and saves results in `./results/`.

```bash
# Full run (all batches)
python script/extract_10k_v5.py \
  --drive "gdrive:AI_Infrastructure_Investment_Project/raw_filings" \
  --output ./results/

# If the script stops, just run the same command again.
# It will automatically skip batches that are already done.

# Run a single batch (useful for testing)
python script/extract_10k_v5.py \
  --drive "gdrive:AI_Infrastructure_Investment_Project/raw_filings" \
  --batch batch_1.zip \
  --output ./results/

# Test on a single local HTML file
python script/extract_10k_v5.py --input ./0000001750_AIR_FY2025_10K.html
```

### Step 2 — Generate the deliverable files

Once extraction is done, run these three commands to produce the final files:

```bash
# Replication log (ambiguous and failed extractions)
python3 -c "
import pandas as pd
df = pd.read_csv('./results/extraction_results_global.csv')
mask = (
    (df['extraction_status'] != 'OK') |
    (df['ambiguity_flag'] == True) |
    (df['useful_life_text'] == 'Not disclosed') |
    (df['confidence'] <= 1)
)
df_log = df[mask][['firm_cik','firm_ticker','fiscal_year','filename',
                    'extraction_status','confidence','ambiguity_flag',
                    'ambiguity_detail','useful_life_text','error_message']].copy()
df_log['issue_description'] = ''
df_log.loc[df_log['extraction_status'] != 'OK', 'issue_description'] = 'Extraction error'
df_log.loc[df_log['useful_life_text'] == 'Not disclosed', 'issue_description'] = 'No useful life found'
df_log.loc[df_log['ambiguity_flag'] == True, 'issue_description'] = 'Multiple durations detected'
df_log.to_csv('./results/replication_log_global.csv', index=False)
print(f'Done: {len(df_log)} entries')
"

# Source log (traceability: one row per filing)
python3 -c "
import pandas as pd
df = pd.read_csv('./results/extraction_results_global.csv')
source = df[['firm_cik','firm_ticker','fiscal_year','filename',
             'filing_date','period_end_date','source_section',
             'section_title','confidence','extraction_status']].copy()
source['edgar_url'] = source['firm_cik'].apply(
    lambda c: f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={c}&type=10-K'
)
source['download_date'] = '2025-06-01'
source['source_type'] = 'SEC EDGAR 10-K (iXBRL HTML)'
source.to_csv('./results/source_log.csv', index=False)
print(f'Done: {len(source)} filings')
"

# Verbatim global file (merge all individual .txt files)
cat ./results/verbatims/*.txt > ./results/verbatim_global.txt && \
  echo "Done: $(ls ./results/verbatims/*.txt | wc -l) files merged"
```

### Step 3 — Run the synthesis

This step compares useful-life values across years (algorithmic) and classifies AI disclosures using Gemini.

```bash
# Without Gemini (algorithmic analysis only, no API key needed)
python script/synthesize_10k.py \
  --input ./results/extraction_results_global.csv \
  --output ./synthesis/ \
  --no-llm

# With Gemini (full analysis, requires GEMINI_API_KEY in .env)
python script/synthesize_10k.py \
  --input ./results/extraction_results_global.csv \
  --output ./synthesis/
```

---

## Output files explained

### `results/extraction_results_global.csv`
The main dataset. One row per firm-year. Contains all extracted values: useful life duration, asset category, AI infrastructure text, hardware mentions, policy changes, etc.

### `results/replication_log_global.csv`
Documents every case where the extraction was uncertain, failed, or could not find information. Required by the handbook (Section 13). Includes the firm, year, and a description of the issue.

### `results/source_log.csv`
Traceability file. For every row in the main dataset, it records which HTML file was used, which section the data came from, the filing date, and a link to the SEC EDGAR page.

### `results/verbatim_global.txt`
All verbatim text excerpts in a single file. Each entry shows exactly what text was extracted from the filing, so any result can be verified by hand.

### `synthesis/synthesis_results.csv`
The final analytical dataset produced by `synthesize_10k.py`. Adds derived variables: detected policy changes, change dates, Gemini AI classification (detailed / vague / not relevant), and hardware adoption timeline.

### `synthesis/rapport_global.txt`
A human-readable summary of the main findings: disclosure rates by year, average useful life trends, number of policy changes detected, hardware adoption over time, and AI disclosure classification breakdown.

---

## Deliverables (per handbook requirements)

| Requirement | File |
|---|---|
| Structured datasets | `results/extraction_results_global.csv` + `synthesis/synthesis_results.csv` |
| Complete archive of source filings | Google Drive (`raw_filings/batch_*.zip`) |
| Verbatim disclosure excerpts | `results/verbatim_global.txt` |
| Source log | `results/source_log.csv` |
| Replication log | `results/replication_log_global.csv` |

All deliverable files are also copied to the `rendu/` folder for submission.

---

## How the extraction works (simplified)

1. The script downloads one ZIP file at a time from Google Drive
2. It opens each HTML file inside the ZIP (no files saved to disk)
3. It locates **Item 8** (Financial Statements) in the filing
4. It finds the accounting policies section and extracts the depreciation sentence
5. It searches for AI-related keywords (GPU, data center, machine learning, etc.)
6. It saves the result as one row in the CSV
7. It deletes the ZIP and moves to the next batch

If the script stops at any point (internet cut, crash), running the same command again will automatically skip batches that were already completed.
