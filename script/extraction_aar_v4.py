"""
extraction_aar_v4.py
====================
Extracteur robuste pour rapports 10-K iXBRL (format SEC EDGAR)
Projet : Durées de vie des actifs IT / Infrastructure IA

Corrections et améliorations vs v3 :
  ─────────────────────────────────────────────────────────────────────
  1. SECTION MAL CLASSÉE :
     v3 détectait "Property, Plant and Equipment" comme sous-titre et
     basculait en section "ppe_note", alors que c'est un sous-titre
     DANS Note 1 "Summary of Significant Accounting Policies".
     → v4 : hiérarchie à deux niveaux (note_header > sub_heading).
       Le sub-heading ne remplace la section que s'il est lui-même un
       titre de note numéroté (ex: "Note 4 — Property and Equipment").

  2. TASK 3 MANQUANTE (Policy Changes) :
     v3 n'implémentait pas la détection de changements de politique
     d'amortissement (Section 7 du manuel).
     → v4 : nouveau champ policy_change ("Yes" / "No" / "Not disclosed")
       avec recherche de patterns ciblés dans Item 8.

  3. VERBATIM TROP LARGE :
     v3 prenait le <p> entier qui incluait des phrases non pertinentes
     ("Leasehold improvements are amortized...").
     → v4 : trimming par phrase — on isole la/les phrase(s) contenant
       les mots-clés de durée de vie + scope IT.

  4. AMBIGUÏTÉ MAL QUALIFIÉE :
     v3 flaggait "12.5 ans" qui vient des intangible assets (customer
     relationships), pas des actifs IT.
     → v4 : l'ambiguïté ne compte que les durées extraites de segments
       qui ont eux-mêmes passé le filtre IT-scope.

  5. CHAMPS MANQUANTS (Task 2) :
     → v4 : ajout asset_category, section_title, policy_change.

  6. SCOPE MD&A POUR TASK 5 :
     Le manuel (Section 2.2) autorise la consultation du MD&A comme
     source supplémentaire pour identifier les discussions IA.
     → v4 : si rien trouvé dans Item 8, scan aussi Item 7 (MD&A)
       pour les mots-clés IA uniquement.
  ─────────────────────────────────────────────────────────────────────

Usage :
    python extraction_aar_v4.py --input 0000001750_AIR_FY2025_10K.html
    python extraction_aar_v4.py --folder ./filings/ --output ./results/
"""

import os
import re
import json
import logging
import argparse
import hashlib
import traceback
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
    # Task 2 : durée de vie
    asset_category: str = "Not disclosed"       # v4 : catégorie d'actif
    useful_life_text: str = "Not disclosed"
    useful_life_min_years: Optional[float] = None
    useful_life_max_years: Optional[float] = None
    # Task 3 : changement de politique
    policy_change: str = "Not disclosed"        # v4 : "Yes" / "No" / "Not disclosed"
    policy_change_text: str = ""                 # v4 : verbatim si changement détecté
    # Task 5 : infrastructure IA
    ai_infra_text: str = "Not disclosed"
    ai_infra_source: str = ""                    # v4 : "Item 8" ou "MD&A" ou ""
    # Méta-qualité
    source_section: str = "unknown"
    section_title: str = ""                      # v4 : titre de section verbatim
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
                                             "h1", "h2", "h3", "h4"]))
    container_tags = list(soup_section.find_all(["div", "span"]))

    for tag in leaf_tags + container_tags:
        text = tag.get_text(" ", strip=True)
        text = re.sub(r"\s{2,}", " ", text).strip()

        if not text:
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


def process_file(filepath: str) -> ExtractionResult:
    """Pipeline complet pour un fichier 10-K iXBRL."""
    filename = os.path.basename(filepath)
    meta = parse_filename_metadata(filename)

    result = ExtractionResult(
        firm_cik=meta["firm_cik"],
        firm_ticker=meta["firm_ticker"],
        fiscal_year=meta["fiscal_year"],
        filename=filename,
    )

    try:
        logging.info(f"[{filename}] Chargement...")
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        soup = BeautifulSoup(content, "html.parser")
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
            result.useful_life_text = trim_verbatim(raw_text)
            result.source_section = best["section_hint"]
            result.section_title = best.get("section_title", "")
            result.confidence = best["confidence"]

            # Extraction numérique
            duration = extract_it_relevant_duration(raw_text)
            if duration:
                result.useful_life_min_years = duration["min"]
                result.useful_life_max_years = duration.get("max")

            # Catégorie d'actif
            result.asset_category = extract_asset_category(raw_text)

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


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 8 : EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def export_results(results: list[ExtractionResult], output_dir: str):
    os.makedirs(output_dir, exist_ok=True)

    # CSV
    df = pd.DataFrame([asdict(r) for r in results])
    main_csv = os.path.join(output_dir, "extraction_results.csv")
    df.to_csv(main_csv, index=False, encoding="utf-8")
    print(f"✅ Résultats : {main_csv}")

    # JSON
    json_path = os.path.join(output_dir, "extraction_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, indent=2, ensure_ascii=False)
    print(f"✅ JSON : {json_path}")

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
    log_csv = os.path.join(output_dir, "replication_log.csv")
    df_log.to_csv(log_csv, index=False, encoding="utf-8")
    print(f"📋 Replication log : {log_csv} ({len(df_log)} entrée(s))")

    # Verbatim (Section 4.1 du manuel)
    verbatim_path = os.path.join(output_dir, "extraction_verbatim.txt")
    with open(verbatim_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write("=" * 70 + "\n")
            f.write(f"FIRM         : {r.firm_ticker} (CIK {r.firm_cik}) | FY{r.fiscal_year}\n")
            f.write(f"FILE         : {r.filename}\n")
            f.write(f"STATUS       : {r.extraction_status} | Confiance : {r.confidence}/3\n")
            f.write(f"SECTION      : {r.source_section}\n")
            f.write(f"SECTION TITLE: {r.section_title}\n")
            f.write(f"ASSET CAT.   : {r.asset_category}\n")
            f.write(f"DURÉE        : {r.useful_life_min_years}-{r.useful_life_max_years} ans\n")
            f.write(f"POLICY CHANGE: {r.policy_change}\n")
            if r.ambiguity_flag:
                f.write(f"⚠ AMBIGUÏTÉ  : {r.ambiguity_detail}\n")
            if r.error_message:
                f.write(f"⚠ ERREUR     : {r.error_message}\n")
            f.write("\n[VERBATIM — Durée de vie]\n")
            f.write(r.useful_life_text + "\n")
            if r.policy_change_text:
                f.write("\n[VERBATIM — Changement de politique]\n")
                f.write(r.policy_change_text + "\n")
            f.write("\n[VERBATIM — Infrastructure IA]\n")
            f.write(f"{r.ai_infra_text}")
            if r.ai_infra_source:
                f.write(f"  [Source: {r.ai_infra_source}]")
            f.write("\n\n")
    print(f"📄 Verbatim : {verbatim_path}")


# ─────────────────────────────────────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extracteur 10-K iXBRL v4 — Durées de vie IT & Infrastructure IA"
    )
    parser.add_argument("--input", type=str, help="Chemin vers un fichier HTML unique")
    parser.add_argument("--folder", type=str, help="Dossier contenant plusieurs fichiers HTML")
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

    files_to_process = []
    if args.input:
        files_to_process.append(args.input)
    elif args.folder:
        folder = Path(args.folder)
        files_to_process = sorted(
            [str(p) for p in folder.glob("*.html")]
            + [str(p) for p in folder.glob("*.htm")]
        )
    else:
        default = "/mnt/user-data/uploads/0000001750_AIR_FY2025_10K.html"
        if os.path.exists(default):
            files_to_process.append(default)
        else:
            parser.print_help()
            return

    if not files_to_process:
        print("Aucun fichier HTML trouvé.")
        return

    print(f"\n🔍 Traitement de {len(files_to_process)} fichier(s)...\n")

    results = []
    for filepath in files_to_process:
        print(f"  → {os.path.basename(filepath)}")
        result = process_file(filepath)
        results.append(result)

        status_icon = "✅" if result.extraction_status == "OK" and result.confidence > 0 else "❌"
        print(f"    {status_icon} Durée : {result.useful_life_min_years}-{result.useful_life_max_years} ans | "
              f"Section : {result.source_section} | "
              f"Confiance : {result.confidence}/3 | "
              f"Segments : {result.item8_segment_count}")
        print(f"    Asset category : {result.asset_category}")
        print(f"    Policy change  : {result.policy_change}")
        print(f"    AI infra       : {'Found (' + result.ai_infra_source + ')' if result.ai_infra_text != 'Not disclosed' else 'Not disclosed'}")
        if result.ambiguity_flag:
            print(f"    ⚠  Ambiguïté   : {result.ambiguity_detail}")
        if result.extraction_status != "OK":
            print(f"    💬 {result.error_message}")

    print()
    export_results(results, args.output)
    print("\nExtraction terminée.")


if __name__ == "__main__":
    main()