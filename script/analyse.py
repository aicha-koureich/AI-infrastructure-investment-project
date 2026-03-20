import os
import re
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
chemin_html = "/home/jean-marie/cassiopé/0000001750_AIR_FY2025_10K.html"
fichier_sortie = "/home/jean-marie/cassiopé/extraction/extraction_aar.txt"

def extraire_donnees_aar(chemin_html, fichier_sortie):
    if not os.path.exists(chemin_html):
        print("Fichier introuvable.")
        return

    # Utilisation de errors='ignore' pour éviter l'erreur Unicode 0x92
    with open(chemin_html, 'r', encoding='utf-8', errors='ignore') as f:
        soup = BeautifulSoup(f, 'html.parser')

    # --- ÉTAPE 1 : Cibler la zone des PPE (Note 1 ou Note 5) ---
    # Pour AAR Corp, les durées de vie sont souvent dans "Significant Accounting Policies"
    # On récupère tout le texte pour isoler la section
    full_text = soup.get_text(" ", strip=True)
    
    # Task 3 & 5 : Focus strict sur le matériel informatique et l'IA
    asset_scope = [
        "server", "computing equipment", "computer hardware", 
        "data center", "gpu", "high-performance computing", 
        "artificial intelligence", "machine learning", "cloud infrastructure"
    ]
    
    # On garde "years" et "useful lives" pour la Task 3
    keywords = ["useful lives", "estimated life", "years", "depreciated", "change"]

    # --- ÉTAPE 2 : Recherche intelligente (Version Handbook) ---
    segments = soup.find_all(['p', 'td', 'div', 'ix:nonnumeric'])
    resultats = []
    
    # Exclusions strictes selon section 2.3 du manuel
    exclusions = ["building", "vehicle", "office equipment", "furniture", "fixtures", "aircraft"]

    print(f"Analyse de {len(segments)} segments...")

    for s in segments:
        texte_complet = s.get_text(" ", strip=True)
        # On travaille par paragraphe complet pour la Task 5 (9.3 Output)
        p_low = texte_complet.lower()
        
        # FILTRE 1 : Est-ce que ça parle de notre Scope (Computing/AI) ?
        if any(a in p_low for a in asset_scope):
            
            # FILTRE 2 : Exclusion des actifs hors-sujet (Section 2.3)
            # On n'exclut que si la phrase ne contient PAS de mot "computing" 
            # (car une phrase peut lister "buildings AND servers")
            if any(ex in p_low for ex in exclusions) and not any(inc in p_low for inc in ["server", "hardware", "computing"]):
                continue

            # FILTRE 3 : On cherche soit une durée de vie (Task 3) soit une mention IA (Task 5)
            has_useful_life = any(k in p_low for k in keywords)
            has_ai_mention = any(ai in p_low for ai in ["ai", "intelligence", "gpu", "machine learning"])

            if has_useful_life or has_ai_mention:
                # Nettoyage verbatim
                clean_text = re.sub(r'\s+', ' ', texte_complet).strip()
                
                # Vérification de sécurité (doublons et longueur)
                if 50 < len(clean_text) < 2000:
                    resultats.append(clean_text)

    # --- ÉTAPE 3 : Sauvegarde ---
    os.makedirs(os.path.dirname(fichier_sortie), exist_ok=True)
    with open(fichier_sortie, 'w', encoding='utf-8') as f:
        f.write(f"--- EXTRACTION AAR CORP : {os.path.basename(chemin_html)} ---\n\n")
        
        # Nettoyage des doublons
        uniques = list(dict.fromkeys(resultats))
        
        if not uniques:
            f.write("Aucun bloc trouvé avec les filtres actuels.\n")
        else:
            for i, res in enumerate(uniques):
                f.write(f"BLOC {i+1} :\n{res}\n")
                f.write("-" * 50 + "\n")

    print(f"Terminé. {len(uniques)} blocs extraits dans {fichier_sortie}")

extraire_donnees_aar(chemin_html, fichier_sortie)