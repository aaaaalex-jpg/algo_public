"""
Recherche : peut-on repérer tôt les crises DURABLES (2000-2002, 2008, 2022) sans
sortir du marché lors des paniques passagères (2018, 2020, 2025) ?

Ce script télécharge un historique long (depuis 1990) des indices et de quelques
indicateurs économiques, et l'enregistre dans data/recherche/ (fichiers CSV).
Il ne touche PAS à ta base de données ni à tes portefeuilles.

    python recherche_crises.py
"""
import io
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from modules.collecte import ENTETES_HTTP, _avec_reessais, _historique_yahoo  # noqa: E402

DEBUT = "1990-01-01"
DOSSIER = config.DATA_DIR / "recherche"

YAHOO = {
    "SP500": "^GSPC",        # S&P 500
    "STOXX50": "^STOXX50E",  # Euro Stoxx 50
    "DAX": "^GDAXI",         # DAX (plus long historique européen)
    "CAC40": "^FCHI",        # CAC 40
    "VIX": "^VIX",           # indice de la peur
}
FRED = {
    "FED_FUNDS": "FEDFUNDS",      # taux directeur de la Fed (mensuel)
    "SPREAD_BAA": "BAA10Y",       # écart de taux des entreprises notées BAA vs État US (quotidien)
    "SPREAD_HY": "BAMLH0A0HYM2",  # écart de taux des entreprises fragiles ("high yield")
    "COURBE_10_2": "T10Y2Y",      # taux 10 ans moins 2 ans (négatif = courbe inversée)
    "TAUX_10A": "DGS10",          # taux US à 10 ans
    "CHOMAGE": "UNRATE",          # taux de chômage US (mensuel)
    "INSCRITS_CHOMAGE": "ICSA",   # nouvelles inscriptions au chômage (hebdomadaire)
    "INFLATION": "CPIAUCSL",      # indice des prix US (mensuel)
}


def serie_fred(code: str) -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={code}&cosd={DEBUT}"
    r = requests.get(url, headers=ENTETES_HTTP, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    return pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").to_numpy(),
                     index=pd.to_datetime(df.iloc[:, 0])).dropna()


def main() -> int:
    DOSSIER.mkdir(parents=True, exist_ok=True)
    print(f"Téléchargement de l'historique long (depuis {DEBUT}) dans {DOSSIER}\n")
    erreurs = 0
    for nom, symbole in YAHOO.items():
        try:
            df = _avec_reessais(_historique_yahoo, symbole, DEBUT)
            df.to_csv(DOSSIER / f"{nom}.csv", index_label="date")
            print(f"  ✅ {nom:<17} {len(df):>6} jours, du {df.index[0].date()} au {df.index[-1].date()}")
        except Exception as e:  # noqa: BLE001
            erreurs += 1
            print(f"  ❌ {nom:<17} {e}")
        time.sleep(0.5)
    for nom, code in FRED.items():
        try:
            s = _avec_reessais(serie_fred, code)
            s.rename("valeur").to_csv(DOSSIER / f"{nom}.csv", index_label="date")
            print(f"  ✅ {nom:<17} {len(s):>6} valeurs, du {s.index[0].date()} au {s.index[-1].date()}")
        except Exception as e:  # noqa: BLE001
            erreurs += 1
            print(f"  ❌ {nom:<17} {e}")
    print(f"\n{'✅ Terminé' if not erreurs else f'⚠️  Terminé avec {erreurs} erreur(s)'} — "
          f"tu peux prévenir Claude que c'est fini.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
