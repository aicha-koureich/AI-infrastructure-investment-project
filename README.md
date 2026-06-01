# AI Infrastructure Investment Project

Research project on AI infrastructure investment disclosure by S&P 1500 companies between 2013 and 2024, based on SEC 10-K annual filings.

**Instructor:** Chang Gong — *AI Infrastructure Data Construction Handbook*

---

## How it works

### Step 1 — Extraction (`extract_10k_v5.py`)

Reads raw HTML 10-K filings from Google Drive (151 ZIP batches, ~18,000 firm-years) and extracts structured data for each company and fiscal year.

- **Task 2** — Finds the depreciation sentence in the accounting policies note and extracts the useful life range for IT assets (e.g. "3 to 10 years for equipment and capitalized software"). Outputs `useful_life_min_years`, `useful_life_max_years`, `asset_category`, and the verbatim sentence.
- **Task 3** — Searches for explicit mentions of policy changes ("revised useful lives", "change in estimate"). Outputs `policy_change` (Yes / No) and the verbatim paragraph if found.
- **Task 5** — Extracts full paragraphs containing AI-related keywords (GPU, data center, machine learning, NVIDIA, etc.) from Item 8 and MD&A. Outputs `ai_infra_text` and its source location.
- **Task 6** — Identifies segment-level mentions of cloud or AI business units. Outputs `segment_ai_text`.
- **Task 7** — Scans the full document for specific hardware models (H100, A100, AMD Instinct, TPU, etc.). Outputs `hardware_mentions` and `hardware_count`.

Produces `results/extraction_results_global.csv` — one row per firm-year, all variables above included — along with individual verbatim `.txt` files in `results/verbatims/`.

---

### Step 2 — Synthesis (`synthesize_10k.py`)

Reads the extraction CSV and runs two types of analysis.

- **Algorithmic pass (Tasks 3-4)** — Compares useful life values year over year for each company. If the max or min changes by more than 0.5 years, it flags a policy change and records the year. Distinguishes real changes from first-time disclosures. Outputs `task3_algo`, `task4_date`, `delta_min`, `delta_max`.
- **LLM pass (Task 5)** — Sends each AI verbatim to Google Gemini (free API) and asks it to classify the disclosure as `detailed` (specific hardware, investment figures), `vague` (generic AI mentions), or `not_relevant` (ERP, cybersecurity, etc.). Also extracts use cases and specific products mentioned. Outputs `llm_disclosure_level`, `llm_justification`, `llm_specific_products`.

## LLM Classification Prompt (Task 5)

AI infrastructure disclosures are classified using Google Gemini 2.0 Flash
via the following structured prompt:

Tu es un assistant de recherche en comptabilité financière.
Analyse ce verbatim extrait d'un rapport 10-K (SEC) et réponds UNIQUEMENT en JSON valide.

Verbatim à analyser :
---
{verbatim}
---

Réponds avec ce JSON exact :
{
  "disclosure_level": "detailed" ou "vague" ou "not_relevant",
  "justification": "une phrase courte",
  "use_cases": ["liste des cas d'usage IA mentionnés"],
  "capex_mentioned": true ou false,
  "specific_products": ["liste des produits/modèles mentionnés"]
}

Règles :
- "detailed" = détails précis sur l'infrastructure IA (modèles GPU, investissements chiffrés, capacités)
- "vague" = mentionne l'IA sans détails concrets ("we use AI")
- "not_relevant" = cybersécurité, ERP, logiciel métier, MRO
- "capex_mentioned" = true si dépenses en capital liées à l'IA

Produces `synthesis/synthesis_results.csv` — the final analytical dataset — along with one summary report per company in `synthesis/firm_reports/` and a global trends report in `synthesis/rapport_global.txt`.

---

## Repository structure

```
script/          ← Python scripts (extract_10k_v5.py, synthesize_10k.py, download_10K.py)
results/         ← Extraction outputs (CSVs, verbatims, logs)
synthesis/       ← Synthesis outputs (analytical dataset, firm reports, global report)
rendu/           ← Final deliverables for submission
```

---

## Deliverables

| Requirement | File |
|---|---|
| Structured datasets | `rendu/extraction_results_global.csv` + `synthesis/synthesis_results.csv` |
| Archive of source filings | Google Drive (`raw_filings/batch_*.zip`) |
| Verbatim excerpts | `rendu/verbatim_global.txt` |
| Source log | `rendu/source_log.csv` |
| Replication log | `rendu/replication_log_global.csv` |

---

## For setup and usage instructions, see [GUIDE.md](GUIDE.md)
