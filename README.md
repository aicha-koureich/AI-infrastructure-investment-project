AI Infrastructure Investment Project - SEC 10-K Extractor

Ce dépôt contient les outils d'extraction automatisée pour le projet de recherche sur l'investissement dans les infrastructures IA, basé sur le AI Infrastructure Data Construction Handbook.
###Fonctionnalités

Le script extract_10k_v5.py traite les rapports SEC Form 10-K (iXBRL) à grande échelle :

    Useful-Life (Task 2) : Durées de vie des actifs IT (serveurs, hardware, software).

    Policy Changes (Task 3) : Détection des changements de méthodes d'amortissement.

    AI Infrastructure (Task 5) : Verbatims sur les GPU, le Machine Learning et les Data Centers.

###Installation et Prérequis
1. Dépendances Python
Bash

pip install pandas beautifulsoup4 lxml

2. Configuration Rclone

Le script utilise directement l'outil système rclone pour communiquer avec Google Drive.

    Assurez-vous que rclone est installé et configuré (nom du remote conseillé : gdrive).

    Plus besoin de monter le Drive (rclone mount), le script gère les transferts un par un.

###Utilisation

Le script propose trois modes d'exécution. Le mode Production est optimisé pour éviter de saturer l'espace disque local.
1. Mode Test (Fichier unique)
Bash

python extract_10k_v5.py --input 0000001750_AIR_FY2025_10K.html

2. Mode Local (Dossier HTML)
Bash

python extract_10k_v5.py --folder ./filings/ --output ./results/

3. Mode Production (Auto-Download & Checkpoint)

C'est le mode recommandé pour traiter les 68 batches du S&P 1500. Le script télécharge un ZIP, le traite, puis le supprime avant de passer au suivant.

    Exécution globale :

Bash

python extract_10k_v5.py --drive "gdrive:AI_Infrastructure_Investment_Project/raw_filings" --output ./results/

    Reprise après interruption (Checkpoint) :
    Si le script s'arrête (coupure internet, crash), relancez simplement la même commande. Le script détecte les fichiers extraction_results_batch_X.csv déjà présents et ne traite que les batches manquants.

    Batch spécifique :

Bash

python extract_10k_v5.py --drive "gdrive:..." --batch batch_12.zip --output ./results/

###Structure des sorties

Le dossier ./results/ est organisé pour garantir l'intégrité des données :

    extraction_results_global.csv : Fusion finale de tous les batches.

    extraction_results_batch_X.csv : Données brutes par batch (sert de checkpoint).

    replication_log_global.csv : Journal centralisant les erreurs (Task 13).

    verbatims/ : Un fichier .txt par firme-année (ex: 0000002488_AMD_FY2024.txt) pour vérification manuelle.

###Robustesse et Sécurité

    Gestion de l'espace disque : Chaque fichier ZIP est supprimé immédiatement après traitement.

    Tolérance aux pannes : Utilisation de --retries 3 via rclone pour les téléchargements.

    Mémoire optimisée : Les fichiers HTML sont lus directement depuis l'archive locale sans extraction massive sur le disque.
