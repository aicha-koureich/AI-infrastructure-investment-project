"""
synthesize_10k.py
=================
Script de synthèse — Phase 2 du pipeline d'extraction 10-K.
Lit le CSV d'extraction (extract_10k_v5.py) et produit :

  1. Passe algorithmique : détection des changements de politique (Task 3-4)
  2. Passe LLM (Gemini) : classification AI disclosure (Task 5)
  3. Agrégation : CSV analytique + rapports par firme + rapport global

Usage :
    # Synthesis with LLM (full analysis, requires GEMINI_API_KEY in .env)
    python script/synthesize_10k.py --input ./results/extraction_results_global.csv --output ./synthesis/

    # Algo only (without LLM, no API key needed)
    python script/synthesize_10k.py --input ./results/extraction_results_global.csv --output ./synthesis/ --no-llm

    # Resume LLM where it stopped (checkpoint auto-resume)
    python script/synthesize_10k.py --input ./results/extraction_results_global.csv --output ./synthesis/

Prérequis :
    pip install pandas python-dotenv requests

    Fichier .env à la racine du projet :
        GEMINI_API_KEY=AIzaSyD_...
"""

import os
import re
import sys
import json
import time
import logging
import argparse
import requests
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
GEMINI_RPM_LIMIT = 14          # 15/min max, on garde une marge
GEMINI_RETRY_WAIT = 65         # secondes entre retries sur rate limit

# Seuils pour la détection de changement algorithmique
MIN_DELTA_YEARS = 0.5          # Écart minimum pour considérer un changement


# ─────────────────────────────────────────────────────────────────────────────
# PASSE 1 : ANALYSE ALGORITHMIQUE (Tasks 3, 4)
# ─────────────────────────────────────────────────────────────────────────────

def run_algorithmic_pass(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pour chaque firme, compare les durées de vie année par année
    et détecte les changements de politique.

    Colonnes ajoutées :
        task3_algo        : "Yes" / "No" / "First disclosure" / "Disappeared"
        task4_date        : Année du changement détecté (ou "")
        delta_min         : Écart useful_life_min vs année précédente
        delta_max         : Écart useful_life_max vs année précédente
        first_disclosure  : Première année où la firme a une disclosure
        years_since_change: Nombre d'années depuis le dernier changement
    """
    df = df.sort_values(["firm_cik", "fiscal_year"]).copy()

    # Colonnes de sortie
    df["task3_algo"] = "No"
    df["task4_date"] = ""
    df["delta_min"] = 0.0
    df["delta_max"] = 0.0
    df["first_disclosure"] = ""
    df["years_since_change"] = ""

    for cik, group in df.groupby("firm_cik"):
        group = group.sort_values("fiscal_year")
        idx_list = group.index.tolist()

        # Première disclosure
        disclosed = group[group["useful_life_min_years"].notna()]
        if not disclosed.empty:
            first_yr = str(disclosed.iloc[0]["fiscal_year"])
            df.loc[idx_list, "first_disclosure"] = first_yr

        prev_min = None
        prev_max = None
        prev_had_data = False
        last_change_year = None

        for i, (idx, row) in enumerate(group.iterrows()):
            curr_min = row["useful_life_min_years"]
            curr_max = row["useful_life_max_years"]
            curr_has_data = pd.notna(curr_min)
            fy = row["fiscal_year"]

            if i == 0:
                # Première année de la série
                prev_min = curr_min
                prev_max = curr_max
                prev_had_data = curr_has_data
                continue

            if curr_has_data and not prev_had_data:
                # Première apparition (pas un changement de politique)
                df.at[idx, "task3_algo"] = "First disclosure"

            elif not curr_has_data and prev_had_data:
                # Disparition de la disclosure
                df.at[idx, "task3_algo"] = "Disappeared"

            elif curr_has_data and prev_had_data:
                # Comparer les valeurs
                d_min = (curr_min - prev_min) if pd.notna(prev_min) else 0
                d_max = (curr_max - prev_max) if pd.notna(prev_max) else 0

                df.at[idx, "delta_min"] = round(d_min, 1)
                df.at[idx, "delta_max"] = round(d_max, 1)

                if abs(d_min) >= MIN_DELTA_YEARS or abs(d_max) >= MIN_DELTA_YEARS:
                    df.at[idx, "task3_algo"] = "Yes"
                    df.at[idx, "task4_date"] = str(fy)
                    last_change_year = fy

            # Years since last change
            if last_change_year and curr_has_data:
                try:
                    df.at[idx, "years_since_change"] = int(fy) - int(last_change_year)
                except (ValueError, TypeError):
                    pass

            prev_min = curr_min
            prev_max = curr_max
            prev_had_data = curr_has_data

    # Task 3 finale : combiner algo + extraction
    # Si l'extraction a trouvé "Yes" (texte explicite), ça prime sur l'algo
    mask_extraction_yes = df["policy_change"] == "Yes"
    df.loc[mask_extraction_yes, "task3_algo"] = "Yes (explicit)"

    return df


# ─────────────────────────────────────────────────────────────────────────────
# PASSE 2 : CLASSIFICATION LLM (Task 5 — Gemini)
# ─────────────────────────────────────────────────────────────────────────────

CLASSIFICATION_PROMPT = """Tu es un assistant de recherche en comptabilité financière.
Analyse ce verbatim extrait d'un rapport 10-K (SEC) et réponds UNIQUEMENT en JSON valide.

Verbatim à analyser :
---
{verbatim}
---

Réponds avec ce JSON exact (rien d'autre, pas de markdown, pas de backticks) :
{{
  "disclosure_level": "detailed" ou "vague" ou "not_relevant",
  "justification": "une phrase courte expliquant ton choix",
  "use_cases": ["liste", "des", "cas d'usage IA mentionnés"],
  "capex_mentioned": true ou false,
  "specific_products": ["liste", "des", "produits/modèles spécifiques mentionnés"]
}}

Règles :
- "detailed" = l'entreprise donne des détails précis sur son infrastructure IA (modèles de GPU, investissements chiffrés, capacités de data center)
- "vague" = l'entreprise mentionne l'IA mais sans détails concrets (juste "we use AI" ou "AI-powered solutions")
- "not_relevant" = le texte ne parle pas vraiment d'infrastructure IA (cybersécurité, ERP, logiciel métier, MRO)
- "capex_mentioned" = true si le texte mentionne des dépenses en capital (CapEx, investment, purchase, build) liées à l'IA
- Sois strict : "artificial intelligence" dans un contexte de logiciel métier = "not_relevant"
"""


def call_gemini(prompt: str, api_key: str) -> Optional[dict]:
    """Appel à l'API Gemini avec gestion des erreurs et rate limits."""
    try:
        response = requests.post(
            f"{GEMINI_API_URL}?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,  # Quasi-déterministe
                    "maxOutputTokens": 500,
                },
            },
            timeout=30,
        )

        if response.status_code == 429:
            logging.warning("Rate limit Gemini — attente 65s...")
            time.sleep(GEMINI_RETRY_WAIT)
            return call_gemini(prompt, api_key)  # Retry une fois

        if response.status_code != 200:
            logging.error(f"Gemini HTTP {response.status_code}: {response.text[:200]}")
            return None

        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]

        # Parser le JSON (nettoyer les backticks si Gemini en ajoute)
        text = text.strip()
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        return json.loads(text)

    except json.JSONDecodeError as e:
        logging.error(f"Gemini JSON invalide: {e}")
        return None
    except requests.exceptions.Timeout:
        logging.error("Gemini timeout (30s)")
        return None
    except Exception as e:
        logging.error(f"Gemini erreur: {e}")
        return None


def run_llm_pass(df: pd.DataFrame, output_dir: str) -> pd.DataFrame:
    """
    Envoie les verbatims AI à Gemini pour classification.
    Checkpoint : sauvegarde après chaque appel dans un fichier JSON.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logging.error("GEMINI_API_KEY non trouvée dans .env")
        print("❌ GEMINI_API_KEY manquante. Crée un fichier .env avec :")
        print("   GEMINI_API_KEY=AIzaSyD_...")
        return df

    print(f"🤖 Classification LLM (Gemini {GEMINI_MODEL})")
    print(f"   Rate limit : {GEMINI_RPM_LIMIT} req/min\n")

    # Colonnes LLM
    for col in ["llm_disclosure_level", "llm_justification", "llm_use_cases",
                "llm_capex_mentioned", "llm_specific_products"]:
        if col not in df.columns:
            df[col] = ""

    # Checkpoint : charger les résultats déjà obtenus
    checkpoint_path = Path(output_dir) / "llm_checkpoint.json"
    checkpoint = {}
    if checkpoint_path.exists():
        with open(checkpoint_path, "r") as f:
            checkpoint = json.load(f)
        print(f"   ⏭ Checkpoint : {len(checkpoint)} résultat(s) déjà en cache\n")

    # Filtrer : seulement les lignes avec du texte AI non vide
    mask = (df["ai_infra_text"] != "Not disclosed") & (df["ai_infra_text"].str.len() > 20)
    to_classify = df[mask].copy()

    if to_classify.empty:
        print("   Aucun verbatim AI à classifier.")
        return df

    print(f"   {len(to_classify)} verbatim(s) à classifier\n")

    request_count = 0

    for idx, row in to_classify.iterrows():
        key = f"{row['firm_cik']}_{row['fiscal_year']}"

        # Skip si déjà dans le checkpoint
        if key in checkpoint:
            result = checkpoint[key]
            df.at[idx, "llm_disclosure_level"] = result.get("disclosure_level", "")
            df.at[idx, "llm_justification"] = result.get("justification", "")
            df.at[idx, "llm_use_cases"] = ";".join(result.get("use_cases", []))
            df.at[idx, "llm_capex_mentioned"] = str(result.get("capex_mentioned", ""))
            df.at[idx, "llm_specific_products"] = ";".join(result.get("specific_products", []))
            continue

        # Construire le verbatim combiné (AI + segments)
        verbatim_parts = [row["ai_infra_text"]]
        if pd.notna(row.get("segment_ai_text")) and row["segment_ai_text"] != "Not disclosed":
            verbatim_parts.append(row["segment_ai_text"])
        combined = "\n\n".join(verbatim_parts)

        # Tronquer si trop long (Gemini accepte beaucoup mais on reste raisonnable)
        if len(combined) > 3000:
            combined = combined[:3000] + "..."

        prompt = CLASSIFICATION_PROMPT.format(verbatim=combined)

        # Rate limiting
        request_count += 1
        if request_count > 1 and request_count % GEMINI_RPM_LIMIT == 0:
            print(f"   ⏳ Rate limit pause (60s)...")
            time.sleep(62)

        # Appel
        ticker = row.get("firm_ticker", "?")
        fy = row.get("fiscal_year", "?")
        print(f"   → {ticker} FY{fy}...", end=" ", flush=True)

        result = call_gemini(prompt, api_key)

        if result:
            df.at[idx, "llm_disclosure_level"] = result.get("disclosure_level", "")
            df.at[idx, "llm_justification"] = result.get("justification", "")
            df.at[idx, "llm_use_cases"] = ";".join(result.get("use_cases", []))
            df.at[idx, "llm_capex_mentioned"] = str(result.get("capex_mentioned", ""))
            df.at[idx, "llm_specific_products"] = ";".join(result.get("specific_products", []))

            # Sauver le checkpoint
            checkpoint[key] = result
            os.makedirs(output_dir, exist_ok=True)
            with open(checkpoint_path, "w") as f:
                json.dump(checkpoint, f, indent=2, ensure_ascii=False)

            level = result.get("disclosure_level", "?")
            print(f"✅ {level}")
        else:
            print("❌ échec")

        # Pause inter-requête (4.3s = ~14 req/min)
        time.sleep(4.3)

    # Remplir les lignes sans AI text
    df.loc[~mask, "llm_disclosure_level"] = "no_ai_text"

    return df


# ─────────────────────────────────────────────────────────────────────────────
# PASSE 3 : AGRÉGATION ET RAPPORTS
# ─────────────────────────────────────────────────────────────────────────────

def generate_firm_reports(df: pd.DataFrame, output_dir: str):
    """Génère un rapport TXT par firme."""
    firm_dir = Path(output_dir) / "firm_reports"
    firm_dir.mkdir(parents=True, exist_ok=True)

    for cik, group in df.groupby("firm_cik"):
        group = group.sort_values("fiscal_year")
        ticker = group.iloc[0]["firm_ticker"]
        fpath = firm_dir / f"{cik}_{ticker}.txt"

        with open(fpath, "w", encoding="utf-8") as f:
            f.write(f"{'='*70}\n")
            f.write(f"RAPPORT DE SYNTHÈSE — {ticker} (CIK {cik})\n")
            f.write(f"{'='*70}\n\n")

            # Résumé
            years = group["fiscal_year"].tolist()
            f.write(f"Période couverte : FY{years[0]} → FY{years[-1]}\n")
            f.write(f"Nombre d'exercices : {len(years)}\n\n")

            # Évolution des durées de vie
            f.write(f"─── DURÉES DE VIE (Task 2) ───\n\n")
            f.write(f"{'FY':<8} {'Min':>6} {'Max':>6} {'Catégorie':<40} {'Δ':>8}\n")
            f.write(f"{'─'*70}\n")
            for _, row in group.iterrows():
                mn = row["useful_life_min_years"]
                mx = row["useful_life_max_years"]
                cat = str(row["asset_category"])[:38] if pd.notna(row["asset_category"]) else "N/A"
                mn_s = f"{mn:.0f}" if pd.notna(mn) else "N/A"
                mx_s = f"{mx:.0f}" if pd.notna(mx) else "N/A"
                delta = ""
                d_max = row.get("delta_max", 0)
                if d_max and d_max != 0:
                    delta = f"{d_max:+.0f}"
                f.write(f"FY{row['fiscal_year']:<6} {mn_s:>6} {mx_s:>6} {cat:<40} {delta:>8}\n")
            f.write("\n")

            # Changements détectés
            changes = group[group["task3_algo"].isin(["Yes", "Yes (explicit)"])]
            if not changes.empty:
                f.write(f"─── CHANGEMENTS DE POLITIQUE (Task 3-4) ───\n\n")
                for _, row in changes.iterrows():
                    f.write(f"  ▸ FY{row['fiscal_year']} : ")
                    f.write(f"Δmin={row['delta_min']:+.1f}, Δmax={row['delta_max']:+.1f}")
                    if row["task3_algo"] == "Yes (explicit)":
                        f.write(" [mention explicite dans le 10-K]")
                    f.write("\n")
                    if pd.notna(row.get("policy_change_text")) and row["policy_change_text"]:
                        f.write(f"    Texte : {str(row['policy_change_text'])[:200]}\n")
                f.write("\n")
            else:
                f.write("─── CHANGEMENTS DE POLITIQUE : Aucun détecté ───\n\n")

            # Hardware timeline
            hw_rows = group[group["hardware_count"] > 0]
            if not hw_rows.empty:
                f.write(f"─── HARDWARE TIMELINE (Task 7) ───\n\n")
                for _, row in hw_rows.iterrows():
                    f.write(f"  FY{row['fiscal_year']} : {row['hardware_mentions']}\n")
                f.write("\n")

            # AI Disclosure (LLM)
            ai_rows = group[group.get("llm_disclosure_level", "").isin(
                ["detailed", "vague"]
            )] if "llm_disclosure_level" in group.columns else pd.DataFrame()
            if not ai_rows.empty:
                f.write(f"─── INFRASTRUCTURE IA (Task 5 — Classification LLM) ───\n\n")
                for _, row in ai_rows.iterrows():
                    level = row.get("llm_disclosure_level", "N/A")
                    justif = row.get("llm_justification", "")
                    products = row.get("llm_specific_products", "")
                    f.write(f"  FY{row['fiscal_year']} : [{level.upper()}]\n")
                    if justif:
                        f.write(f"    Justification : {justif}\n")
                    if products:
                        f.write(f"    Produits : {products}\n")
                f.write("\n")

    print(f"📁 Rapports firmes : {firm_dir}/ ({len(df['firm_cik'].unique())} fichier(s))")


def generate_global_report(df: pd.DataFrame, output_dir: str):
    """Génère le rapport de tendances globales."""
    report_path = Path(output_dir) / "rapport_global.txt"
    n_firms = df["firm_cik"].nunique()
    n_obs = len(df)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"{'='*70}\n")
        f.write(f"RAPPORT GLOBAL — AI Infrastructure Investment\n")
        f.write(f"{'='*70}\n\n")
        f.write(f"Échantillon : {n_firms} firmes, {n_obs} observations firme-année\n")
        fy_range = f"FY{df['fiscal_year'].min()} → FY{df['fiscal_year'].max()}"
        f.write(f"Période : {fy_range}\n\n")

        # 1. Taux de disclosure
        f.write(f"─── 1. TAUX DE DISCLOSURE (Task 2) ───\n\n")
        for fy, grp in df.groupby("fiscal_year"):
            total = len(grp)
            disclosed = grp["useful_life_min_years"].notna().sum()
            pct = disclosed / total * 100 if total > 0 else 0
            f.write(f"  FY{fy} : {disclosed}/{total} ({pct:.0f}%)\n")
        f.write("\n")

        # 2. Durée de vie moyenne par année
        f.write(f"─── 2. DURÉE DE VIE MOYENNE PAR ANNÉE ───\n\n")
        f.write(f"  {'FY':<8} {'Moy Min':>10} {'Moy Max':>10} {'Méd Max':>10} {'N':>6}\n")
        f.write(f"  {'─'*50}\n")
        for fy, grp in df.groupby("fiscal_year"):
            valid = grp[grp["useful_life_min_years"].notna()]
            if valid.empty:
                continue
            avg_min = valid["useful_life_min_years"].mean()
            avg_max = valid["useful_life_max_years"].mean()
            med_max = valid["useful_life_max_years"].median()
            f.write(f"  FY{fy:<6} {avg_min:>10.1f} {avg_max:>10.1f} {med_max:>10.1f} {len(valid):>6}\n")
        f.write("\n")

        # 3. Changements de politique
        changes = df[df["task3_algo"].isin(["Yes", "Yes (explicit)"])]
        f.write(f"─── 3. CHANGEMENTS DE POLITIQUE (Task 3) ───\n\n")
        f.write(f"  Total : {len(changes)} changement(s) détecté(s)\n")
        if not changes.empty:
            f.write(f"\n  Par année :\n")
            for fy, grp in changes.groupby("fiscal_year"):
                f.write(f"    FY{fy} : {len(grp)} firme(s)\n")
            f.write(f"\n  Détail :\n")
            for _, row in changes.iterrows():
                f.write(f"    {row['firm_ticker']} FY{row['fiscal_year']} : "
                        f"Δmin={row['delta_min']:+.1f}, Δmax={row['delta_max']:+.1f}\n")
        f.write("\n")

        # 4. Hardware adoption
        f.write(f"─── 4. ADOPTION HARDWARE (Task 7) ───\n\n")
        hw = df[df["hardware_count"] > 0]
        if not hw.empty:
            f.write(f"  Firmes mentionnant du hardware : {hw['firm_cik'].nunique()}\n\n")
            # Compter les modèles par année
            f.write(f"  {'FY':<8} {'Firmes':>8} {'Modèles les plus cités':<50}\n")
            f.write(f"  {'─'*65}\n")
            for fy, grp in hw.groupby("fiscal_year"):
                all_models = ";".join(grp["hardware_mentions"].dropna()).split(";")
                from collections import Counter
                top = Counter(all_models).most_common(5)
                top_str = ", ".join(f"{m}({c})" for m, c in top if m)
                f.write(f"  FY{fy:<6} {len(grp):>8} {top_str:<50}\n")
        else:
            f.write("  Aucune mention de hardware spécifique.\n")
        f.write("\n")

        # 5. AI Disclosure (LLM)
        if "llm_disclosure_level" in df.columns:
            f.write(f"─── 5. CLASSIFICATION AI DISCLOSURE (Task 5 — LLM) ───\n\n")
            for level in ["detailed", "vague", "not_relevant", "no_ai_text"]:
                count = (df["llm_disclosure_level"] == level).sum()
                pct = count / n_obs * 100
                f.write(f"  {level:<20} : {count:>6} ({pct:.1f}%)\n")
            f.write("\n")

            # Évolution dans le temps
            f.write(f"  Évolution 'detailed' par année :\n")
            for fy, grp in df.groupby("fiscal_year"):
                total = len(grp)
                detailed = (grp["llm_disclosure_level"] == "detailed").sum()
                pct = detailed / total * 100 if total > 0 else 0
                bar = "█" * int(pct / 2)
                f.write(f"    FY{fy} : {detailed:>4}/{total:<4} ({pct:>5.1f}%) {bar}\n")
            f.write("\n")

    print(f"📊 Rapport global : {report_path}")


def export_synthesis(df: pd.DataFrame, output_dir: str):
    """Exporte le CSV analytique final."""
    os.makedirs(output_dir, exist_ok=True)

    # CSV analytique (colonnes sélectionnées, sans les verbatims lourds)
    cols_synthesis = [
        "firm_cik", "firm_ticker", "fiscal_year", "period_end_date",
        "filing_date",
        # Task 2
        "asset_category", "useful_life_min_years", "useful_life_max_years",
        "source_section", "section_title", "confidence",
        # Task 3-4 (algo)
        "task3_algo", "task4_date", "delta_min", "delta_max",
        "policy_change", "first_disclosure", "years_since_change",
        # Task 5 (LLM)
        "llm_disclosure_level", "llm_justification", "llm_use_cases",
        "llm_capex_mentioned", "llm_specific_products",
        "ai_infra_source",
        # Task 6
        "segment_ai_text",
        # Task 7
        "hardware_mentions", "hardware_count",
        # QA
        "ambiguity_flag", "ambiguity_detail",
        "extraction_status", "error_message",
    ]

    # Garder seulement les colonnes qui existent
    cols_present = [c for c in cols_synthesis if c in df.columns]
    df_out = df[cols_present].copy()

    csv_path = Path(output_dir) / "synthesis_results.csv"
    df_out.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"✅ CSV synthèse : {csv_path} ({len(df_out)} lignes, {len(cols_present)} colonnes)")

    # JSON complet (pour debug / archive)
    json_path = Path(output_dir) / "synthesis_results.json"
    df_out.to_json(json_path, orient="records", indent=2, force_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Synthèse 10-K — Analyse algorithmique + Classification LLM"
    )
    parser.add_argument("--input", type=str, required=True,
                        help="CSV d'extraction (extraction_results_global.csv)")
    parser.add_argument("--output", type=str, default="./synthesis",
                        help="Dossier de sortie")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip la passe LLM (algo seul)")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Charger le CSV
    if not Path(args.input).exists():
        print(f"❌ Fichier introuvable : {args.input}")
        return

    print(f"\n📂 Chargement de {args.input}...")
    df = pd.read_csv(args.input)
    print(f"   {len(df)} lignes, {df['firm_cik'].nunique()} firmes\n")

    # Passe 1 : Algo
    print(f"{'='*60}")
    print(f"📐 PASSE 1 — Analyse algorithmique (Tasks 3-4)")
    print(f"{'='*60}\n")
    df = run_algorithmic_pass(df)

    changes = df[df["task3_algo"].isin(["Yes", "Yes (explicit)"])]
    print(f"  ✅ {len(changes)} changement(s) de politique détecté(s)")
    first_disc = df[df["task3_algo"] == "First disclosure"]
    print(f"  ✅ {len(first_disc)} première(s) disclosure(s)")
    print()

    # Passe 2 : LLM
    os.makedirs(args.output, exist_ok=True)
    if not args.no_llm:
        print(f"{'='*60}")
        print(f"🤖 PASSE 2 — Classification LLM Gemini (Task 5)")
        print(f"{'='*60}\n")
        df = run_llm_pass(df, args.output)
        print()
    else:
        print("⏭ Passe LLM skippée (--no-llm)\n")
        for col in ["llm_disclosure_level", "llm_justification", "llm_use_cases",
                     "llm_capex_mentioned", "llm_specific_products"]:
            if col not in df.columns:
                df[col] = ""

    # Passe 3 : Agrégation
    print(f"{'='*60}")
    print(f"📊 PASSE 3 — Agrégation et rapports")
    print(f"{'='*60}\n")

    export_synthesis(df, args.output)
    generate_firm_reports(df, args.output)
    generate_global_report(df, args.output)

    print(f"\n✅ Synthèse terminée. Résultats dans {args.output}/")


if __name__ == "__main__":
    main()