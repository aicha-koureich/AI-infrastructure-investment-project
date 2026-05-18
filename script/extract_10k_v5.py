"""
extract_10k_v5.py
=================
Extracteur robuste pour rapports 10-K iXBRL (format SEC EDGAR)
Projet : Durées de vie des actifs IT / Infrastructure IA

Testé sur : AIR FY2025, AMD FY2013, AMD FY2024, APD FY2020

Usage :
    # Fichier unique
    python extract_10k_v5.py --input 0000001750_AIR_FY2025_10K.html

    # Dossier local (HTML en vrac)
    python extract_10k_v5.py --folder ./filings/ --output ./results/

    # Google Drive monté via rclone (lit les ZIP sans extraction)
    python extract_10k_v5.py --drive ~/google_drive/AI_Infrastructure_Investment_Project/raw_filings --output ./results/

    # Un seul batch
    python extract_10k_v5.py --drive ~/google_drive/.../raw_filings --batch batch_1.zip --output ./results/

Sorties :
    ./results/
    ├── extraction_results.csv          # CSV global (toutes les firmes)
    ├── extraction_results.json         # JSON global
    ├── replication_log.csv             # Log des cas ambigus/erreurs (Section 13)
    └── verbatims/                      # Un fichier TXT par firme-année
        ├── 0000001750_AIR_FY2025.txt
        ├── 0000002488_AMD_FY2013.txt
        └── ...
"""

import os
import re
import sys
import json
import logging
import argparse
import hashlib
import traceback
import zipfile
import subprocess
import shutil
import tempfile
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd
from bs4 import BeautifulSoup, Tag, NavigableString

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION CENTRALE
# ─────────────────────────────────────────────────────────────────────────────

# Asset scope : équipements IT / Cloud / IA (Section 2.3 du manuel)
ASSET_SCOPE = [
    r"\bserver(s)?\b",
    r"\bcomputing equipment\b",
    r"\bcomputer hardware\b",
    r"\bdata center(s)?\b",
    r"\bgpu(s)?\b",
    r"\bhigh[- ]performance computing\b",
    r"\bartificial intelligence\b",
    r"\bmachine learning\b",
    r"\bcloud infrastructure\b",
    r"\bcapitalized software\b",
    r"\binformation technology\b",
    r"\bit equipment\b",
    r"\bnetwork equipment\b",
    # Patterns composites fréquents dans les notes PPE
    r"\bequipment[,\s]+furniture\b",
    r"\bfurniture[,\s]+fixtures?\b.*\bcapitalized\b",
    r"\bequipment\b.*\bsoftware\b",
]

# Mots-clés durée de vie (Task 2 du manuel)
USEFUL_LIFE_KW = [
    r"\buseful li(fe|ves)\b",
    r"\bestimated li(fe|ves)\b",
    r"\bdepreciat(ed|ion|ing)\b",
    r"\bamortiz(ed|ation|ing)\b",
    r"\bstraight[- ]line\b",
]

# Mots-clés IA/cloud (Task 5 du manuel)
AI_KW = [
    r"\bartificial intelligence\b",
    r"\bmachine learning\b",
    r"\bgpu(s)?\b",
    r"\bdata center(s)?\b",
    r"\bcloud infrastructure\b",
    r"\bai[- ]?powered\b",
    r"\bneural network(s)?\b",
    r"\bhigh[- ]performance computing\b",
]

# v5 : Patterns de hardware spécifique (Task 7 — Hardware timeline)
# Permet la détection "early adopter" vs "late adopter"
HARDWARE_MODELS = {
    # NVIDIA
    "NVIDIA": r"\bnvidia\b",
    "H100": r"\bh100\b",
    "H200": r"\bh200\b",
    "A100": r"\ba100\b",
    "V100": r"\bv100\b",
    "B100": r"\bb100\b",
    "B200": r"\bb200\b",
    "GB200": r"\bgb200\b",
    "Blackwell": r"\bblackwell\b",
    "Hopper": r"\bhopper\b",
    "DGX": r"\bdgx\b",
    "HGX": r"\bhgx\b",
    # AMD
    "AMD_Instinct": r"\binstinct\b",
    "AMD_Radeon": r"\bradeon\b",
    "AMD_EPYC": r"\bepyc\b",
    "AMD_Ryzen": r"\bryzen\b",
    "MI300": r"\bmi300\b",
    "MI325": r"\bmi325\b",
    "MI350": r"\bmi350\b",
    # Intel
    "Intel_Xeon": r"\bxeon\b",
    "Intel_Gaudi": r"\bgaudi\s*\d?\b",
    # Google
    "TPU": r"\btpu(s)?\b|\btensor processing unit\b",
    # AWS
    "Trainium": r"\btrainium\b",
    "Inferentia": r"\binferentia\b",
}

# v5 : Mots-clés Task 6 (Segment-level computing/AI mentions)
SEGMENT_AI_KW = [
    r"\bcloud (?:segment|business|services|computing|platform)\b",
    r"\bai (?:segment|business|services|solutions|platform|products)\b",
    r"\bdata center (?:segment|business|services)\b",
    r"\b(?:azure|aws|gcp|google cloud)\b",
    r"\bcomputing (?:segment|business)\b",
]

# Exclusions strictes (Section 2.3 du manuel)
HARD_EXCLUSIONS = [
    r"\bvehicle(s)?\b",
    r"\baircraft\b",
    r"\bland\b",
]

# v4 : Patterns de changement de politique (Task 3 du manuel)
POLICY_CHANGE_PATTERNS = [
    r"(?i)change[ds]?\s+(the|its|our)\s+(estimated\s+)?useful\s+li(fe|ves)",
    r"(?i)revise[ds]?\s+(the|its|our)\s+(estimated\s+)?useful\s+li(fe|ves)",
    r"(?i)change\s+in\s+(accounting\s+)?estimate",
    r"(?i)modif(y|ied|ication)\s+.{0,30}useful\s+li(fe|ves)",
    r"(?i)extend(ed|ing)\s+.{0,30}useful\s+li(fe|ves)",
    r"(?i)shorten(ed|ing)\s+.{0,30}useful\s+li(fe|ves)",
    r"(?i)prospective(ly)?\s+(change|adjust|adopt|effect)",
    r"(?i)depreciation\s+.{0,30}(change|revis|modif|adjust)",
    r"(?i)(increase|decrease|reduce)[ds]?\s+.{0,30}(estimated\s+)?useful\s+li(fe|ves)",
    r"(?i)reassess(ed|ment)?\s+.{0,30}useful\s+li(fe|ves)",
]

# Patterns de durée de vie (extraction structurée)
DURATION_PATTERN = re.compile(
    r"""
    (?P<min>\d+(?:\.\d+)?)\s*       # min (ex: 3)
    (?:[-–—\s]+(?:to\s+)?)?         # séparateur optionnel
    (?P<max>\d+(?:\.\d+)?)?\s*      # max optionnel (ex: 10)
    \s*years?                        # "year" ou "years"
    """,
    re.VERBOSE | re.IGNORECASE,
)

# v4 : Conversion des nombres écrits en lettres → chiffres
# Couvre "one" à "fifty" (plage réaliste pour les durées de vie d'actifs)
WORD_TO_NUM = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "twenty-one": "21", "twenty-two": "22",
    "twenty-three": "23", "twenty-four": "24", "twenty-five": "25",
    "thirty": "30", "thirty-five": "35", "forty": "40", "forty-five": "45",
    "fifty": "50",
}

# Regex pour trouver les nombres en lettres suivis de "year(s)"
# Ex: "two to six years", "three years", "twenty-five years"
_word_num_re = re.compile(
    r'\b(' + '|'.join(sorted(WORD_TO_NUM.keys(), key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)


def normalize_word_numbers(text: str) -> str:
    """
    Convertit les nombres écrits en lettres en chiffres dans un texte.
    Ex: "two to six years" → "2 to 6 years"
    Seuls les nombres dans un contexte de durée sont convertis (proches de "year").
    """
    def _replace_near_year(match):
        word = match.group(1).lower()
        # Vérifier que "year" est proche (dans les 30 caractères suivants)
        after = text[match.end():match.end() + 40].lower()
        before = text[max(0, match.start() - 40):match.start()].lower()
        if 'year' in after or 'year' in before:
            return WORD_TO_NUM.get(word, match.group(0))
        return match.group(0)

    return _word_num_re.sub(_replace_near_year, text)


# ─────────────────────────────────────────────────────────────────────────────
# DATACLASS DE RÉSULTAT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExtractionResult:
    firm_cik: str
    firm_ticker: str
    fiscal_year: str
    filename: str
    # Task 1 : Chronologie (v5)
    filing_date: str = ""                        # v5 : date de publication
    period_end_date: str = ""                    # v5 : fin d'exercice fiscal
    # Task 2 : durée de vie
    asset_category: str = "Not disclosed"
    useful_life_text: str = "Not disclosed"
    useful_life_min_years: Optional[float] = None
    useful_life_max_years: Optional[float] = None
    # Task 3 : changement de politique
    policy_change: str = "Not disclosed"
    policy_change_text: str = ""
    # Task 5 : infrastructure IA
    ai_infra_text: str = "Not disclosed"
    ai_infra_source: str = ""
    # Task 6 : Segment-level (v5)
    segment_ai_text: str = "Not disclosed"       # v5 : mentions de segments cloud/IA
    # Task 7 : Hardware timeline (v5)
    hardware_mentions: str = ""                  # v5 : liste séparée par ; des modèles trouvés
    hardware_count: int = 0                      # v5 : nombre de modèles distincts
    # Méta-qualité
    source_section: str = "unknown"
    section_title: str = ""
    confidence: int = 0
    ambiguity_flag: bool = False
    ambiguity_detail: str = ""
    # Infos d'isolation Item 8
    item8_method: str = ""
    item8_segment_count: int = 0
    # Log QA
    extraction_status: str = "OK"
    error_message: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 1 : NETTOYAGE iXBRL
# ─────────────────────────────────────────────────────────────────────────────

def clean_ixbrl(soup: BeautifulSoup) -> BeautifulSoup:
    """Nettoie les balises iXBRL en préservant la structure DOM."""
    for tag in soup(["script", "style", "meta"]):
        tag.decompose()
    for tag in soup.find_all(re.compile(r'^ix:', re.IGNORECASE)):
        tag.unwrap()
    return soup


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 2 : ISOLATION DE SECTIONS (Item 7 + Item 8)
# ─────────────────────────────────────────────────────────────────────────────

def _is_real_section_header(tag: Tag, text: str) -> int:
    """Score un tag comme vrai titre de section (pas TOC)."""
    score = 0
    if text.strip().startswith("ITEM"):
        score += 3
    if tag.name == 'b':
        score += 2
    elif tag.find('b'):
        score += 2
    style = tag.get('style', '') or ''
    if 'font-weight:bold' in style or 'font-weight: bold' in style:
        score += 2
    if not tag.find_parent('a') and tag.name != 'a':
        score += 1
    if not tag.find_parent('td'):
        score += 1
    prev = tag.find_previous_sibling()
    if prev and prev.name == 'a' and prev.get('id'):
        score += 2
    return score


def _collect_item_candidates(soup: BeautifulSoup, item_re, all_tags=None):
    """Collecte toutes les occurrences d'un Item avec scoring."""
    if all_tags is None:
        all_tags = list(soup.find_all(True))
    candidates = []
    for i, tag in enumerate(all_tags):
        text = tag.get_text(" ", strip=True)
        if len(text) > 500:
            continue
        if item_re.match(text):
            candidates.append({
                "index": i,
                "tag": tag,
                "text": text[:100],
                "header_score": _is_real_section_header(tag, text),
            })
    return candidates


def isolate_section_by_items(soup: BeautifulSoup,
                              start_re, end_re,
                              label: str = "section") -> tuple[Optional[BeautifulSoup], str]:
    """
    Isole une section du document entre deux marqueurs Item.
    Gère le piège TOC via scoring + validation de zone.
    """
    all_tags = list(soup.find_all(True))
    start_candidates = _collect_item_candidates(soup, start_re, all_tags)
    end_candidates = _collect_item_candidates(soup, end_re, all_tags)

    if not start_candidates:
        return None, f"NO_{label.upper()}_FOUND"

    # Trier par score décroissant, puis index décroissant
    start_candidates.sort(key=lambda c: (c["header_score"], c["index"]), reverse=True)
    best_start = start_candidates[0]

    # Trouver le premier end_candidate après best_start avec score > 0
    start_idx = best_start["index"]
    best_end_idx = len(all_tags)

    real_ends = [c for c in end_candidates
                 if c["index"] > start_idx and c["header_score"] > 3]
    if real_ends:
        real_ends.sort(key=lambda c: c["index"])
        best_end_idx = real_ends[0]["index"]
    else:
        any_ends = [c for c in end_candidates if c["index"] > start_idx]
        if any_ends:
            any_ends.sort(key=lambda c: c["index"])
            best_end_idx = any_ends[0]["index"]

    # Validation de zone
    zone_size = best_end_idx - start_idx
    method = f"score={best_start['header_score']},idx={start_idx},zone={zone_size}"

    if zone_size < 100:
        # Fallback : dernière occurrence
        for fb in sorted(start_candidates, key=lambda c: c["index"], reverse=True):
            if fb["index"] == best_start["index"]:
                continue
            fb_ends = [c for c in end_candidates if c["index"] > fb["index"]]
            fb_end = fb_ends[0]["index"] if fb_ends else len(all_tags)
            if fb_end - fb["index"] > 100:
                best_start = fb
                best_end_idx = fb_end
                start_idx = fb["index"]
                method = f"fallback,idx={start_idx},zone={fb_end - start_idx}"
                break

    # Extraction par position dans le HTML brut
    start_tag = all_tags[start_idx]
    end_tag = all_tags[best_end_idx] if best_end_idx < len(all_tags) else None

    html_str = str(soup)
    start_str = str(start_tag)[:200]
    start_pos = html_str.find(start_str)
    if start_pos == -1:
        start_pos = 0

    if end_tag is not None:
        end_str = str(end_tag)[:200]
        end_pos = html_str.find(end_str, start_pos + 1)
        if end_pos == -1:
            end_pos = len(html_str)
    else:
        end_pos = len(html_str)

    section_soup = BeautifulSoup(html_str[start_pos:end_pos], "html.parser")
    return section_soup, method


def isolate_item8(soup: BeautifulSoup) -> tuple[Optional[BeautifulSoup], str]:
    """Isole Item 8 (Financial Statements)."""
    return isolate_section_by_items(
        soup,
        re.compile(r'(?i)item\s*8[\.\s]'),
        re.compile(r'(?i)item\s*9[\.\s]'),
        label="ITEM8",
    )


def isolate_item7(soup: BeautifulSoup) -> tuple[Optional[BeautifulSoup], str]:
    """Isole Item 7 (MD&A) — source supplémentaire pour Task 5."""
    return isolate_section_by_items(
        soup,
        re.compile(r'(?i)item\s*7[\.\s]'),
        re.compile(r'(?i)item\s*7a[\.\s]|item\s*8[\.\s]'),
        label="ITEM7",
    )


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 3 : EXTRACTION DES SEGMENTS TEXTUELS (avec section hiérarchique)
# ─────────────────────────────────────────────────────────────────────────────

def extract_text_segments(soup_section: BeautifulSoup,
                          source_label: str = "item_8") -> list[dict]:
    """
    Extrait tous les segments textuels avec déduplication et tracking
    hiérarchique de section.

    v4 : la section est déterminée par le titre de note NUMÉROTÉ le plus
    récent (ex: "1. Summary of Significant Accounting Policies"), pas
    par un sous-titre interne comme "Property, Plant and Equipment".
    """
    segments = []
    seen_hashes = set()

    # v4 : section hiérarchique à deux niveaux
    current_note_section = f"{source_label}_other"  # note-level
    current_note_title = ""                          # titre verbatim de la note
    current_sub_heading = ""                         # sous-titre (informatif seulement)
    _pending_note_classify = False                   # attend le titre après "NOTE X:"

    # Patterns pour titres de NOTES numérotées
    # Formats observés :
    #   AIR  : "1. Summary of Significant Accounting Policies"
    #   AMD  : "NOTE 2: Summary of..." ou "NOTE 2:" (titre séparé)
    #   APD  : "1. MAJOR ACCOUNTING POLICIES"
    note_header_re = re.compile(
        r"^(?:\d+\.\s+|NOTE\s+\d+|Note\s+\d+)",
        re.IGNORECASE,
    )

    # Patterns pour classifier le type de note
    note_type_patterns = {
        "significant_accounting_policies": re.compile(
            r"(?i)significant accounting polic|summary of significant|"
            r"basis of presentation|major accounting polic"
        ),
        "ppe_note": re.compile(
            r"(?i)^(?:note\s+\d+|^\d+\.)\s*.*(?:property[,\s]+plant|property and equipment|fixed assets)"
        ),
        "notes_to_financials": re.compile(
            r"(?i)notes? to (consolidated )?financial"
        ),
    }

    # Patterns pour sous-titres PPE (informatifs, ne changent pas la note)
    sub_heading_patterns = {
        "ppe_sub": re.compile(
            r"(?i)^property[,\s]+plant and equipment|^property and equipment|^fixed assets"
        ),
        "intangible_sub": re.compile(
            r"(?i)^intangible assets|^goodwill"
        ),
        "depreciation_sub": re.compile(
            r"(?i)^depreciation|^amortization"
        ),
    }

    leaf_tags = list(soup_section.find_all(["p", "td", "li",
                                             "h1", "h2", "h3", "h4",
                                             "font"]))
    container_tags = list(soup_section.find_all(["div", "span"]))

    # v5 : taille max d'un segment (évite les div conteneurs géants
    # type AMD FY2013 où un <font> de 3500 chars contient l'intégralité
    # de la politique comptable + acquisition SeaMicro)
    MAX_SEGMENT_LEN = 2000

    for tag in leaf_tags + container_tags:
        text = tag.get_text(" ", strip=True)
        text = re.sub(r"\s{2,}", " ", text).strip()

        if not text:
            continue

        # v5 : rejeter les segments trop longs (conteneurs)
        if len(text) > MAX_SEGMENT_LEN:
            # Mais toujours permettre le tracking de section
            is_note_header = note_header_re.match(text[:200])
            if is_note_header:
                text_start = text[:200]
                for section_name, pat in note_type_patterns.items():
                    if pat.search(text_start):
                        current_note_section = section_name
                        current_note_title = text_start.strip()[:150]
                        break
            continue

        # v4 : les titres de notes courts ("NOTE 2:") doivent passer
        # pour mettre à jour le tracking de section, même s'ils sont
        # trop courts pour être un segment utile
        is_note_header = note_header_re.match(text)

        if len(text) < 20 and not is_note_header:
            continue

        # Déduplication
        h = hashlib.md5(text[:200].encode()).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)

        # v4 : mise à jour de la section — seulement si c'est un titre de note numéroté
        text_start = text[:200]
        if is_note_header:
            # C'est un titre de note numéroté → mettre à jour la section
            matched_section = False
            for section_name, pat in note_type_patterns.items():
                if pat.search(text_start):
                    current_note_section = section_name
                    current_note_title = text_start.strip()[:150]
                    matched_section = True
                    break

            if not matched_section:
                # Le titre n'est peut-être que "NOTE 2:" (AMD format)
                # Marquer qu'on attend le titre complet dans le prochain tag
                current_note_title = text_start.strip()[:150]
                _pending_note_classify = True
                if current_note_section == f"{source_label}_other":
                    current_note_section = "notes_to_financials"

        # v4 : si le tag précédent était un "NOTE X:" sans titre,
        # essayer de classifier depuis ce tag-ci
        elif _pending_note_classify and len(text) < 300:
            for section_name, pat in note_type_patterns.items():
                if pat.search(text_start):
                    current_note_section = section_name
                    current_note_title = current_note_title + " " + text_start.strip()[:100]
                    break
            _pending_note_classify = False

        # Mise à jour du sous-titre (informatif seulement)
        for sub_name, sub_pat in sub_heading_patterns.items():
            if sub_pat.match(text_start) and len(text) < 200:
                current_sub_heading = sub_name
                break

        segments.append({
            "text": text,
            "section_hint": current_note_section,
            "section_title": current_note_title,
            "sub_heading": current_sub_heading,
            "tag_type": tag.name,
            "source_label": source_label,
        })

    return segments


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 4 : FILTRAGE ET SCORING
# ─────────────────────────────────────────────────────────────────────────────

def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pat, text, re.IGNORECASE) for pat in patterns)


def _has_it_scope(text: str) -> bool:
    return _matches_any(text, ASSET_SCOPE)


def _has_useful_life_kw(text: str) -> bool:
    return _matches_any(text, USEFUL_LIFE_KW)


def _has_ai_kw(text: str) -> bool:
    return _matches_any(text, AI_KW)


def _is_hard_excluded(text: str) -> bool:
    has_exclusion = _matches_any(text, HARD_EXCLUSIONS)
    if not has_exclusion:
        return False
    return not _has_it_scope(text)


def section_confidence(section_hint: str) -> int:
    return {
        "significant_accounting_policies": 3,
        "ppe_note": 3,
        "notes_to_financials": 2,
        "item_8_other": 1,
        "item_7_other": 1,  # MD&A — confiance plus basse
        "unknown": 0,
    }.get(section_hint, 1)


def filter_segments(segments: list[dict]) -> dict:
    """Filtre les segments selon les critères du manuel."""
    useful_life_candidates = []
    ai_infra_candidates = []

    for seg in segments:
        text = seg["text"]
        section = seg["section_hint"]

        if _is_hard_excluded(text):
            continue

        has_it = _has_it_scope(text)
        has_ul = _has_useful_life_kw(text)
        has_ai = _has_ai_kw(text)

        # v4 : exclusion universelle des contextes d'intangibles
        # (brevets, customer relationships, goodwill, etc.)
        # Appliquée à TOUS les cas, pas seulement Cas 2
        text_lower = text.lower()
        intangible_context = any(kw in text_lower for kw in [
            "intangible", "customer relationship", "goodwill",
            "trade name", "trademark", "patent", "developed technology",
            "business combination", "purchase price allocation",
        ])

        # Cas 1 : durée de vie + scope IT explicite
        # (mais pas si le paragraphe parle principalement d'intangibles)
        if has_ul and has_it:
            # Si contexte intangible, ne retenir que si des mots PPE concrets
            # sont aussi présents (ex: "equipment" + "intangible" dans le même
            # paragraphe de politiques comptables = OK, c'est un résumé global)
            if intangible_context:
                has_concrete_ppe = any(kw in text_lower for kw in [
                    "equipment", "machinery", "furniture", "hardware",
                ])
                if has_concrete_ppe:
                    useful_life_candidates.append({
                        **seg,
                        "confidence": section_confidence(section),
                        "match_type": "explicit_it_scope",
                    })
                # Sinon : skip (c'est un texte d'acquisition d'intangibles)
            else:
                useful_life_candidates.append({
                    **seg,
                    "confidence": section_confidence(section),
                    "match_type": "explicit_it_scope",
                })

        # Cas 2 : durée de vie dans section accounting policies / PPE
        # MAIS pas dans un contexte d'actifs incorporels (intangibles)
        elif has_ul and section in ("significant_accounting_policies", "ppe_note"):
            if not intangible_context and any(kw in text_lower for kw in
                   ["equipment", "software", "computer", "server",
                    "hardware"]):
                useful_life_candidates.append({
                    **seg,
                    "confidence": section_confidence(section),
                    "match_type": "ppe_context",
                })

        # Cas 3 : cellule de tableau PPE (supporte aussi nombres en lettres)
        elif seg["tag_type"] == "td":
            normalized_text = normalize_word_numbers(text)
            if re.search(r'\d+\s*[-–]\s*\d+\s*years?', normalized_text, re.I):
                if not intangible_context and any(kw in text_lower for kw in
                       ["equipment", "software", "computer", "server"]):
                    useful_life_candidates.append({
                        **seg,
                        "confidence": max(1, section_confidence(section)),
                        "match_type": "table_cell",
                    })

        # Task 5 : infrastructure IA
        if has_ai:
            ai_infra_candidates.append({
                **seg,
                "confidence": section_confidence(section),
            })

    # v4 fix : trier par (confidence, match_type_rank) pour que
    # explicit_it_scope > ppe_context > table_cell à confiance égale
    match_type_rank = {"explicit_it_scope": 3, "ppe_context": 2, "table_cell": 1}
    useful_life_candidates.sort(
        key=lambda x: (x["confidence"], match_type_rank.get(x["match_type"], 0)),
        reverse=True,
    )
    ai_infra_candidates.sort(key=lambda x: x["confidence"], reverse=True)

    return {
        "useful_life": useful_life_candidates,
        "ai_infra": ai_infra_candidates,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 5 : EXTRACTION STRUCTURÉE DES DURÉES + VERBATIM TRIMMING
# ─────────────────────────────────────────────────────────────────────────────

def extract_duration_values(text: str) -> list[dict]:
    """Extrait toutes les plages de durée (min, max) depuis un texte."""
    # v4 : normaliser les nombres en lettres avant extraction
    text = normalize_word_numbers(text)
    results = []
    for m in DURATION_PATTERN.finditer(text):
        start = m.start()
        prefix = text[max(0, start - 15):start].strip().lower()
        if re.search(r'(note|item|page|#|no\.|number)\s*$', prefix):
            continue
        val_min = float(m.group("min"))
        val_max = float(m.group("max")) if m.group("max") else None
        if val_min <= 0 or val_min > 50:
            continue
        if val_max is not None and (val_max <= 0 or val_max > 50):
            continue
        entry = {"min": val_min, "max": val_max}
        if entry not in results:
            results.append(entry)
    return results


def extract_it_relevant_duration(text: str) -> Optional[dict]:
    """
    Extrait la plage de durée associée aux actifs IT dans un texte
    contenant potentiellement plusieurs plages.
    """
    # v4 : normaliser les nombres en lettres
    normalized = normalize_word_numbers(text)

    it_words = (
        r'equipment|software|computer|server|hardware|technology|'
        r'data center|computing|network'
    )

    # Tentative 0 (prioritaire) : "X to Y years for <IT_word>"
    # Ex: "two to six years for equipment", "3-10 years for equipment"
    for m in re.finditer(
        r'(?i)(\d+(?:\.\d+)?)\s*[-–—\s]+(?:to\s+)?(\d+(?:\.\d+)?)\s*years?\s+for\s+'
        r'(?:' + it_words + r')',
        normalized
    ):
        return {"min": float(m.group(1)), "max": float(m.group(2))}

    # Tentative 0b : "X years for <IT_word>" (sans range)
    for m in re.finditer(
        r'(?i)(\d+(?:\.\d+)?)\s*years?\s+for\s+'
        r'(?:' + it_words + r')',
        normalized
    ):
        return {"min": float(m.group(1)), "max": None}

    # Tentative 1 : "<IT_word> ... X-Y years" (asset→durée)
    for m in re.finditer(
        r'(?i)(' + it_words + r')'
        r'[^.]{0,80}?'
        r'(\d+(?:\.\d+)?)\s*[-–—\s]+(?:to\s+)?(\d+(?:\.\d+)?)\s*years?',
        normalized
    ):
        return {"min": float(m.group(2)), "max": float(m.group(3))}

    # Tentative 1b : "<IT_word> ... X years" (sans max)
    for m in re.finditer(
        r'(?i)(' + it_words + r')'
        r'[^.]{0,80}?'
        r'(\d+(?:\.\d+)?)\s*years?',
        normalized
    ):
        return {"min": float(m.group(2)), "max": None}

    # Tentative 2 : plages <= 15 ans (typique IT)
    all_durations = extract_duration_values(text)
    it_durations = [d for d in all_durations
                    if (d["max"] if d["max"] is not None else d["min"]) <= 15]
    if it_durations:
        with_range = [d for d in it_durations if d["max"] is not None]
        return with_range[0] if with_range else it_durations[0]

    return all_durations[0] if all_durations else None


def extract_asset_category(text: str) -> str:
    """
    v4 : extrait la catégorie d'actif depuis le verbatim.
    Ex : "10-40 years for buildings and improvements and 3-10 years for
          equipment, furniture and fixtures, and capitalized software"
        → "equipment, furniture and fixtures, and capitalized software"
    """
    # v4 : normaliser les nombres en lettres
    normalized = normalize_word_numbers(text)

    # Pattern : "X-Y years for <IT category>" — on cherche spécifiquement
    # le "years for" qui précède un mot IT-scope
    it_words = (
        r"(?:equipment|software|computer|server|hardware|technology|"
        r"data center|computing|network|it\b)"
    )
    m = re.search(
        r'(?i)\d+\s*(?:[-–—]\s*\d+)?\s*years?\s+for\s+'
        r'(' + it_words + r'.+?)(?:\.\s|\band\s+\d|,\s*\d|\.\s*$|$)',
        normalized
    )
    if m:
        category = m.group(1).strip().rstrip('.').rstrip(',')
        # Couper aux limites de clause (autre durée, leasehold, etc.)
        category = re.split(
            r'(?i);\s|\bwhich\b|\bthat\b|\bLeasehold\b|'
            r'\band\s+\d+\s*(?:[-–—]\s*\d+)?\s*years?|'
            r',\s*\d+\s*(?:[-–—]\s*\d+)?\s*(?:to\s+\d+\s*)?years?',
            category
        )[0].strip()
        return category

    # Fallback : n'importe quel "years for <category>"
    m = re.search(
        r'(?i)\d+\s*(?:[-–—]\s*\d+)?\s*years?\s+for\s+(.+?)(?:\.\s|\band\s+\d|,\s*\d|\.\s*$|$)',
        normalized
    )
    if m:
        category = m.group(1).strip().rstrip('.').rstrip(',')
        category = re.split(
            r'(?i);\s|\bwhich\b|\bthat\b|'
            r'\band\s+\d+\s*(?:[-–—]\s*\d+)?\s*years?|'
            r',\s*\d+\s*(?:[-–—]\s*\d+)?\s*(?:to\s+\d+\s*)?years?',
            category
        )[0].strip()
        return category

    # Pattern : "<category> ... X years" (tables PPE, ex: "Computer hardware ... 3-7 years")
    m = re.search(
        r'(?i)((?:equipment|software|computer|server|hardware|'
        r'data center|computing|network)[A-Za-z,\s&]*?)\s*'
        r'(?:[-–—.…\s]{2,}|:)\s*'
        r'\d+\s*(?:[-–—]\s*\d+)?\s*years?',
        normalized
    )
    if m:
        return m.group(1).strip()

    # Pattern : "equipment, X to Y years" (AMD FY2013 semicolon-separated)
    # Ex: "equipment, two to six years; buildings..."
    m = re.search(
        r'(?i)((?:equipment|software|computer|server|hardware|'
        r'data center|computing|network)[A-Za-z,\s&]*?)'
        r',?\s*\d+\s*(?:[-–—]\s*(?:to\s+)?\d+)?\s*years?',
        normalized
    )
    if m:
        return m.group(1).strip()

    # v5 : Fallback élargi — chercher des noun phrases IT-related dans le texte
    # Cas DXYN : "property, plant and equipment have been computed for ...
    #            useful lives of the related assets, ranging from..."
    # Extraire les noms d'actifs concrets mentionnés autour de la durée.
    it_noun_phrases = re.findall(
        r'(?i)\b('
        r'computer (?:equipment|hardware|software|systems?)|'
        r'(?:office\s+)?equipment(?:\s+and\s+furniture)?|'
        r'machinery(?:\s+and\s+equipment)?|'
        r'furniture(?:\s+and\s+fixtures)?|'
        r'capitalized software|'
        r'(?:information\s+)?technology(?:\s+equipment)?|'
        r'data center (?:equipment|hardware|infrastructure)|'
        r'servers?(?:\s+and\s+(?:storage|networking))?|'
        r'(?:network|networking)\s+equipment|'
        r'leasehold improvements|'
        r'building(?:s)?(?:\s+and\s+improvements)?'
        r')\b',
        normalized
    )

    if it_noun_phrases:
        # Garder uniquement les phrases IT (exclure buildings, leasehold purs)
        it_only = [
            p for p in it_noun_phrases
            if not re.match(r'(?i)^(?:building|leasehold)', p.strip())
        ]
        if it_only:
            # Déduplication en gardant l'ordre
            seen = set()
            unique = []
            for p in it_only:
                key = p.lower().strip()
                if key not in seen:
                    seen.add(key)
                    unique.append(p.strip())
            return ", ".join(unique[:4])  # Max 4 catégories

    return "Not disclosed"


def trim_verbatim(full_text: str) -> str:
    """
    v4 : isole la/les phrase(s) pertinentes du paragraphe entier.

    Au lieu de retourner le <p> complet (qui peut inclure des phrases
    sur les leasehold improvements, les investissements, etc.), on
    extrait seulement les phrases contenant les mots-clés de durée
    de vie + scope IT.
    """
    # Découper en phrases (approximatif mais suffisant)
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', full_text)

    relevant = []
    for sent in sentences:
        has_duration = bool(DURATION_PATTERN.search(sent))
        has_ul_kw = _has_useful_life_kw(sent)
        has_it = _has_it_scope(sent) or any(
            kw in sent.lower() for kw in ["equipment", "software"]
        )

        if (has_duration or has_ul_kw) and has_it:
            relevant.append(sent.strip())

    if relevant:
        return " ".join(relevant)

    # Fallback : retourner le texte complet si rien de plus ciblé
    return full_text


def detect_ambiguity(candidates: list[dict]) -> tuple[bool, str]:
    """
    v4 : ne compte comme ambiguës que les durées issues de segments
    IT-scope distincts (pas des conteneurs div englobants).
    """
    all_durations = set()
    seen_duration_texts = set()

    for c in candidates[:5]:
        text = c["text"]

        # Ignorer les conteneurs div englobants (commencent par header
        # du document — signe d'un div trop large)
        if text.startswith("Table of Contents"):
            continue

        # Vérifier scope IT
        if not _has_it_scope(text):
            text_lower = text.lower()
            if not any(kw in text_lower for kw in ["equipment", "software"]):
                continue

        # Déduplication par contenu de durée (éviter les div parent/enfant)
        dur_key = text[:100]
        if dur_key in seen_duration_texts:
            continue
        seen_duration_texts.add(dur_key)

        for d in extract_duration_values(text):
            val = (d["min"], d.get("max"))
            max_val = val[1] if val[1] is not None else val[0]
            if max_val is not None and max_val <= 15:
                all_durations.add(val)

    if len(all_durations) > 1:
        detail = " | ".join(
            f"{d[0]}-{d[1]} ans" if d[1] else f"{d[0]} ans"
            for d in sorted(all_durations)
        )
        return True, detail

    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 6 : DÉTECTION DE CHANGEMENT DE POLITIQUE (TASK 3)
# ─────────────────────────────────────────────────────────────────────────────

def detect_policy_change(segments: list[dict]) -> tuple[str, str]:
    """
    v4 : cherche dans tous les segments d'Item 8 si un changement
    de politique d'amortissement est explicitement mentionné.

    Retourne (status, verbatim) :
    - ("Yes", texte)        si changement trouvé
    - ("No", "")            si aucune mention
    - ("Unclear", texte)    si mention ambiguë
    """
    for seg in segments:
        text = seg["text"]
        for pat in POLICY_CHANGE_PATTERNS:
            if re.search(pat, text):
                # Vérifier que ça concerne des actifs IT (pas des baux, etc.)
                text_lower = text.lower()
                it_related = _has_it_scope(text) or any(
                    kw in text_lower for kw in
                    ["equipment", "software", "depreciat", "computing",
                     "server", "hardware", "technology"]
                )
                if it_related:
                    return "Yes", trim_verbatim(text)
                # Si pas clairement IT mais mentionne "useful life" → Unclear
                if _has_useful_life_kw(text):
                    return "Unclear", trim_verbatim(text)

    return "No", ""


# ─────────────────────────────────────────────────────────────────────────────
# v5 : NOUVELLES EXTRACTIONS (Tasks 1, 6, 7)
# ─────────────────────────────────────────────────────────────────────────────

def extract_filing_dates(soup: BeautifulSoup, content: str) -> tuple[str, str]:
    """
    v5 : Task 1 — Date de publication et fin d'exercice.

    Cherche dans :
    1. Balises iXBRL DocumentPeriodEndDate (texte ISO ou "Month DD, YYYY")
    2. Texte de couverture "For the fiscal year ended..."
    3. En-tête EDGAR "FILED AS OF DATE: YYYYMMDD"
    """
    filing_date = ""
    period_end = ""

    # Tentative 1a : DocumentPeriodEndDate (texte natif, format ISO)
    m = re.search(
        r'(?i)DocumentPeriodEndDate[^>]*>(?:<[^>]+>)*(\d{4}-\d{2}-\d{2})',
        content[:500000]
    )
    if m:
        period_end = m.group(1)

    # Tentative 1b : DocumentPeriodEndDate (texte natif, format "Month DD, YYYY")
    if not period_end:
        m = re.search(
            r'(?i)DocumentPeriodEndDate[^>]*>(?:<[^>]+>)*([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})',
            content[:500000]
        )
        if m:
            period_end = m.group(1).strip()

    # Tentative 2 : "For the fiscal year ended..." (US: "December 31, 2024")
    if not period_end:
        # Normaliser &nbsp; et &#160; en espaces pour le matching
        normalized = re.sub(r'&nbsp;|&#160;|\xa0', ' ', content[:200000])
        m = re.search(
            r'(?i)for the fiscal year ended[\s,]+(\w+\s+\d{1,2},?\s+\d{4})',
            normalized
        )
        if m:
            period_end = m.group(1).strip()

    # Tentative 3 : format britannique "30 September 2020"
    if not period_end:
        normalized = re.sub(r'&nbsp;|&#160;|\xa0', ' ', content[:200000])
        m = re.search(
            r'(?i)fiscal year ended[\s,]+(\d{1,2}\s+\w+\s+\d{4})',
            normalized
        )
        if m:
            period_end = m.group(1).strip()

    # Filing date : EDGAR header "FILED AS OF DATE: YYYYMMDD"
    m = re.search(
        r'(?i)filed as of date[\s:]+(\d{8})',
        content[:50000]
    )
    if m:
        d = m.group(1)
        filing_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    else:
        # Tentative bis : DocumentFilingDate iXBRL
        m = re.search(
            r'(?i)(?:DocumentFiling|FilingDate)[^>]*>(?:<[^>]+>)*(\d{4}-\d{2}-\d{2})',
            content[:500000]
        )
        if m:
            filing_date = m.group(1)

    # Tentative 4 : xbrli:endDate (anciens formats iXBRL comme APD)
    # On prend la date la plus récente (= fin d'exercice fiscal le plus probable)
    if not period_end:
        end_dates = re.findall(
            r'<xbrli:endDate>(\d{4}-\d{2}-\d{2})</xbrli:endDate>',
            content[:500000]
        )
        if end_dates:
            # La date max est le plus probable period_end
            period_end = max(end_dates)

    return filing_date, period_end


def extract_hardware_mentions(segments: list[dict],
                                full_content: Optional[str] = None) -> tuple[str, int]:
    """
    v5 : Task 7 — Détecte les modèles de hardware spécifiques.

    Scanne d'abord les segments (Item 7+8), puis le document complet si fourni
    (les mentions de hardware sont souvent dans Item 1 Business, hors scope
    pour la comptabilité mais valide pour la timeline hardware).

    Retourne (liste séparée par ';', count).
    """
    found = set()

    # 1) Scan des segments structurés
    for seg in segments:
        text = seg["text"]
        for label, pat in HARDWARE_MODELS.items():
            if re.search(pat, text, re.IGNORECASE):
                found.add(label)

    # 2) Scan du document complet pour les mentions Item 1 / partout
    if full_content and len(found) < len(HARDWARE_MODELS):
        for label, pat in HARDWARE_MODELS.items():
            if label in found:
                continue
            if re.search(pat, full_content, re.IGNORECASE):
                found.add(label)

    if not found:
        return "", 0
    return ";".join(sorted(found)), len(found)


def extract_segment_info(segments: list[dict]) -> str:
    """
    v5 : Task 6 — Extrait les mentions de segments cloud/IA.

    Cherche les paragraphes parlant de segments business (Microsoft Cloud,
    AWS, Azure, AI products, etc.).
    """
    matches = []
    seen = set()
    cyber_re = re.compile(r"(?i)cybers(ecurity|attack|breach)|vulnerabilit")

    for seg in segments:
        text = seg["text"]
        if cyber_re.search(text):
            continue

        # Cherche au moins un mot-clé segment
        for pat in SEGMENT_AI_KW:
            if re.search(pat, text, re.IGNORECASE):
                # Déduplication
                key = text[:150]
                if key in seen:
                    break
                seen.add(key)
                matches.append(text)
                break

        if len(matches) >= 3:  # Limite pour ne pas exploser le CSV
            break

    if not matches:
        return "Not disclosed"
    # Concatène les paragraphes avec séparateur clair
    return " ||| ".join(matches[:3])


def merge_verbatim_with_neighbors(best_seg: dict, all_segments: list[dict],
                                   max_extra_chars: int = 500) -> str:
    """
    v5 : Si le verbatim choisi est tronqué (pas de durée numérique),
    cherche dans les 5 segments voisins pour récupérer les nombres
    manquants. Cas DXYN : "ranging from" → ajouter le segment suivant
    qui contient "5 to 40 years".
    """
    base_text = best_seg["text"]

    # Si le texte contient déjà des nombres + "year", c'est bon
    if re.search(r'\d+.*years?', base_text):
        return base_text

    # Sinon, chercher dans les voisins (avant/après dans la liste)
    try:
        idx = all_segments.index(best_seg)
    except ValueError:
        return base_text

    # Regarder les 5 segments suivants pour trouver les chiffres
    extras = []
    chars_added = 0
    for i in range(idx + 1, min(idx + 6, len(all_segments))):
        neighbor = all_segments[i]
        ntext = neighbor["text"]
        if re.search(r'\d+\s*(?:to\s+|[-–—]\s*)?\d*\s*years?', ntext, re.I):
            # Limiter la taille pour éviter d'absorber un gros bloc
            if len(ntext) <= 300:
                extras.append(ntext)
                chars_added += len(ntext)
                if chars_added >= max_extra_chars:
                    break

    if extras:
        return base_text + " " + " ".join(extras)
    return base_text


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 7 : PIPELINE PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def parse_filename_metadata(filename: str) -> dict:
    name = Path(filename).stem
    parts = name.split("_")
    meta = {"firm_cik": "unknown", "firm_ticker": "unknown", "fiscal_year": "unknown"}
    if len(parts) >= 1:
        meta["firm_cik"] = parts[0]
    if len(parts) >= 2:
        meta["firm_ticker"] = parts[1]
    if len(parts) >= 3:
        fy = parts[2]
        meta["fiscal_year"] = fy[2:] if fy.upper().startswith("FY") else fy
    return meta


def process_html(content: str, filename: str) -> ExtractionResult:
    """Pipeline complet pour un contenu HTML 10-K iXBRL."""
    meta = parse_filename_metadata(filename)

    result = ExtractionResult(
        firm_cik=meta["firm_cik"],
        firm_ticker=meta["firm_ticker"],
        fiscal_year=meta["fiscal_year"],
        filename=filename,
    )

    try:
        logging.info(f"[{filename}] Parsing...")

        soup = BeautifulSoup(content, "html.parser")

        # v5 : Task 1 — Date de publication / fin d'exercice
        # (faire AVANT clean_ixbrl pour ne pas perdre les balises dei:)
        filing_date, period_end = extract_filing_dates(soup, content)
        result.filing_date = filing_date
        result.period_end_date = period_end

        soup = clean_ixbrl(soup)

        # ── 1. Isolation Item 8 ──
        item8_soup, method = isolate_item8(soup)
        result.item8_method = method

        if item8_soup is None:
            result.extraction_status = "ITEM8_NOT_FOUND"
            result.error_message = f"Item 8 non trouvé ({method})."
            return result

        # ── 2. Extraction des segments Item 8 ──
        segments_item8 = extract_text_segments(item8_soup, "item_8")
        result.item8_segment_count = len(segments_item8)
        logging.info(f"[{filename}] {len(segments_item8)} segments Item 8.")

        # ── 3. Filtrage et scoring ──
        filtered = filter_segments(segments_item8)
        ul_candidates = filtered["useful_life"]
        ai_candidates = filtered["ai_infra"]

        # ── 4. Task 2 : Durée de vie utile ──
        if ul_candidates:
            best = ul_candidates[0]
            raw_text = best["text"]

            # v5 : si le segment choisi est tronqué (pas de nombre),
            # fusionner avec les segments voisins pour récupérer les durées
            merged_text = merge_verbatim_with_neighbors(best, segments_item8)

            result.useful_life_text = trim_verbatim(merged_text)
            result.source_section = best["section_hint"]
            result.section_title = best.get("section_title", "")
            result.confidence = best["confidence"]

            # Extraction numérique (sur le texte fusionné)
            duration = extract_it_relevant_duration(merged_text)
            if duration:
                result.useful_life_min_years = duration["min"]
                result.useful_life_max_years = duration.get("max")

            # Catégorie d'actif (sur le texte fusionné aussi)
            result.asset_category = extract_asset_category(merged_text)

            # Ambiguïté (v4 : filtrée IT-only)
            ambig, detail = detect_ambiguity(ul_candidates)
            result.ambiguity_flag = ambig
            result.ambiguity_detail = detail

        # ── 5. Task 3 : Changement de politique ──
        pc_status, pc_text = detect_policy_change(segments_item8)
        result.policy_change = pc_status
        result.policy_change_text = pc_text

        # ── 6. Task 5 : Infrastructure IA ──
        # Section 9.1 du manuel : "Extract disclosure text related to
        # AI infrastructure and large-scale computing"
        # On filtre les mentions passagères (ERP, contrôles internes)
        # pour ne garder que les discussions d'infrastructure/investissement.
        ai_infra_context_re = re.compile(
            r"(?i)(capital\s+expenditure|capex|"
            r"(?:invest(?:ment|ing|ed)\s+(?:in\s+)?(?:ai|data center|gpu|computing|infrastructure))|"
            r"(?:(?:ai|computing|cloud)\s+infrastructure)|"
            r"(?:data center\s+(?:expansion|construction|capacity|build|deploy))|"
            r"cluster|server\s+farm|"
            r"(?:(?:expand|build|deploy|procure|purchase)\w*\s+.{0,30}(?:gpu|server|computing|data center))|"
            r"spending\s+on\s+(?:ai|computing|infrastructure)|"
            r"computing\s+(?:capacity|power|resource)|"
            r"training\s+(?:model|run|infrastructure)|"
            r"(?:gpu|accelerator)\s+(?:cluster|fleet|capacity|procurement))"
        )
        cyber_re = re.compile(r"(?i)cybers(ecurity|attack|breach)|vulnerabilit")

        def _is_ai_infra_relevant(text: str) -> bool:
            """Vérifie si la mention IA est dans un contexte d'infrastructure."""
            if cyber_re.search(text):
                return False
            # Accepter si contexte d'investissement/infrastructure
            if ai_infra_context_re.search(text):
                return True
            # Accepter si mention directe de data center ou GPU (toujours pertinent)
            if re.search(r'(?i)\bgpu|\bdata center|\bhigh[- ]performance computing', text):
                return True
            return False

        # D'abord chercher dans Item 8
        if ai_candidates:
            relevant_ai = [c for c in ai_candidates if _is_ai_infra_relevant(c["text"])]
            if relevant_ai:
                result.ai_infra_text = relevant_ai[0]["text"]
                result.ai_infra_source = "Item 8"

        # Si rien dans Item 8, consulter MD&A (Section 2.2 du manuel)
        if result.ai_infra_text == "Not disclosed":
            item7_soup, _ = isolate_item7(soup)
            if item7_soup is not None:
                segments_item7 = extract_text_segments(item7_soup, "item_7")
                filtered_mda = filter_segments(segments_item7)
                ai_mda = filtered_mda["ai_infra"]
                if ai_mda:
                    relevant_mda = [c for c in ai_mda if _is_ai_infra_relevant(c["text"])]
                    if relevant_mda:
                        result.ai_infra_text = relevant_mda[0]["text"]
                        result.ai_infra_source = "MD&A (Item 7)"

        # ── 7. Task 6 : Segment-level (v5) ──
        # Cherche dans Item 7 (MD&A) en priorité, puis Item 8
        item7_soup_for_seg, _ = isolate_item7(soup)
        if item7_soup_for_seg is not None:
            segments_item7_seg = extract_text_segments(item7_soup_for_seg, "item_7")
            seg_text = extract_segment_info(segments_item7_seg)
            if seg_text == "Not disclosed":
                seg_text = extract_segment_info(segments_item8)
            result.segment_ai_text = seg_text
        else:
            result.segment_ai_text = extract_segment_info(segments_item8)

        # ── 8. Task 7 : Hardware timeline (v5) ──
        # Cherche dans tous les segments disponibles (Item 7 + Item 8)
        all_segments_for_hw = list(segments_item8)
        if item7_soup_for_seg is not None:
            try:
                all_segments_for_hw.extend(segments_item7_seg)
            except NameError:
                pass
        hw_mentions, hw_count = extract_hardware_mentions(
            all_segments_for_hw, full_content=content
        )
        result.hardware_mentions = hw_mentions
        result.hardware_count = hw_count

        logging.info(
            f"[{filename}] Terminé. "
            f"Durée: {result.useful_life_min_years}-{result.useful_life_max_years} ans | "
            f"Confiance: {result.confidence}/3 | "
            f"Policy change: {result.policy_change}"
        )

    except Exception as e:
        result.extraction_status = "ERROR"
        result.error_message = str(e)
        logging.error(f"[{filename}] ERREUR : {e}\n{traceback.format_exc()}")

    return result


def process_file(filepath: str) -> ExtractionResult:
    """Wrapper : lit un fichier HTML depuis le disque et lance le pipeline."""
    filename = os.path.basename(filepath)
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    return process_html(content, filename)


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 8 : EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def export_results(results: list[ExtractionResult], output_dir: str,
                   label: str = ""):
    """
    Exporte les résultats en CSV + JSON + verbatims individuels.
    Si label est fourni, les CSV sont suffixés (ex: extraction_results_batch_1.csv).
    """
    os.makedirs(output_dir, exist_ok=True)
    suffix = f"_{label}" if label else ""

    # CSV
    df = pd.DataFrame([asdict(r) for r in results])
    main_csv = os.path.join(output_dir, f"extraction_results{suffix}.csv")
    df.to_csv(main_csv, index=False, encoding="utf-8")
    print(f"✅ Résultats : {main_csv} ({len(results)} firme(s))")

    # JSON
    json_path = os.path.join(output_dir, f"extraction_results{suffix}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, indent=2, ensure_ascii=False)

    # Verbatims individuels (Section 4.1 du manuel)
    verbatim_dir = os.path.join(output_dir, "verbatims")
    os.makedirs(verbatim_dir, exist_ok=True)
    for r in results:
        stem = Path(r.filename).stem  # ex: 0000001750_AIR_FY2025_10K
        # Simplifier le nom : CIK_TICKER_FY → un fichier par firme-année
        vpath = os.path.join(verbatim_dir, f"{stem}.txt")
        with open(vpath, "w", encoding="utf-8") as f:
            f.write(f"FIRM         : {r.firm_ticker} (CIK {r.firm_cik}) | FY{r.fiscal_year}\n")
            f.write(f"FILE         : {r.filename}\n")
            f.write(f"FILING DATE  : {r.filing_date or 'N/A'}\n")
            f.write(f"PERIOD END   : {r.period_end_date or 'N/A'}\n")
            f.write(f"STATUS       : {r.extraction_status} | Confiance : {r.confidence}/3\n")
            f.write(f"SECTION      : {r.source_section}\n")
            f.write(f"SECTION TITLE: {r.section_title}\n")
            f.write(f"ASSET CAT.   : {r.asset_category}\n")
            f.write(f"DURÉE        : {r.useful_life_min_years}-{r.useful_life_max_years} ans\n")
            f.write(f"POLICY CHANGE: {r.policy_change}\n")
            f.write(f"HARDWARE     : {r.hardware_mentions or 'None'} ({r.hardware_count} modèle(s))\n")
            if r.ambiguity_flag:
                f.write(f"⚠ AMBIGUÏTÉ  : {r.ambiguity_detail}\n")
            if r.error_message:
                f.write(f"⚠ ERREUR     : {r.error_message}\n")
            f.write(f"\n[VERBATIM — Durée de vie]\n{r.useful_life_text}\n")
            if r.policy_change_text:
                f.write(f"\n[VERBATIM — Changement de politique]\n{r.policy_change_text}\n")
            f.write(f"\n[VERBATIM — Infrastructure IA]\n{r.ai_infra_text}")
            if r.ai_infra_source:
                f.write(f"  [Source: {r.ai_infra_source}]")
            f.write(f"\n\n[VERBATIM — Segments (Task 6)]\n{r.segment_ai_text}\n")

    # Replication log (Section 13 du manuel)
    log_mask = (
        (df["extraction_status"] != "OK") |
        (df["ambiguity_flag"] == True) |
        (df["useful_life_text"] == "Not disclosed") |
        (df["confidence"] <= 1) |
        (df["policy_change"] == "Unclear")
    )
    df_log = df[log_mask].copy()
    df_log["review_priority"] = df_log.apply(
        lambda r: "HIGH" if r["extraction_status"] != "OK"
        else "HIGH" if r["useful_life_text"] == "Not disclosed"
        else "MEDIUM" if r["ambiguity_flag"] or r["policy_change"] == "Unclear"
        else "LOW",
        axis=1,
    )
    log_csv = os.path.join(output_dir, f"replication_log{suffix}.csv")
    df_log.to_csv(log_csv, index=False, encoding="utf-8")
    print(f"📋 Replication log : {log_csv} ({len(df_log)} entrée(s))")
    print(f"📄 Verbatims : {verbatim_dir}/ ({len(results)} fichier(s))")


# ─────────────────────────────────────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extracteur 10-K iXBRL v5 — Durées de vie IT & Infrastructure IA"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--input", type=str,
                       help="Chemin vers un fichier HTML unique")
    group.add_argument("--folder", type=str,
                       help="Dossier local contenant des fichiers HTML")
    group.add_argument("--drive", type=str,
                       help="Chemin rclone distant (ex: gdrive:AI_Infrastructure_Investment_Project/raw_filings)")
    parser.add_argument("--batch", type=str, default=None,
                       help="Traiter un seul batch (ex: batch_1.zip)")
    parser.add_argument("--temp-dir", type=str, default=None,
                       help="Dossier temporaire pour les ZIP téléchargés (défaut: auto)")
    parser.add_argument(
        "--output", type=str, default="./extraction",
        help="Dossier de sortie (défaut : ./extraction)"
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # ── Mode 1 : Fichier unique ──
    if args.input:
        with open(args.input, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        result = process_html(content, os.path.basename(args.input))
        _print_result(result)
        export_results([result], args.output)

    # ── Mode 2 : Dossier local (HTML en vrac) ──
    elif args.folder:
        folder = Path(args.folder)
        files = sorted(
            [p for p in folder.glob("*.html")]
            + [p for p in folder.glob("*.htm")]
        )
        if not files:
            print("Aucun fichier HTML trouvé.")
            return

        print(f"\n🔍 {len(files)} fichier(s) HTML...\n")
        results = []
        for filepath in files:
            result = process_file(str(filepath))
            _print_result(result)
            results.append(result)

        export_results(results, args.output)

    # ── Mode 3 : Google Drive via rclone copy (Download-Process-Clean) ──
    elif args.drive:
        remote_path = args.drive  # ex: "gdrive:AI_.../raw_filings"

        # Vérifier que rclone est installé
        if shutil.which("rclone") is None:
            print("❌ rclone n'est pas installé. Installe-le avec : sudo apt install rclone")
            return

        # Lister les ZIP disponibles sur le Drive
        print(f"📡 Listing des batches sur {remote_path}...")
        try:
            ls_result = subprocess.run(
                ["rclone", "lsf", remote_path, "--include", "batch_*.zip"],
                capture_output=True, text=True, timeout=60,
            )
            if ls_result.returncode != 0:
                print(f"❌ Erreur rclone lsf : {ls_result.stderr.strip()}")
                print("   Vérifie ta config rclone : rclone config")
                return
            all_zips = sorted([
                name.strip() for name in ls_result.stdout.strip().split("\n")
                if name.strip().endswith(".zip")
            ])
        except subprocess.TimeoutExpired:
            print("❌ Timeout lors du listing. Vérifie ta connexion.")
            return

        if not all_zips:
            print(f"❌ Aucun batch_*.zip trouvé sur {remote_path}")
            return

        # Filtrer par --batch si spécifié
        if args.batch:
            batch_filter = args.batch
            if not batch_filter.endswith(".zip"):
                batch_filter += ".zip"
            all_zips = [z for z in all_zips if z == batch_filter]
            if not all_zips:
                print(f"❌ Batch introuvable : {args.batch}")
                return

        # Checkpoint : quels batches sont déjà traités ?
        os.makedirs(args.output, exist_ok=True)
        done_batches = set()
        for f in Path(args.output).glob("extraction_results_batch_*.csv"):
            # extraction_results_batch_12.csv → batch_12
            batch_label = f.stem.replace("extraction_results_", "")
            done_batches.add(batch_label + ".zip")

        remaining = [z for z in all_zips if z not in done_batches]
        skipped = len(all_zips) - len(remaining)

        print(f"  Total : {len(all_zips)} batch(es)")
        if skipped:
            print(f"  ⏭ Déjà traités (checkpoint) : {skipped}")
        print(f"  → À traiter : {len(remaining)}\n")

        if not remaining:
            print("✅ Tous les batches sont déjà traités !")
            # Fusionner les CSV existants quand même
            _merge_batch_csvs(args.output)
            return

        # Dossier temporaire
        temp_base = Path(args.temp_dir) if args.temp_dir else Path(tempfile.mkdtemp(prefix="10k_"))
        temp_base.mkdir(parents=True, exist_ok=True)

        all_results = []
        try:
            for i, zip_name in enumerate(remaining, 1):
                batch_name = Path(zip_name).stem  # batch_12
                print(f"{'='*60}")
                print(f"📦 [{i}/{len(remaining)}] {batch_name}")
                print(f"{'='*60}")

                local_zip = temp_base / zip_name

                # ── 1. Télécharger le ZIP ──
                remote_file = f"{remote_path}/{zip_name}"
                print(f"  ⬇ Téléchargement de {zip_name}...")
                try:
                    dl_result = subprocess.run(
                        ["rclone", "copy", remote_file, str(temp_base),
                         "--progress", "--retries", "3", "--low-level-retries", "10"],
                        capture_output=True, text=True, timeout=600,
                    )
                    if dl_result.returncode != 0 or not local_zip.exists():
                        print(f"  ❌ Échec du téléchargement : {dl_result.stderr.strip()}")
                        continue
                    size_mb = local_zip.stat().st_size / (1024 * 1024)
                    print(f"  ✅ Téléchargé ({size_mb:.1f} Mo)")
                except subprocess.TimeoutExpired:
                    print(f"  ❌ Timeout (>10 min). Skip.")
                    continue

                # ── 2. Ouvrir le ZIP local et traiter ──
                batch_results = []
                try:
                    with zipfile.ZipFile(str(local_zip), "r") as zf:
                        html_files = sorted([
                            n for n in zf.namelist()
                            if n.lower().endswith((".html", ".htm"))
                            and not n.startswith("__MACOSX")
                        ])
                        print(f"  📄 {len(html_files)} fichier(s) HTML\n")

                        for html_name in html_files:
                            filename = os.path.basename(html_name)
                            try:
                                content = zf.read(html_name).decode(
                                    "utf-8", errors="ignore"
                                )
                                result = process_html(content, filename)
                                _print_result(result, indent="    ")
                                batch_results.append(result)
                            except Exception as e:
                                logging.error(f"    ❌ {filename}: {e}")
                                meta = parse_filename_metadata(filename)
                                batch_results.append(ExtractionResult(
                                    firm_cik=meta["firm_cik"],
                                    firm_ticker=meta["firm_ticker"],
                                    fiscal_year=meta["fiscal_year"],
                                    filename=filename,
                                    extraction_status="ERROR",
                                    error_message=str(e),
                                ))

                except zipfile.BadZipFile:
                    print(f"  ❌ ZIP corrompu après téléchargement : {zip_name}")
                    if local_zip.exists():
                        local_zip.unlink()
                    continue

                # ── 3. Sauver le CSV batch (= checkpoint) ──
                if batch_results:
                    export_results(batch_results, args.output, label=batch_name)
                    all_results.extend(batch_results)

                # ── 4. Nettoyage du ZIP local ──
                if local_zip.exists():
                    local_zip.unlink()
                    logging.info(f"  🗑 ZIP local supprimé")

                print()

        finally:
            # Nettoyage du dossier temp (même si crash)
            if not args.temp_dir and temp_base.exists():
                shutil.rmtree(temp_base, ignore_errors=True)

        # Fusion globale
        _merge_batch_csvs(args.output)

    # ── Mode par défaut ──
    else:
        parser.print_help()
        return

    print("\n✅ Extraction terminée.")


def _merge_batch_csvs(output_dir: str):
    """Fusionne tous les extraction_results_batch_*.csv en un CSV global."""
    batch_csvs = sorted(Path(output_dir).glob("extraction_results_batch_*.csv"))
    if len(batch_csvs) <= 1:
        return
    print(f"\n{'='*60}")
    print(f"📊 FUSION GLOBALE : {len(batch_csvs)} batch(es)")
    print(f"{'='*60}")
    dfs = [pd.read_csv(p) for p in batch_csvs]
    df_all = pd.concat(dfs, ignore_index=True)
    global_csv = os.path.join(output_dir, "extraction_results_global.csv")
    df_all.to_csv(global_csv, index=False, encoding="utf-8")
    print(f"✅ {global_csv} ({len(df_all)} firme-année(s))")


def _print_result(r: ExtractionResult, indent: str = "  "):
    """Affiche un résumé compact d'un résultat."""
    icon = "✅" if r.extraction_status == "OK" and r.confidence > 0 else "❌"
    print(f"{indent}→ {r.filename}")
    print(f"{indent}  {icon} Durée : {r.useful_life_min_years}-{r.useful_life_max_years} ans | "
          f"Section : {r.source_section} | Confiance : {r.confidence}/3")
    if r.period_end_date or r.filing_date:
        print(f"{indent}  Date : period_end={r.period_end_date or 'N/A'}, filing={r.filing_date or 'N/A'}")
    print(f"{indent}  Asset category : {r.asset_category}")
    print(f"{indent}  Policy change  : {r.policy_change}")
    ai_status = f"Found ({r.ai_infra_source})" if r.ai_infra_text != "Not disclosed" else "Not disclosed"
    print(f"{indent}  AI infra       : {ai_status}")
    if r.hardware_count > 0:
        print(f"{indent}  Hardware       : {r.hardware_mentions} ({r.hardware_count} modèle(s))")
    if r.segment_ai_text != "Not disclosed":
        print(f"{indent}  Segments       : ✓ ({len(r.segment_ai_text)} chars)")
    if r.ambiguity_flag:
        print(f"{indent}  ⚠  Ambiguïté   : {r.ambiguity_detail}")
    if r.extraction_status != "OK":
        print(f"{indent}  💬 {r.error_message}")


if __name__ == "__main__":
    main()