# tinySA -> WWB

Application Mac (Python / Tkinter) qui pilote un tinySA Ultra+ ZS-407 en USB, balaie une plage
en tranches de 11,25 MHz (450 points, pas de 25 kHz), garde les scans par session et exporte
un CSV lisible par Shure Wireless Workbench.

## Fabriquer l'application Mac (puce M1)

1. Onglet **Actions** du dépôt, puis **Fabriquer l'application Mac (M1)**.
2. Bouton **Run workflow**, puis attendre quelques minutes.
3. Ouvrir l'exécution terminée, section **Artifacts**, télécharger `tinySA-WWB-mac-M1`.
4. Décompresser, puis glisser `tinySA WWB.app` dans le dossier Applications.

### Première ouverture

L'application n'est pas signée par Apple. Si macOS la refuse : clic droit sur l'application,
**Ouvrir**, puis **Ouvrir** à nouveau. Sinon, dans le Terminal :

    xattr -dr com.apple.quarantine "/Applications/tinySA WWB.app"

## Lancer sans fabriquer l'application

    pip3 install pyserial matplotlib
    python3 tinysa_wwb.py

Case « Mode démo » pour essayer sans appareil.
