🛠 Script d'Extraction de Données (v4)

Ce script est conçu pour automatiser l'extraction de données financières et textuelles à partir des rapports annuels SEC Form 10-K (format iXBRL). Il répond spécifiquement aux exigences du AI Infrastructure Data Construction Handbook.
Fonctions principales :

    Tâche 2 (Useful-Life) : Extraction "chirurgicale" des durées de vie des actifs IT (serveurs, software, matériel réseau).

    Tâche 3 (Policy Changes) : Détection automatique des changements de méthodes d'amortissement.

    Tâche 5 (AI Infrastructure) : Extraction des paragraphes entiers mentionnant l'infrastructure IA, les GPU et le Machine Learning.

    Robustesse : Gestion des nombres en lettres, filtrage du "bruit" (ignore les données liées aux acquisitions/goodwill) et gestion des tableaux HTML complexes.

Utilisation :

Le script s'exécute depuis le terminal. Assurez-vous d'avoir installé les dépendances (pandas, beautifulsoup4, lxml).

Pour traiter un seul fichier :
Bash

python script/extraction_aar_v4.py --input raw_filings/nom_du_fichier.html

Pour traiter tout un dossier (recommandé) :
Bash

python script/extraction_aar_v4.py --folder raw_filings/ --output logs_and_tracking/

Fichiers de sortie :

    extraction_results.csv : Données structurées par entreprise/année.

    replication_log.csv : Journal des ambiguïtés et erreurs pour le contrôle qualité.

    extraction_verbatim.txt : Texte intégral extrait pour vérification manuelle.


