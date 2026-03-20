import fitz  # PyMuPDF
import re

chemin_pdf = "/home/jean-marie/cassiopé/0000001750_AIR_FY2025_10K.pdf"

fichier_sortie = "/home/jean-marie/cassiopé/extraction/extraction.txt"

def trouver_page_sommaire(doc):
    """Cherche 'Table of Contents' dans les 5 premières pages."""
    for i in range(min(5, len(doc))):
        texte = doc[i].get_text()
        if "Table of Contents" in texte:
            print(f"Sommaire détecté à la page {i + 1}")
            return doc[i]
    return None

def extraire_donnees_ia_strict(chemin_pdf, fichier_sortie):
    doc = fitz.open(chemin_pdf)
    
    # --- ETAPE 1 : Trouver les bornes dans la Table des Matières ---
    page_sommaire = trouver_page_sommaire(doc)
    
    # On cherche "Financial Statements" et le chapitre suivant pour borner
    start_page = 0
    end_page = len(doc)
    
    if page_sommaire:
        texte_sommaire = page_sommaire.get_text()

    # Recherche du numéro de page pour Financial Statements (Item 8)
    match_start = re.search(r"Financial Statements and Supplementary Data.*?(\d+)", texte_sommaire, re.DOTALL)
    if match_start:
        start_page = int(match_start.group(1))
    
    # Recherche du chapitre suivant pour définir la fin (Item 9)
    match_end = re.search(r"Changes in and Disagreements with Accountants.*?(\d+)", texte_sommaire, re.DOTALL)
    if match_end:
        end_page = int(match_end.group(1)) - 1

    print(f"Zone d'analyse identifiée : Pages {start_page} à {end_page}")

    # --- ETAPE 2 : Définition des filtres ---
    sections_cibles = ["Significant Accounting Policies", "Property, Plant and Equipment", "PPE"]
    asset_scope = ["server", "computing equipment", "computer hardware", "data center"]
    
    resultats = []

    # --- ETAPE 3 : Analyse restreinte ---
    # On itère uniquement sur la plage de pages identifiée
    for page_num in range(start_page - 1, end_page): # -1 car l'index commence à 0
        page = doc[page_num]
        blocks = page.get_text("blocks")
        
        # On vérifie si on est dans une sous-section pertinente sur cette page
        texte_page_complet = page.get_text().lower()
        est_dans_bonne_section = any(sec.lower() in texte_page_complet for sec in sections_cibles)
        
        if est_dans_bonne_section:
            for block in blocks:
                bloc_texte = block[4]
                
                # Vérification du Asset Scope (mots-clés précis du manuel)
                if any(kw in bloc_texte.lower() for kw in asset_scope):
                    resultats.append({
                        "page": page_num + 1,
                        "texte": bloc_texte.strip().replace('\n', ' ')
                    })

    # --- ETAPE 4 : Écriture ---
    with open(fichier_sortie, 'w', encoding='utf-8') as f:
        f.write(f"--- EXTRACTION CIBLÉE : {chemin_pdf} ---\n")
        f.write(f"ZONE ANALYSÉE : PAGES {start_page} À {end_page}\n\n")
        
        if not resultats:
            f.write("Not disclosed\n")
        else:
            for res in resultats:
                f.write(f"PAGE: {res['page']}\n")
                f.write(f"TEXTE VERBATIM: {res['texte']}\n")
                f.write("-" * 30 + "\n")

    print(f"Analyse terminée. {len(resultats)} blocs trouvés.")

# Lancement
extraire_donnees_ia_strict(chemin_pdf, fichier_sortie)