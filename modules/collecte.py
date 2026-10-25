"""
Module 1 : Data Pipeline.

Collecte chaque matin :
  - les cours quotidiens des actions de l'univers (Yahoo Finance)
  - les séries macro : VIX, taux US 10 ans, EUR/USD, indices (Yahoo),
    taux Fed et inflation US (FRED), taux BCE et inflation zone euro (BCE)
  - un instantané des ratios fondamentaux de chaque action (Yahoo)

Tout est enregistré dans la base SQLite locale. Une erreur sur une action
ou une série n'arrête pas la collecte : elle est notée dans le journal.
"""
import io
import logging
import time
from datetime import date, timedelta

import pandas as pd
import requests

import config
from modules import db

log = logging.getLogger("algo")

ENTETES_HTTP = {"User-Agent": "Mozilla/5.0 (Macintosh) algo-recommandation/1.0"}


def _avec_reessais(fonction, *args, essais: int = 3, pause: float = 2.0, **kwargs):
    """Relance une fonction en cas d'erreur réseau passagère."""
    derniere_erreur = None
    for i in range(essais):
        try:
            return fonction(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - on veut tout rattraper ici
            derniere_erreur = e
            time.sleep(pause * (i + 1))
    raise derniere_erreur


def _historique_yahoo(symbole: str, debut: str) -> pd.DataFrame:
    import yfinance as yf  # import ici : le reste du code n'en dépend pas

    df = yf.Ticker(symbole).history(start=debut, auto_adjust=True, actions=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    return df[[c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]]


# ------------------------------- prix ---------------------------------------
def collecter_prix(conn, tickers=None) -> dict:
    """Télécharge les nouveaux cours (incrémental : seulement ce qui manque)."""
    tickers = list(tickers or config.UNIVERS.keys())
    bilan = {"ok": 0, "erreurs": []}
    for i, t in enumerate(tickers, 1):
        derniere = db.derniere_date_prix(conn, t)
        if derniere:
            # on recharge quelques jours en arrière pour capter les corrections
            debut = (pd.Timestamp(derniere) - timedelta(days=7)).strftime("%Y-%m-%d")
        else:
            debut = config.DATE_DEBUT_HISTORIQUE
        try:
            df = _avec_reessais(_historique_yahoo, t, debut)
            n = db.upsert_prix(conn, t, df)
            bilan["ok"] += 1
            log.info(f"  [{i}/{len(tickers)}] {t:<10} {n:>5} jours enregistrés (depuis {debut})")
        except Exception as e:  # noqa: BLE001
            bilan["erreurs"].append(f"{t}: {e}")
            log.warning(f"  [{i}/{len(tickers)}] {t:<10} ERREUR : {e}")
        time.sleep(0.3)  # politesse envers le serveur
    db.journaliser(conn, "collecte_prix", "ok" if not bilan["erreurs"] else "partiel",
                   f"{bilan['ok']} actions, {len(bilan['erreurs'])} erreurs")
    return bilan


# ------------------------------- macro --------------------------------------
def _serie_fred(code: str) -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={code}&cosd={config.DATE_DEBUT_HISTORIQUE}"
    r = requests.get(url, headers=ENTETES_HTTP, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    # 1re colonne = date, 2e colonne = valeur ("." = valeur manquante)
    s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").to_numpy(),
                  index=pd.to_datetime(df.iloc[:, 0]))
    return s.dropna()


def _serie_bce(cle: str) -> pd.Series:
    url = (f"https://data-api.ecb.europa.eu/service/data/{cle}"
           f"?format=csvdata&startPeriod={config.DATE_DEBUT_HISTORIQUE[:7]}")
    r = requests.get(url, headers=ENTETES_HTTP, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    s = pd.Series(pd.to_numeric(df["OBS_VALUE"], errors="coerce").to_numpy(),
                  index=pd.to_datetime(df["TIME_PERIOD"]))
    return s.dropna().sort_index()


def collecter_macro(conn) -> dict:
    bilan = {"ok": 0, "erreurs": []}

    for nom, symbole in config.MACRO_YAHOO.items():
        try:
            derniere = conn.execute("SELECT MAX(date) FROM macro WHERE serie = ?", (nom,)).fetchone()[0]
            debut = ((pd.Timestamp(derniere) - timedelta(days=7)).strftime("%Y-%m-%d")
                     if derniere else config.DATE_DEBUT_HISTORIQUE)
            df = _avec_reessais(_historique_yahoo, symbole, debut)
            n = db.upsert_macro(conn, nom, "yahoo", df["Close"]) if not df.empty else 0
            bilan["ok"] += 1
            log.info(f"  {nom:<10} {n:>5} valeurs (Yahoo {symbole})")
        except Exception as e:  # noqa: BLE001
            bilan["erreurs"].append(f"{nom}: {e}")
            log.warning(f"  {nom:<10} ERREUR : {e}")

    for source, series, lecteur in (("fred", config.MACRO_FRED, _serie_fred),
                                    ("bce", config.MACRO_BCE, _serie_bce)):
        for nom, code in series.items():
            try:
                s = _avec_reessais(lecteur, code)
                n = db.upsert_macro(conn, nom, source, s)
                bilan["ok"] += 1
                log.info(f"  {nom:<10} {n:>5} valeurs ({source.upper()})")
            except Exception as e:  # noqa: BLE001
                bilan["erreurs"].append(f"{nom}: {e}")
                log.warning(f"  {nom:<10} ERREUR : {e}")

    db.journaliser(conn, "collecte_macro", "ok" if not bilan["erreurs"] else "partiel",
                   f"{bilan['ok']} séries, {len(bilan['erreurs'])} erreurs")
    return bilan


# ---------------------------- fondamentaux ----------------------------------
def _infos_yahoo(symbole: str) -> dict:
    import yfinance as yf

    infos = yf.Ticker(symbole).info or {}
    if not infos or len(infos) < 5:
        raise ValueError("aucune donnée fondamentale renvoyée")
    return infos


def collecter_fondamentaux(conn, tickers=None) -> dict:
    """Enregistre un instantané des ratios du jour (1 relevé par action et par jour).

    Ces relevés quotidiens constituent, au fil des mois, un historique
    "point-in-time" honnête : c'est lui qui permettra plus tard de vérifier
    si le score fondamental a vraiment une valeur prédictive.
    """
    tickers = list(tickers or config.UNIVERS.keys())
    aujourd_hui = date.today().isoformat()
    bilan = {"ok": 0, "erreurs": []}
    for i, t in enumerate(tickers, 1):
        try:
            infos = _avec_reessais(_infos_yahoo, t)
            db.enregistrer_fondamentaux(conn, t, aujourd_hui, infos)
            bilan["ok"] += 1
            log.info(f"  [{i}/{len(tickers)}] {t:<10} ratios enregistrés")
        except Exception as e:  # noqa: BLE001
            bilan["erreurs"].append(f"{t}: {e}")
            log.warning(f"  [{i}/{len(tickers)}] {t:<10} ERREUR : {e}")
        time.sleep(0.5)
    db.journaliser(conn, "collecte_fondamentaux", "ok" if not bilan["erreurs"] else "partiel",
                   f"{bilan['ok']} actions, {len(bilan['erreurs'])} erreurs")
    return bilan
