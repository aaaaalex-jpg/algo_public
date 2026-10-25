"""
Base de données locale (SQLite).

Un seul fichier : data/algo.db. Tables :
  - prix          : cours quotidiens de chaque action
  - macro         : séries macro (VIX, taux, inflation, EUR/USD...)
  - fondamentaux  : un instantané des ratios financiers par action et par jour
  - scores        : les scores calculés chaque jour (historique conservé)
  - journal       : le suivi de chaque exécution (succès / erreurs)
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

import config


def _schema() -> str:
    num = ",\n    ".join(f"{c} REAL" for c in config.CHAMPS_FONDAMENTAUX_NUM)
    txt = ",\n    ".join(f"{c} TEXT" for c in config.CHAMPS_FONDAMENTAUX_TXT)
    return f"""
CREATE TABLE IF NOT EXISTS prix (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS macro (
    serie  TEXT NOT NULL,
    date   TEXT NOT NULL,
    valeur REAL,
    source TEXT,
    PRIMARY KEY (serie, date)
);

CREATE TABLE IF NOT EXISTS fondamentaux (
    ticker       TEXT NOT NULL,
    date_releve  TEXT NOT NULL,
    {num},
    {txt},
    PRIMARY KEY (ticker, date_releve)
);

CREATE TABLE IF NOT EXISTS scores (
    date               TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    nom                TEXT,
    poche              TEXT,
    secteur            TEXT,
    score_fondamental  REAL,
    valorisation       REAL,
    qualite            REAL,
    croissance         REAL,
    bilan              REAL,
    bonus              REAL,
    completude         REAL,
    score_technique    REAL,
    tendance           REAL,
    repli              REAL,
    momentum           REAL,
    score_combine      REAL,
    signal             TEXT,
    raisons            TEXT,
    entree             TEXT,
    invalidation       TEXT,
    these              TEXT,
    details            TEXT,
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS journal (
    horodatage TEXT NOT NULL,
    etape      TEXT,
    statut     TEXT,
    message    TEXT
);

CREATE INDEX IF NOT EXISTS idx_prix_date ON prix (date);
"""


def connexion(db_path: Path | None = None) -> sqlite3.Connection:
    """Ouvre la base (et la crée si besoin)."""
    chemin = Path(db_path or config.DB_PATH)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(chemin)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_schema())
    return conn


# ------------------------------- prix ---------------------------------------
def upsert_prix(conn: sqlite3.Connection, ticker: str, df: pd.DataFrame) -> int:
    """Insère ou met à jour les cours d'une action. df : index = dates."""
    if df is None or df.empty:
        return 0
    lignes = [
        (
            ticker,
            pd.Timestamp(d).strftime("%Y-%m-%d"),
            _f(r.get("Open")), _f(r.get("High")), _f(r.get("Low")),
            _f(r.get("Close")), _f(r.get("Volume")),
        )
        for d, r in df.iterrows()
        if pd.notna(r.get("Close"))
    ]
    conn.executemany(
        """INSERT INTO prix (ticker, date, open, high, low, close, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(ticker, date) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low,
             close=excluded.close, volume=excluded.volume""",
        lignes,
    )
    conn.commit()
    return len(lignes)


def derniere_date_prix(conn: sqlite3.Connection, ticker: str) -> str | None:
    r = conn.execute("SELECT MAX(date) FROM prix WHERE ticker = ?", (ticker,)).fetchone()
    return r[0] if r and r[0] else None


def charger_prix(conn: sqlite3.Connection, tickers=None, depuis: str | None = None) -> pd.DataFrame:
    """Renvoie un DataFrame long : ticker, date, open, high, low, close, volume."""
    req = "SELECT ticker, date, open, high, low, close, volume FROM prix WHERE 1=1"
    params: list = []
    if tickers:
        tickers = list(tickers)
        req += f" AND ticker IN ({','.join('?' * len(tickers))})"
        params += tickers
    if depuis:
        req += " AND date >= ?"
        params.append(depuis)
    req += " ORDER BY ticker, date"
    df = pd.read_sql_query(req, conn, params=params)
    df["date"] = pd.to_datetime(df["date"])
    return df


# ------------------------------- macro --------------------------------------
def upsert_macro(conn: sqlite3.Connection, serie: str, source: str, valeurs: pd.Series) -> int:
    valeurs = valeurs.dropna()
    lignes = [
        (serie, pd.Timestamp(d).strftime("%Y-%m-%d"), float(v), source)
        for d, v in valeurs.items()
    ]
    conn.executemany(
        """INSERT INTO macro (serie, date, valeur, source) VALUES (?, ?, ?, ?)
           ON CONFLICT(serie, date) DO UPDATE SET valeur=excluded.valeur, source=excluded.source""",
        lignes,
    )
    conn.commit()
    return len(lignes)


def charger_macro(conn: sqlite3.Connection, serie: str) -> pd.Series:
    df = pd.read_sql_query(
        "SELECT date, valeur FROM macro WHERE serie = ? ORDER BY date", conn, params=[serie]
    )
    if df.empty:
        return pd.Series(dtype=float)
    return pd.Series(df["valeur"].to_numpy(dtype=float), index=pd.to_datetime(df["date"]), name=serie)


# ---------------------------- fondamentaux ----------------------------------
def enregistrer_fondamentaux(conn: sqlite3.Connection, ticker: str, date_releve: str, infos: dict) -> None:
    champs = config.CHAMPS_FONDAMENTAUX_NUM + config.CHAMPS_FONDAMENTAUX_TXT
    valeurs = [ticker, date_releve]
    for c in config.CHAMPS_FONDAMENTAUX_NUM:
        valeurs.append(_f(infos.get(c)))
    for c in config.CHAMPS_FONDAMENTAUX_TXT:
        v = infos.get(c)
        valeurs.append(str(v) if v is not None else None)
    colonnes = ", ".join(["ticker", "date_releve"] + champs)
    marqueurs = ", ".join("?" * len(valeurs))
    maj = ", ".join(f"{c}=excluded.{c}" for c in champs)
    conn.execute(
        f"""INSERT INTO fondamentaux ({colonnes}) VALUES ({marqueurs})
            ON CONFLICT(ticker, date_releve) DO UPDATE SET {maj}""",
        valeurs,
    )
    conn.commit()


def charger_fondamentaux_recents(conn: sqlite3.Connection) -> pd.DataFrame:
    """Le relevé le plus récent de chaque action, indexé par ticker."""
    df = pd.read_sql_query(
        """SELECT f.* FROM fondamentaux f
           JOIN (SELECT ticker, MAX(date_releve) AS d FROM fondamentaux GROUP BY ticker) m
             ON f.ticker = m.ticker AND f.date_releve = m.d""",
        conn,
    )
    return df.set_index("ticker")


# ------------------------------- scores -------------------------------------
COLONNES_SCORES = [
    "nom", "poche", "secteur", "score_fondamental", "valorisation", "qualite",
    "croissance", "bilan", "bonus", "completude", "score_technique", "tendance",
    "repli", "momentum", "score_combine", "signal", "raisons", "entree",
    "invalidation", "these", "details",
]


def enregistrer_scores(conn: sqlite3.Connection, date: str, df: pd.DataFrame) -> None:
    """df indexé par ticker, avec (au moins une partie de) COLONNES_SCORES."""
    lignes = []
    for ticker, r in df.iterrows():
        ligne = [date, ticker]
        for c in COLONNES_SCORES:
            v = r.get(c)
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False, default=str)
            elif v is not None and not isinstance(v, str) and pd.isna(v):
                v = None
            elif hasattr(v, "item"):  # types numpy -> types Python
                v = v.item()
            ligne.append(v)
        lignes.append(ligne)
    colonnes = ", ".join(["date", "ticker"] + COLONNES_SCORES)
    marqueurs = ", ".join("?" * (len(COLONNES_SCORES) + 2))
    conn.executemany(f"INSERT OR REPLACE INTO scores ({colonnes}) VALUES ({marqueurs})", lignes)
    conn.commit()


def maj_these(conn: sqlite3.Connection, date: str, ticker: str, these: str) -> None:
    conn.execute("UPDATE scores SET these = ? WHERE date = ? AND ticker = ?", (these, date, ticker))
    conn.commit()


# ------------------------------- journal ------------------------------------
def journaliser(conn: sqlite3.Connection, etape: str, statut: str, message: str = "") -> None:
    conn.execute(
        "INSERT INTO journal (horodatage, etape, statut, message) VALUES (?, ?, ?, ?)",
        (datetime.now().isoformat(timespec="seconds"), etape, statut, message[:2000]),
    )
    conn.commit()


def statistiques(conn: sqlite3.Connection) -> dict:
    """Résumé du contenu de la base (pour la commande 'statut')."""
    q = lambda sql: conn.execute(sql).fetchone()
    return {
        "prix_lignes": q("SELECT COUNT(*) FROM prix")[0],
        "prix_actions": q("SELECT COUNT(DISTINCT ticker) FROM prix")[0],
        "prix_derniere_date": q("SELECT MAX(date) FROM prix")[0],
        "macro_series": q("SELECT COUNT(DISTINCT serie) FROM macro")[0],
        "fondamentaux_dernier_releve": q("SELECT MAX(date_releve) FROM fondamentaux")[0],
        "fondamentaux_actions": q("SELECT COUNT(DISTINCT ticker) FROM fondamentaux")[0],
        "scores_derniere_date": q("SELECT MAX(date) FROM scores")[0],
        "derniere_execution": q("SELECT horodatage, etape, statut FROM journal ORDER BY rowid DESC LIMIT 1"),
    }


def _f(v):
    """Convertit en float, ou None si absent / invalide."""
    try:
        if v is None:
            return None
        v = float(v)
        return None if v != v else v  # NaN -> None
    except (TypeError, ValueError):
        return None
