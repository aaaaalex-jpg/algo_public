"""
Suivi du portefeuille : réel et fictif.

  - "reel"   : tes vrais comptes. Tu enregistres toi-même tes achats, ventes et apports
               (python main.py achat / vente / apport). L'algo valorise, compare au marché
               et t'alerte si une de tes positions casse sa thèse.
  - "fictif" : le même capital de départ, géré automatiquement par l'algo selon les règles
               de config.py. C'est le test le plus honnête possible : des décisions prises
               chaque matin sans connaître la suite, exécutées à l'ouverture de la séance
               suivante, avec frais et glissement de prix.

Règles du portefeuille fictif :
  - achat d'un "Candidat à étudier" si la poche a du cash, de la place et du budget de trades
  - "cash is a position" : aucun nouvel achat si le contexte de marché est "Défensif"
  - vente sur rupture de thèse (fondamental qui s'effondre ou tendance cassée),
    jamais sur la simple volatilité
  - budget de BUDGET_TRADES_AN ordres sur 365 jours glissants ; au-delà, seulement
    les ventes de protection (gros risque) et les achats à score >= SCORE_EXCEPTIONNEL
    (grosse opportunité), dans la limite de RESERVE_EXCEPTIONNELLE ordres

Garde-fous (valables pour le fictif, et sous forme d'alertes pour le réel) :
  - stop par ligne à -STOP_LIGNE depuis le prix de revient
  - perte maximale du portefeuille (drawdown) à trois niveaux : vigilance, protection, urgence
  - au plus MAX_SECTEUR d'une poche dans un même secteur
  - au plus MAX_ACHATS_PAR_JOUR nouveaux achats par jour
"""
import html
import logging
import math
from datetime import date, datetime, timedelta

import sqlite3

import pandas as pd

import config
from modules import db, regles_vente as rv

log = logging.getLogger("algo")

PORTEFEUILLES = ("reel", "fictif")
POCHES = ("CTO", "PEA")

SCHEMA = """
CREATE TABLE IF NOT EXISTS portefeuilles (
    nom TEXT PRIMARY KEY, date_debut TEXT, cree_le TEXT
);
CREATE TABLE IF NOT EXISTS cash (
    portefeuille TEXT, poche TEXT, montant REAL, PRIMARY KEY (portefeuille, poche)
);
CREATE TABLE IF NOT EXISTS benchmark (
    portefeuille TEXT, poche TEXT, unites REAL, PRIMARY KEY (portefeuille, poche)
);
CREATE TABLE IF NOT EXISTS positions (
    portefeuille TEXT, ticker TEXT, poche TEXT, quantite REAL,
    prix_revient_eur REAL, date_entree TEXT, score_entree REAL,
    PRIMARY KEY (portefeuille, ticker)
);
CREATE TABLE IF NOT EXISTS ordres (
    id INTEGER PRIMARY KEY AUTOINCREMENT, portefeuille TEXT, date_ordre TEXT,
    ticker TEXT, poche TEXT, sens TEXT, montant_eur REAL, motif TEXT,
    statut TEXT, date_execution TEXT, commentaire TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, portefeuille TEXT, date TEXT, ticker TEXT,
    poche TEXT, sens TEXT, quantite REAL, prix_local REAL, taux_change REAL,
    prix_eur REAL, frais_eur REAL, montant_eur REAL, pnl_eur REAL, motif TEXT
);
CREATE TABLE IF NOT EXISTS apports (
    portefeuille TEXT, date TEXT, poche TEXT, montant REAL
);
CREATE TABLE IF NOT EXISTS valorisations (
    portefeuille TEXT, date TEXT, poche TEXT, titres_eur REAL, cash_eur REAL,
    total_eur REAL, benchmark_eur REAL, PRIMARY KEY (portefeuille, date, poche)
);
CREATE TABLE IF NOT EXISTS decisions_jour (
    portefeuille TEXT, date_marche TEXT, PRIMARY KEY (portefeuille, date_marche)
);
"""


def assurer_schema(conn) -> None:
    conn.executescript(SCHEMA)
    try:  # bases créées avant la vente partielle : on ajoute la colonne
        conn.execute("ALTER TABLE ordres ADD COLUMN fraction REAL")
    except sqlite3.OperationalError:
        pass


# ----------------------------------------------------------- prix et devises --
def _serie_avant(conn, serie: str, jour: str) -> float | None:
    r = conn.execute("SELECT valeur FROM macro WHERE serie = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                     (serie, jour)).fetchone()
    return r[0] if r else None


def taux_change(conn, devise: str, jour: str) -> float:
    """Nombre d'unités de la devise pour 1 euro (1 si EUR)."""
    if devise == "EUR":
        return 1.0
    v = _serie_avant(conn, f"EUR{devise}", jour)
    return v if v else config.TAUX_DEFAUT.get(devise, 1.0)


def cours(conn, ticker: str, jour: str) -> tuple[str, float] | None:
    """Dernière clôture connue à la date donnée : (date, cours en devise locale)."""
    r = conn.execute("SELECT date, close FROM prix WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                     (ticker, jour)).fetchone()
    return (r[0], r[1]) if r else None


def date_marche(conn) -> str | None:
    """Date de la dernière séance disponible dans la base."""
    r = conn.execute("SELECT MAX(date) FROM prix").fetchone()
    return r[0] if r and r[0] else None


def _niveau_benchmark_eur(conn, poche: str, jour: str) -> float | None:
    serie, dev, secours = config.BENCHMARK[poche]
    v = _serie_avant(conn, serie, jour) or _serie_avant(conn, secours, jour)
    if not v:
        return None
    return v / taux_change(conn, dev, jour)


# -------------------------------------------------------------- initialisation --
def existe(conn, nom: str) -> bool:
    assurer_schema(conn)
    return conn.execute("SELECT 1 FROM portefeuilles WHERE nom = ?", (nom,)).fetchone() is not None


def initialiser(conn, forcer: bool = False, jour: str | None = None) -> list[str]:
    """Crée les portefeuilles réel et fictif avec le capital de config.CAPITAL_INITIAL."""
    assurer_schema(conn)
    jour = jour or date_marche(conn) or date.today().isoformat()
    crees = []
    for nom in PORTEFEUILLES:
        if existe(conn, nom) and not forcer:
            continue
        for table in ("cash", "benchmark", "positions", "ordres", "transactions", "apports",
                      "valorisations", "decisions_jour"):
            conn.execute(f"DELETE FROM {table} WHERE portefeuille = ?", (nom,))
        conn.execute("INSERT OR REPLACE INTO portefeuilles VALUES (?, ?, ?)",
                     (nom, jour, datetime.now().isoformat(timespec="seconds")))
        for poche, montant in config.CAPITAL_INITIAL.items():
            conn.execute("INSERT INTO cash VALUES (?, ?, ?)", (nom, poche, float(montant)))
            niveau = _niveau_benchmark_eur(conn, poche, jour)
            conn.execute("INSERT INTO benchmark VALUES (?, ?, ?)",
                         (nom, poche, montant / niveau if niveau else None))
        crees.append(nom)
    conn.commit()
    return crees


def _cash(conn, nom: str, poche: str) -> float:
    r = conn.execute("SELECT montant FROM cash WHERE portefeuille = ? AND poche = ?", (nom, poche)).fetchone()
    return r[0] if r else 0.0


def _ajuster_cash(conn, nom: str, poche: str, delta: float) -> None:
    conn.execute("UPDATE cash SET montant = montant + ? WHERE portefeuille = ? AND poche = ?", (delta, nom, poche))


def positions(conn, nom: str) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM positions WHERE portefeuille = ? ORDER BY poche, ticker",
                             conn, params=[nom]).set_index("ticker")


# --------------------------------------------------------------------- apports --
def apport(conn, poche: str, montant: float, jour: str | None = None, portefeuilles=PORTEFEUILLES) -> None:
    """Ajoute de l'argent sur une poche (par défaut sur le réel ET le fictif, pour qu'ils restent comparables).
    Le même montant est "investi" dans la référence, pour une comparaison juste."""
    assurer_schema(conn)
    jour = jour or date_marche(conn) or date.today().isoformat()
    niveau = _niveau_benchmark_eur(conn, poche, jour)
    for nom in portefeuilles:
        if not existe(conn, nom):
            raise RuntimeError("Portefeuilles non initialisés : lance  python main.py init-portefeuille")
        _ajuster_cash(conn, nom, poche, montant)
        conn.execute("INSERT INTO apports VALUES (?, ?, ?, ?)", (nom, jour, poche, montant))
        if niveau:
            conn.execute("UPDATE benchmark SET unites = COALESCE(unites, 0) + ? WHERE portefeuille = ? AND poche = ?",
                         (montant / niveau, nom, poche))
    conn.commit()
    # si la valorisation de ce jour existe déjà, on la met à jour : sinon l'apport
    # serait pris pour une perte puis un gain, et fausserait les garde-fous
    for nom in portefeuilles:
        if conn.execute("SELECT 1 FROM valorisations WHERE portefeuille = ? AND date = ?", (nom, jour)).fetchone():
            valoriser(conn, nom, jour)


# ---------------------------------------------------------------- transactions --
def _enregistrer_transaction(conn, nom, jour, ticker, poche, sens, quantite, prix_local, taux, frais_eur, motif):
    prix_eur = prix_local / taux
    brut = quantite * prix_eur
    pnl = None
    if sens == "achat":
        montant = brut + frais_eur
        _ajuster_cash(conn, nom, poche, -montant)
        pos = conn.execute("SELECT quantite, prix_revient_eur FROM positions WHERE portefeuille = ? AND ticker = ?",
                           (nom, ticker)).fetchone()
        if pos:
            q = pos[0] + quantite
            pru = (pos[0] * pos[1] + montant) / q
            conn.execute("UPDATE positions SET quantite = ?, prix_revient_eur = ? WHERE portefeuille = ? AND ticker = ?",
                         (q, pru, nom, ticker))
        else:
            score = conn.execute("SELECT score_combine FROM scores WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                                 (ticker, jour)).fetchone()
            conn.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (nom, ticker, poche, quantite, montant / quantite, jour, score[0] if score else None))
    else:
        pos = conn.execute("SELECT quantite, prix_revient_eur FROM positions WHERE portefeuille = ? AND ticker = ?",
                           (nom, ticker)).fetchone()
        if not pos or pos[0] < quantite - 1e-9:
            raise RuntimeError(f"Vente impossible : tu ne détiens pas {quantite} {ticker} dans le portefeuille {nom}")
        montant = brut - frais_eur
        pnl = montant - quantite * pos[1]
        _ajuster_cash(conn, nom, poche, montant)
        reste = pos[0] - quantite
        if reste <= 1e-9:
            conn.execute("DELETE FROM positions WHERE portefeuille = ? AND ticker = ?", (nom, ticker))
        else:
            conn.execute("UPDATE positions SET quantite = ? WHERE portefeuille = ? AND ticker = ?", (reste, nom, ticker))
    conn.execute(
        """INSERT INTO transactions (portefeuille, date, ticker, poche, sens, quantite, prix_local, taux_change,
           prix_eur, frais_eur, montant_eur, pnl_eur, motif) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (nom, jour, ticker, poche, sens, quantite, prix_local, taux, prix_eur, frais_eur, montant, pnl, motif))
    return montant, pnl


def transaction_reelle(conn, sens: str, ticker: str, quantite: float, prix_local: float,
                       jour: str | None = None, frais_eur: float = 0.0) -> str:
    """Enregistre un achat ou une vente que TU as passé chez ton courtier."""
    assurer_schema(conn)
    if not existe(conn, "reel"):
        raise RuntimeError("Portefeuilles non initialisés : lance  python main.py init-portefeuille")
    ticker = ticker.upper() if ticker.upper() in config.UNIVERS else ticker
    if ticker not in config.UNIVERS:
        raise RuntimeError(f"{ticker} n'est pas dans l'univers : ajoute-le d'abord dans config.py")
    jour = jour or date.today().isoformat()
    poche = config.poche(ticker)
    taux = taux_change(conn, config.devise(ticker), jour)
    if sens == "achat":
        cout = quantite * prix_local / taux + frais_eur
        if cout > _cash(conn, "reel", poche) + 1e-6:
            raise RuntimeError(f"Cash insuffisant sur le {poche} : {cout:,.2f} € nécessaires, "
                               f"{_cash(conn, 'reel', poche):,.2f} € disponibles (as-tu enregistré ton apport ?)")
    montant, pnl = _enregistrer_transaction(conn, "reel", jour, ticker, poche, sens, quantite, prix_local, taux,
                                            frais_eur, "saisie manuelle")
    conn.commit()
    txt = f"{'Achat enregistré' if sens == 'achat' else 'Vente enregistrée'} : {quantite:g} {ticker} à {prix_local:,.2f} {config.devise(ticker)} " \
          f"({montant:,.2f} € sur le {poche})"
    if pnl is not None:
        txt += f" — plus/moins-value : {pnl:+,.2f} €"
    return txt


# ------------------------------------------------------- portefeuille fictif --
def _prochaine_ouverture(conn, ticker: str, apres: str):
    return conn.execute("SELECT date, open, close FROM prix WHERE ticker = ? AND date > ? ORDER BY date LIMIT 1",
                        (ticker, apres)).fetchone()


def executer_ordres_en_attente(conn, nom: str = "fictif") -> list[str]:
    """Exécute les ordres simulés à l'ouverture de la séance qui suit leur décision."""
    messages = []
    ordres = conn.execute("SELECT id, date_ordre, ticker, poche, sens, montant_eur, motif, fraction FROM ordres "
                          "WHERE portefeuille = ? AND statut = 'en_attente' ORDER BY sens DESC, id", (nom,)).fetchall()
    # les ventes ("vente" > "achat") passent d'abord : elles libèrent du cash
    for oid, d_ordre, t, poche, sens, montant_cible, motif, fraction in ordres:
        barre = _prochaine_ouverture(conn, t, d_ordre)
        if not barre:
            continue  # la séance suivante n'est pas encore dans la base
        jour, ouverture, cloture = barre
        prix = ouverture if ouverture and ouverture > 0 else cloture
        taux = taux_change(conn, config.devise(t), jour)
        frais_pct = config.FRAIS_COURTAGE[poche] + (config.FRAIS_CHANGE if config.devise(t) != "EUR" else 0)
        if sens == "achat":
            prix *= 1 + config.GLISSEMENT
            budget = min(montant_cible, _cash(conn, nom, poche))
            quantite = math.floor(budget / (prix / taux * (1 + frais_pct)))
            if quantite < 1:
                statut, com = "annule", "montant insuffisant pour acheter une action"
            else:
                frais = quantite * prix / taux * frais_pct
                _enregistrer_transaction(conn, nom, jour, t, poche, "achat", quantite, prix, taux, frais, motif)
                statut, com = "execute", f"{quantite} × {prix:,.2f} {config.devise(t)}"
        else:
            pos = conn.execute("SELECT quantite FROM positions WHERE portefeuille = ? AND ticker = ?", (nom, t)).fetchone()
            qv = 0 if not pos else (pos[0] if not fraction or fraction >= 1 else math.floor(pos[0] * fraction))
            if not pos:
                statut, com = "annule", "plus de position"
            elif qv < 1:
                statut, com = "annule", "ligne trop petite pour une vente partielle"
            else:
                prix *= 1 - config.GLISSEMENT
                frais = qv * prix / taux * frais_pct
                _, pnl = _enregistrer_transaction(conn, nom, jour, t, poche, "vente", qv, prix, taux, frais, motif)
                statut, com = "execute", f"{qv:g} × {prix:,.2f} {config.devise(t)}, résultat {pnl:+,.0f} €"
        conn.execute("UPDATE ordres SET statut = ?, date_execution = ?, commentaire = ? WHERE id = ?",
                     (statut, jour, com, oid))
        messages.append(f"{sens} {t} : {statut} ({com})")
    conn.commit()
    return messages


def rupture_these(ligne) -> str | None:
    """Motif de vente si l'entreprise se dégrade (score fondamental), sinon None.
    Les règles de prix (tendance, stop, gagnants) sont dans modules/regles_vente.py."""
    if ligne is None:
        return None
    sf = ligne.get("score_fondamental")
    if pd.notna(sf) and sf < config.SEUILS["eviter_fond"]:
        return f"rupture fondamentale (score {sf:.0f} < {config.SEUILS['eviter_fond']})"
    return None


def etat_ligne(conn, nom: str, ticker: str, jour: str, r: dict) -> rv.EtatLigne | None:
    """Mémoire de la ligne (plus haut, séances sous la MM200, vente partielle) reconstituée
    à partir des cours enregistrés : la même que celle tenue par le backtest."""
    pos = conn.execute("SELECT date_entree, prix_revient_eur FROM positions WHERE portefeuille = ? AND ticker = ?",
                       (nom, ticker)).fetchone()
    if not pos:
        return None
    date_entree, pru_eur = pos
    achats = conn.execute("SELECT SUM(quantite * prix_local), SUM(quantite) FROM transactions WHERE portefeuille = ? "
                          "AND ticker = ? AND sens = 'achat' AND date >= ?", (nom, ticker, date_entree)).fetchone()
    prix_entree = (achats[0] / achats[1] if achats and achats[1]
                   else pru_eur * taux_change(conn, config.devise(ticker), date_entree))
    partielle = conn.execute("SELECT 1 FROM transactions WHERE portefeuille = ? AND ticker = ? AND sens = 'vente' "
                             "AND date >= ? AND motif LIKE 'prise de bénéfice%'", (nom, ticker, date_entree)).fetchone()
    h = pd.read_sql_query("SELECT date, close FROM prix WHERE ticker = ? AND date <= ? ORDER BY date",
                          conn, params=[ticker, jour])
    closes = pd.Series(h["close"].to_numpy(dtype=float), index=pd.to_datetime(h["date"]))
    return rv.etat_depuis_historique(closes, date_entree, prix_entree, r, partielle is not None)


def taille_cible(score: float, valeur_poche: float) -> float:
    facteur = min(1.5, max(0.5, 0.5 + (score - 60) / 20))
    return min(config.TAILLE_BASE * facteur, config.TAILLE_MAX) * valeur_poche


def _nb_trades_12_mois(conn, nom: str, jour: str) -> int:
    depuis = (pd.Timestamp(jour) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    faits = conn.execute("SELECT COUNT(*) FROM transactions WHERE portefeuille = ? AND date > ?", (nom, depuis)).fetchone()[0]
    attente = conn.execute("SELECT COUNT(*) FROM ordres WHERE portefeuille = ? AND statut = 'en_attente'", (nom,)).fetchone()[0]
    return faits + attente


def _nb_exceptionnels_12_mois(conn, nom: str, jour: str) -> int:
    """Ordres passés hors budget (gros risque ou grosse opportunité)."""
    depuis = (pd.Timestamp(jour) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    return conn.execute("SELECT COUNT(*) FROM transactions WHERE portefeuille = ? AND date > ? AND motif LIKE '%hors budget%'",
                        (nom, depuis)).fetchone()[0] + conn.execute(
        "SELECT COUNT(*) FROM ordres WHERE portefeuille = ? AND statut = 'en_attente' AND motif LIKE '%hors budget%'",
        (nom,)).fetchone()[0]


# ------------------------------------------------------------------ garde-fous --
NIVEAUX = ("Normal", "Vigilance", "Protection", "Urgence")
REGLES = (
    "aucune restriction",
    f"achats seulement si score ≥ {config.SCORE_VIGILANCE}, tailles divisées par 2",
    "plus aucun achat, le cash reste au repos",
    f"plus aucun achat et vente des lignes qui perdent {config.PERTE_LIGNE_URGENCE * 100:.0f} % ou plus",
)


def indice_performance(conn, nom: str) -> pd.Series:
    """Indice de performance du portefeuille (base 1), apports neutralisés.

    Un apport de 1 000 € n'est pas un gain : on calcule chaque jour le rendement
    (valeur du jour - apports du jour) / valeur de la veille, puis on les enchaîne."""
    v = pd.read_sql_query("SELECT date, SUM(total_eur) AS total FROM valorisations WHERE portefeuille = ? "
                          "GROUP BY date ORDER BY date", conn, params=[nom])
    if v.empty:
        return pd.Series(dtype=float)
    valeurs = v.set_index("date")["total"]
    flux = pd.Series(0.0, index=valeurs.index)
    for d, montant in conn.execute("SELECT date, montant FROM apports WHERE portefeuille = ?", (nom,)):
        i = valeurs.index.searchsorted(d)  # apport rattaché à la première valorisation qui suit
        if i < len(valeurs):
            flux.iloc[i] += montant
    rendements = ((valeurs - flux) / valeurs.shift(1) - 1).fillna(0.0)
    return (1 + rendements).cumprod()


def niveau_recul(recul: float) -> int:
    niveau = 0
    for i, seuil in enumerate((config.DRAWDOWN_VIGILANCE, config.DRAWDOWN_PROTECTION, config.DRAWDOWN_URGENCE), 1):
        if recul >= seuil - 1e-9:
            niveau = i
    return niveau


class SuiviGardeFous:
    """Niveau de garde-fou séance après séance, avec pause puis reprise.

    Sans pause, un portefeuille repassé en cash après une chute resterait bloqué
    à jamais : le cash ne remonte pas, le recul depuis le plus haut ne se résorbe
    jamais, et plus aucun achat ne serait possible. D'où la règle :
      - Protection ou Urgence -> pause de PAUSE_GARDE_FOU séances (aucun achat ;
        en Urgence, les lignes perdantes sont vendues)
      - fin de la pause -> le plus haut de référence est remis à la valeur du jour,
        et la reprise est prudente (Vigilance) pendant PAUSE_GARDE_FOU séances
    Utilisé à l'identique par le portefeuille fictif et par le backtest."""

    def __init__(self):
        self.pic = None
        self.pause = 0
        self.reprise = 0

    def maj(self, indice: float) -> tuple[int, float, str]:
        self.pic = indice if self.pic is None else max(self.pic, indice)
        recul = 1 - indice / self.pic
        niveau = niveau_recul(recul)
        etat = ""
        if self.pause > 0:
            self.pause -= 1
            if self.pause == 0:
                self.pic, recul = indice, 0.0
                self.reprise = config.PAUSE_GARDE_FOU
                niveau, etat = 1, "reprise"
            else:
                niveau, etat = max(niveau, 2), "pause"
        elif niveau >= 2:
            self.pause = config.PAUSE_GARDE_FOU
            etat = "pause"
        elif self.reprise > 0:
            self.reprise -= 1
            if niveau <= 1:
                niveau, etat = 1, "reprise"
        return niveau, recul, etat


def etat_garde_fous(conn, nom: str) -> dict:
    """Recul du portefeuille depuis son plus haut et niveau de garde-fou correspondant."""
    indice = indice_performance(conn, nom)
    if len(indice) < 2:
        return {"niveau": 0, "libelle": NIVEAUX[0], "regle": REGLES[0], "drawdown": 0.0, "perte_jour": 0.0, "etat": ""}
    suivi = SuiviGardeFous()
    for v in indice:
        niveau, recul, etat = suivi.maj(float(v))
    perte_jour = float(max(0.0, 1 - indice.iloc[-1] / indice.iloc[-2]))
    libelle, regle = NIVEAUX[niveau], REGLES[niveau]
    if etat == "pause":
        libelle += f" (pause, encore {suivi.pause} séance(s))"
    elif etat == "reprise":
        libelle, regle = "Reprise prudente", f"{REGLES[1]} (encore {suivi.reprise} séance(s))"
    return {"niveau": niveau, "libelle": libelle, "regle": regle,
            "drawdown": float(recul), "perte_jour": perte_jour, "etat": etat}


def perf_ligne(conn, nom: str, ticker: str, jour: str) -> float | None:
    """Plus ou moins-value de la ligne depuis l'achat, en euros (change compris)."""
    r = conn.execute("SELECT prix_revient_eur FROM positions WHERE portefeuille = ? AND ticker = ?", (nom, ticker)).fetchone()
    c = cours(conn, ticker, jour)
    if not r or not c or not r[0]:
        return None
    return (c[1] / taux_change(conn, config.devise(ticker), jour)) / r[0] - 1


def motif_vente(conn, nom: str, ticker: str, ligne, jour: str, niveau: int = 0) -> tuple[str | None, float]:
    """(raison de vendre, part de la ligne à vendre) ou (None, 0).
    D'abord l'entreprise (score fondamental), puis la règle de vente commune avec le backtest."""
    motif = rupture_these(ligne)
    if motif:
        return motif, 1.0
    r = rv.regle()
    perf = perf_ligne(conn, nom, ticker, jour) or 0.0
    etat = etat_ligne(conn, nom, ticker, jour, r)
    c = cours(conn, ticker, jour)
    if etat is None or c is None:
        if perf <= -config.STOP_LIGNE:
            return f"stop de protection ({perf * 100:.1f} % depuis l'achat)", 1.0
        return None, 0.0
    tendance = ligne.get("tendance_haussiere") if ligne is not None else None
    tendance_ok = True if tendance is None or pd.isna(tendance) else bool(tendance)
    return rv.decider(etat, c[1], perf, r, niveau, tendance_ok)


def lignes_a_ecreter(conn, nom: str, jour: str) -> list[tuple[str, float, float]]:
    """Lignes qui dépassent ECRETAGE_SEUIL de leur poche : (ticker, part à vendre, poids actuel)."""
    sortie = []
    valeurs = valoriser(conn, nom, jour, enregistrer=False)
    for p in POCHES:
        tot = valeurs[p]["total_eur"]
        for l in valeurs[p]["lignes"]:
            poids = l["valeur_eur"] / tot if tot else 0.0
            if poids > config.ECRETAGE_SEUIL and l["valeur_eur"] - config.ECRETAGE_CIBLE * tot >= config.MONTANT_MIN_ORDRE:
                sortie.append((l["ticker"], 1 - config.ECRETAGE_CIBLE * tot / l["valeur_eur"], poids))
    return sortie


def decider_fictif(conn, res: pd.DataFrame, ctx: dict, jour: str, garde: dict | None = None) -> list[str]:
    """Décisions du matin pour le portefeuille fictif (une seule fois par séance)."""
    from modules.rapport import SIGNAL_CANDIDAT

    nom = "fictif"
    if conn.execute("SELECT 1 FROM decisions_jour WHERE portefeuille = ? AND date_marche = ?", (nom, jour)).fetchone():
        return []
    garde = garde or etat_garde_fous(conn, nom)
    niveau = garde["niveau"]
    decisions = []
    en_attente = {r[0] for r in conn.execute(
        "SELECT ticker FROM ordres WHERE portefeuille = ? AND statut = 'en_attente'", (nom,))}
    pos = positions(conn, nom)

    def ordre(t, sens, montant, motif, fraction=None):
        conn.execute("INSERT INTO ordres (portefeuille, date_ordre, ticker, poche, sens, montant_eur, motif, statut, "
                     "fraction) VALUES (?, ?, ?, ?, ?, ?, ?, 'en_attente', ?)",
                     (nom, jour, t, config.poche(t), sens, montant, motif, fraction))
        en_attente.add(t)

    if niveau:
        decisions.append(f"Garde-fou {garde['libelle']} : le portefeuille est à -{garde['drawdown'] * 100:.1f} % "
                         f"de son plus haut → {garde['regle']}")

    # 1. ventes de protection : JAMAIS bloquées par le budget (gros risque)
    for t in pos.index:
        if t in en_attente:
            continue
        motif, part = motif_vente(conn, nom, t, res.loc[t] if t in res.index else None, jour, niveau)
        if motif:
            if _nb_trades_12_mois(conn, nom, jour) >= config.BUDGET_TRADES_AN:
                if part < 1:
                    continue  # une prise de bénéfice partielle respecte le budget
                motif += " [hors budget : gros risque]"
            ordre(t, "vente", None, motif, part)
            decisions.append(f"VENTE {'PARTIELLE ' if part < 1 else ''}{t} — {motif}")

    # 1 bis. écrêtage : une ligne devenue trop grosse est ramenée à ECRETAGE_CIBLE de sa poche
    for t, part, poids in lignes_a_ecreter(conn, nom, jour):
        if t in en_attente:
            continue
        motif = (f"écrêtage : ligne à {poids * 100:.0f} % de la poche, ramenée à {config.ECRETAGE_CIBLE * 100:.0f} %"
                 + (" [hors budget : protection]" if _nb_trades_12_mois(conn, nom, jour) >= config.BUDGET_TRADES_AN else ""))
        ordre(t, "vente", None, motif, part)
        decisions.append(f"VENTE PARTIELLE {t} — {motif}")

    # 2. achats, si le contexte et les garde-fous le permettent
    if ctx.get("regime") == "Défensif":
        decisions.append("Aucun achat : contexte défensif, on garde le cash (« cash is a position »)")
    elif niveau >= 2:
        decisions.append(f"Aucun achat : garde-fou {garde['libelle']} actif")
    else:
        valeurs = valoriser(conn, nom, jour, enregistrer=False)
        cash_libre = {p: _cash(conn, nom, p) - sum(
            r[0] or 0 for r in conn.execute("SELECT montant_eur FROM ordres WHERE portefeuille = ? AND poche = ? "
                                            "AND statut = 'en_attente' AND sens = 'achat'", (nom, p)))
            for p in POCHES}
        nb_pos = {p: int((pos["poche"] == p).sum()) for p in POCHES}
        # exposition par (poche, secteur) : positions détenues + achats en attente
        expo: dict[tuple[str, str], float] = {}
        for p in POCHES:
            for l in valeurs[p]["lignes"]:
                cle = (p, config.UNIVERS.get(l["ticker"], {}).get("secteur", "?"))
                expo[cle] = expo.get(cle, 0.0) + l["valeur_eur"]
        for t, m in conn.execute("SELECT ticker, montant_eur FROM ordres WHERE portefeuille = ? AND statut = 'en_attente' "
                                 "AND sens = 'achat'", (nom,)):
            cle = (config.poche(t), config.UNIVERS[t]["secteur"])
            expo[cle] = expo.get(cle, 0.0) + (m or 0)

        ecartes = {"budget": [], "fin_budget": [], "vigilance": [], "secteur": []}
        nb_achats = 0
        candidats = res[(res["signal"] == SIGNAL_CANDIDAT)].sort_values("score_combine", ascending=False)
        for t, r in candidats.iterrows():
            if t in pos.index or t in en_attente:
                continue
            if nb_achats >= config.MAX_ACHATS_PAR_JOUR:
                decisions.append(f"Limite de {config.MAX_ACHATS_PAR_JOUR} nouveaux achats par jour atteinte : "
                                 "les autres candidats attendront")
                break
            score = r["score_combine"]
            p, secteur = config.poche(t), config.UNIVERS[t]["secteur"]
            n = _nb_trades_12_mois(conn, nom, jour)
            exceptionnel = False
            if n >= config.BUDGET_TRADES_AN:
                plafond = config.BUDGET_TRADES_AN + config.RESERVE_EXCEPTIONNELLE
                if score >= config.SCORE_EXCEPTIONNEL and n < plafond:
                    exceptionnel = True
                else:
                    ecartes["budget"].append(t)
                    continue
            elif n >= config.SEUIL_ALERTE_BUDGET and score < config.SCORE_FIN_BUDGET:
                ecartes["fin_budget"].append(t)
                continue
            if niveau == 1 and score < config.SCORE_VIGILANCE:
                ecartes["vigilance"].append(t)
                continue
            if nb_pos[p] >= config.MAX_POSITIONS_PAR_POCHE:
                continue
            total_p = valeurs[p]["total_eur"]
            montant = taille_cible(score, total_p)
            if niveau == 1:
                montant *= config.FACTEUR_TAILLE_VIGILANCE
            place_secteur = config.MAX_SECTEUR * total_p - expo.get((p, secteur), 0.0)
            montant = min(montant, cash_libre[p], place_secteur)
            if montant < config.MONTANT_MIN_ORDRE:
                if place_secteur < config.MONTANT_MIN_ORDRE:
                    ecartes["secteur"].append(t)
                continue
            motif = f"candidat, score {score:.0f}" + (" [hors budget : grosse opportunité]" if exceptionnel else "")
            ordre(t, "achat", montant, motif)
            cash_libre[p] -= montant
            nb_pos[p] += 1
            expo[(p, secteur)] = expo.get((p, secteur), 0.0) + montant
            nb_achats += 1
            decisions.append(f"ACHAT {t} ({p}) — environ {montant:,.0f} € (score {score:.0f})"
                             + (" — exceptionnel, hors budget" if exceptionnel else ""))

        if ecartes["budget"]:
            decisions.append(f"Budget de {config.BUDGET_TRADES_AN} ordres/an atteint : non acheté(s) "
                             f"{', '.join(ecartes['budget'])} (seuls les scores ≥ {config.SCORE_EXCEPTIONNEL} passent)")
        if ecartes["fin_budget"]:
            decisions.append(f"Budget presque consommé (≥ {config.SEUIL_ALERTE_BUDGET} ordres) : score < "
                             f"{config.SCORE_FIN_BUDGET}, non acheté(s) {', '.join(ecartes['fin_budget'])}")
        if ecartes["vigilance"]:
            decisions.append(f"Garde-fou Vigilance : score < {config.SCORE_VIGILANCE}, non acheté(s) "
                             f"{', '.join(ecartes['vigilance'])}")
        if ecartes["secteur"]:
            decisions.append(f"Secteur déjà à {config.MAX_SECTEUR * 100:.0f} % de la poche : non acheté(s) "
                             f"{', '.join(ecartes['secteur'])}")

    conn.execute("INSERT INTO decisions_jour VALUES (?, ?)", (nom, jour))
    conn.commit()
    return decisions


# ---------------------------------------------------------------- valorisation --
def valoriser(conn, nom: str, jour: str, enregistrer: bool = True) -> dict:
    """Valeur de chaque poche en euros + valeur de la référence (benchmark)."""
    pos = positions(conn, nom)
    resultat = {}
    for p in POCHES:
        titres = 0.0
        lignes = []
        for t, r in pos[pos["poche"] == p].iterrows():
            c = cours(conn, t, jour)
            taux = taux_change(conn, config.devise(t), jour)
            prix_eur = (c[1] / taux) if c else r["prix_revient_eur"]
            valeur = r["quantite"] * prix_eur
            titres += valeur
            lignes.append({"ticker": t, "quantite": r["quantite"], "pru_eur": r["prix_revient_eur"],
                           "cours_eur": prix_eur, "valeur_eur": valeur,
                           "pv_pct": (prix_eur / r["prix_revient_eur"] - 1) * 100, "date_entree": r["date_entree"]})
        cash_p = _cash(conn, nom, p)
        u = conn.execute("SELECT unites FROM benchmark WHERE portefeuille = ? AND poche = ?", (nom, p)).fetchone()
        niveau = _niveau_benchmark_eur(conn, p, jour)
        if u and u[0] is None and niveau:  # référence pas encore disponible à la création : on l'ancre maintenant
            investi = (config.CAPITAL_INITIAL[p] + (conn.execute(
                "SELECT COALESCE(SUM(montant), 0) FROM apports WHERE portefeuille = ? AND poche = ?", (nom, p)).fetchone()[0]))
            conn.execute("UPDATE benchmark SET unites = ? WHERE portefeuille = ? AND poche = ?", (investi / niveau, nom, p))
            u = (investi / niveau,)
        bench = u[0] * niveau if (u and u[0] and niveau) else None
        resultat[p] = {"titres_eur": titres, "cash_eur": cash_p, "total_eur": titres + cash_p,
                       "benchmark_eur": bench, "lignes": lignes}
        if enregistrer:
            conn.execute("INSERT OR REPLACE INTO valorisations VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (nom, jour, p, titres, cash_p, titres + cash_p, bench))
    if enregistrer:
        conn.commit()
    return resultat


def investi_total(conn, nom: str, poche: str) -> float:
    return config.CAPITAL_INITIAL[poche] + conn.execute(
        "SELECT COALESCE(SUM(montant), 0) FROM apports WHERE portefeuille = ? AND poche = ?", (nom, poche)).fetchone()[0]


def expositions_secteurs(valeurs: dict) -> dict[tuple[str, str], float]:
    """Part de chaque secteur dans sa poche (0 à 1)."""
    parts = {}
    for p in POCHES:
        total = valeurs[p]["total_eur"]
        for l in valeurs[p]["lignes"]:
            cle = (p, config.UNIVERS.get(l["ticker"], {}).get("secteur", "?"))
            parts[cle] = parts.get(cle, 0.0) + (l["valeur_eur"] / total if total else 0.0)
    return parts


def alertes_reel(conn, res: pd.DataFrame, jour: str | None = None, garde: dict | None = None) -> list[str]:
    """Ce que les règles et les garde-fous feraient sur TON portefeuille. Tu restes seul juge."""
    jour = jour or date_marche(conn)
    garde = garde or etat_garde_fous(conn, "reel")
    alertes = []
    if garde["niveau"]:
        alertes.append(f"Garde-fou {garde['libelle']} : ton portefeuille est à -{garde['drawdown'] * 100:.1f} % "
                       f"de son plus haut. La règle prévoit : {garde['regle']}.")
    if garde["perte_jour"] >= config.ALERTE_PERTE_JOUR:
        alertes.append(f"Ton portefeuille a perdu {garde['perte_jour'] * 100:.1f} % sur la dernière séance.")
    for t in positions(conn, "reel").index:
        motif, part = motif_vente(conn, "reel", t, res.loc[t] if t in res.index else None, jour, garde["niveau"])
        if motif:
            nom_action = config.UNIVERS.get(t, {}).get("nom", t)
            quoi = f"de vendre {part * 100:.0f} % de la ligne" if part < 1 else "de vendre"
            alertes.append(f"{nom_action} ({t}) : {motif} — la règle prévoit {quoi}, à toi de décider.")
    for t, part, poids in lignes_a_ecreter(conn, "reel", jour):
        q = conn.execute("SELECT quantite FROM positions WHERE portefeuille = 'reel' AND ticker = ?", (t,)).fetchone()[0]
        alertes.append(f"{config.UNIVERS.get(t, {}).get('nom', t)} ({t}) pèse {poids * 100:.0f} % de sa poche : "
                       f"la règle prévoit de la ramener à {config.ECRETAGE_CIBLE * 100:.0f} %, soit vendre environ "
                       f"{math.floor(q * part):g} action(s) sur {q:g}. À toi de décider.")
    for (p, secteur), part in expositions_secteurs(valoriser(conn, "reel", jour, enregistrer=False)).items():
        if part > config.MAX_SECTEUR + 0.02:
            alertes.append(f"{secteur} pèse {part * 100:.0f} % de ton {p} (limite {config.MAX_SECTEUR * 100:.0f} %) : "
                           "évite d'y renforcer.")
    n = _nb_trades_12_mois(conn, "reel", jour)
    if n >= config.BUDGET_TRADES_AN:
        alertes.append(f"Tu as passé {n} ordres en 12 mois (budget {config.BUDGET_TRADES_AN}) : uniquement les ventes de "
                       f"protection et les opportunités à score ≥ {config.SCORE_EXCEPTIONNEL}.")
    elif n >= config.SEUIL_ALERTE_BUDGET:
        alertes.append(f"{n} ordres sur 12 mois (budget {config.BUDGET_TRADES_AN}) : sois sélectif, score ≥ "
                       f"{config.SCORE_FIN_BUDGET} seulement.")
    return alertes


# ------------------------------------------------------------ mise à jour --
def mettre_a_jour(conn, res: pd.DataFrame, ctx: dict) -> dict:
    """Appelé chaque matin par le rapport : exécution, décisions, valorisation."""
    assurer_schema(conn)
    jour = date_marche(conn)
    if not jour:
        return {}
    if not existe(conn, "fictif"):
        crees = initialiser(conn, jour=jour)
        log.info(f"  Portefeuilles créés ({', '.join(crees)}) avec {config.CAPITAL_INITIAL}")
    executions = executer_ordres_en_attente(conn, "fictif")
    # valorisation avant décision : les garde-fous ont besoin de la valeur du jour
    for nom in PORTEFEUILLES:
        valoriser(conn, nom, jour)
    garde = {nom: etat_garde_fous(conn, nom) for nom in PORTEFEUILLES}
    decisions = decider_fictif(conn, res, ctx, jour, garde["fictif"])
    if garde["fictif"]["perte_jour"] >= config.ALERTE_PERTE_JOUR:
        decisions.insert(0, f"Alerte : -{garde['fictif']['perte_jour'] * 100:.1f} % sur la dernière séance")
    sortie = {"jour": jour, "executions": executions, "decisions": decisions, "garde": garde,
              "alertes_reel": alertes_reel(conn, res, jour, garde["reel"]), "valeurs": {}}
    for nom in PORTEFEUILLES:
        sortie["valeurs"][nom] = valoriser(conn, nom, jour)
    sortie["trades_12m"] = _nb_trades_12_mois(conn, "fictif", jour)
    sortie["exceptionnels_12m"] = _nb_exceptionnels_12_mois(conn, "fictif", jour)
    sortie["trades_12m_reel"] = _nb_trades_12_mois(conn, "reel", jour)
    sortie["en_attente"] = conn.execute(
        "SELECT ticker, sens, montant_eur, motif FROM ordres WHERE portefeuille = 'fictif' AND statut = 'en_attente'").fetchall()
    sortie["date_debut"] = conn.execute("SELECT date_debut FROM portefeuilles WHERE nom = 'fictif'").fetchone()[0]

    # montants indicatifs pour TES comptes réels, si tu suis les candidats du jour
    from modules.rapport import SIGNAL_CANDIDAT
    reel = sortie["valeurs"]["reel"]
    niveau_reel = garde["reel"]["niveau"]
    parts = expositions_secteurs(reel)
    sortie["suggestions_reel"] = {}
    for t, r in res[res["signal"] == SIGNAL_CANDIDAT].iterrows():
        if t in positions(conn, "reel").index or niveau_reel >= 2:
            continue
        if niveau_reel == 1 and r["score_combine"] < config.SCORE_VIGILANCE:
            continue
        p = config.poche(t)
        place = (config.MAX_SECTEUR - parts.get((p, config.UNIVERS[t]["secteur"]), 0.0)) * reel[p]["total_eur"]
        montant = min(taille_cible(r["score_combine"], reel[p]["total_eur"]), reel[p]["cash_eur"], max(place, 0.0))
        if niveau_reel == 1:
            montant *= config.FACTEUR_TAILLE_VIGILANCE
        c = r.get("cours")
        if pd.notna(c) and c > 0:
            prix_eur = c / taux_change(conn, config.devise(t), jour)
            sortie["suggestions_reel"][t] = (montant, math.floor(montant / prix_eur), prix_eur)
    return sortie


# ------------------------------------------------------------------ affichage --
def _perf(total: float, investi: float) -> float:
    return (total / investi - 1) * 100 if investi else 0.0


def html_section(conn, s: dict) -> str:
    if not s:
        return ""
    e = html.escape
    blocs = []
    titres = {"fictif": "Portefeuille fictif — géré par l'algo", "reel": "Ton portefeuille réel"}
    for nom in ("fictif", "reel"):
        v = s["valeurs"][nom]
        cartes, lignes = [], []
        for p in POCHES:
            d = v[p]
            inv = investi_total(conn, nom, p)
            perf = _perf(d["total_eur"], inv)
            bench = (f"référence {_perf(d['benchmark_eur'], inv):+.1f} %"
                     if d["benchmark_eur"] else "référence n.d.")
            cartes.append(f"""<div class="indic"><small>{p}</small><b>{d['total_eur']:,.0f} €</b>
              <small>{perf:+.1f} % · {bench}</small><small>cash {d['cash_eur']:,.0f} €</small></div>""")
            for l in d["lignes"]:
                nom_action = config.UNIVERS.get(l["ticker"], {}).get("nom", l["ticker"])
                classe = "pos" if l["pv_pct"] >= 0 else "neg"
                lignes.append(f"<tr><td>{p}</td><td><b>{e(nom_action)}</b> <small>{e(l['ticker'])}</small></td>"
                              f"<td class='num'>{l['quantite']:g}</td><td class='num'>{l['pru_eur']:,.2f} €</td>"
                              f"<td class='num'>{l['cours_eur']:,.2f} €</td><td class='num'>{l['valeur_eur']:,.0f} €</td>"
                              f"<td class='num {classe}'>{l['pv_pct']:+.1f} %</td><td>{e(l['date_entree'])}</td></tr>")
        g = s.get("garde", {}).get(nom)
        if g:
            couleur = ("pos", "", "neg", "neg")[g["niveau"]]
            cartes.append(f"""<div class="indic"><small>Garde-fous</small><b class="{couleur}">{e(g['libelle'])}</b>
              <small>recul depuis le plus haut : -{g['drawdown'] * 100:.1f} %</small>
              <small>seuils -{config.DRAWDOWN_VIGILANCE * 100:.0f} / -{config.DRAWDOWN_PROTECTION * 100:.0f} / -{config.DRAWDOWN_URGENCE * 100:.0f} %</small></div>""")
        total = sum(v[p]["total_eur"] for p in POCHES)
        table = (f"""<div class="tableau"><table><thead><tr><th>Poche</th><th>Action</th><th>Qté</th>
            <th>Prix de revient</th><th>Cours</th><th>Valeur</th><th>+/-</th><th>Entrée</th></tr></thead>
            <tbody>{''.join(lignes)}</tbody></table></div>""" if lignes else
                 '<p class="vide">Aucune position : 100 % cash.</p>')
        extra = ""
        if nom == "fictif":
            items = "".join(f"<li>{e(x)}</li>" for x in s["decisions"]) or "<li>Aucune nouvelle décision aujourd'hui.</li>"
            execs = "".join(f"<li>{e(x)}</li>" for x in s["executions"])
            hors = s.get("exceptionnels_12m", 0)
            extra = (f"<p class='sous-titre'>Démarré le {e(s['date_debut'])} · ordres sur 12 mois : "
                     f"{s['trades_12m']}/{config.BUDGET_TRADES_AN}"
                     + (f" (dont {hors} hors budget)" if hors else "") + "</p>"
                     f"<h3>Décisions de ce matin (exécutées à l'ouverture de la prochaine séance)</h3><ul>{items}</ul>"
                     + (f"<h3>Ordres exécutés depuis le dernier rapport</h3><ul>{execs}</ul>" if execs else ""))
        else:
            al = "".join(f'<div class="alerte">⚠️ {e(a)}</div>' for a in s["alertes_reel"])
            sug = "".join(
                f"<li>{e(config.UNIVERS[t]['nom'])} ({e(config.poche(t))}) : environ {m:,.0f} € "
                f"soit {q} action(s) à ~{px:,.2f} €</li>"
                for t, (m, q, px) in s["suggestions_reel"].items() if q >= 1)
            budget = (f"<p class='sous-titre'>Tes ordres sur 12 mois : {s.get('trades_12m_reel', 0)}/"
                      f"{config.BUDGET_TRADES_AN}</p>")
            extra = budget + al + (f"<h3>Montants indicatifs si tu retiens les candidats du jour "
                                   f"(garde-fous appliqués)</h3><ul>{sug}</ul>" if sug else "")
        blocs.append(f"""<h2>{titres[nom]} <small class="total">{total:,.0f} €</small></h2>
          <section class="contexte">{''.join(cartes)}</section>{extra}{table}""")
    return "\n".join(blocs)


def afficher_console(conn, s: dict) -> None:
    if not s:
        return
    for nom in ("fictif", "reel"):
        v = s["valeurs"][nom]
        g = s.get("garde", {}).get(nom)
        print(f" Portefeuille {nom:<6} : " + " | ".join(
            f"{p} {v[p]['total_eur']:,.0f} € ({_perf(v[p]['total_eur'], investi_total(conn, nom, p)):+.1f} %, "
            f"cash {v[p]['cash_eur']:,.0f} €)" for p in POCHES)
            + (f" | garde-fous {g['libelle']} (-{g['drawdown'] * 100:.1f} %)" if g else ""))
    for d in s["decisions"]:
        print(f"   → {d}")
    for a in s["alertes_reel"]:
        print(f"   ⚠️  {a}")


def resume_texte(conn) -> str:
    """Pour la commande  python main.py portefeuille."""
    assurer_schema(conn)
    jour = date_marche(conn) or date.today().isoformat()
    lignes = []
    for nom in PORTEFEUILLES:
        if not existe(conn, nom):
            return "Portefeuilles non initialisés : lance  python main.py init-portefeuille"
        v = valoriser(conn, nom, jour, enregistrer=False)
        lignes.append(f"\n=== Portefeuille {nom.upper()} (au {jour}) ===")
        g = etat_garde_fous(conn, nom)
        lignes.append(f" Garde-fous : {g['libelle']} (recul depuis le plus haut -{g['drawdown'] * 100:.1f} %) — {g['regle']}"
                      f"   · ordres sur 12 mois : {_nb_trades_12_mois(conn, nom, jour)}/{config.BUDGET_TRADES_AN}")
        for p in POCHES:
            d = v[p]
            inv = investi_total(conn, nom, p)
            bench = f"{_perf(d['benchmark_eur'], inv):+.1f} %" if d["benchmark_eur"] else "n.d."
            lignes.append(f" {p} : {d['total_eur']:>10,.2f} €   investi {inv:,.0f} €   perf {_perf(d['total_eur'], inv):+.1f} %"
                          f"   (référence {bench})   cash {d['cash_eur']:,.2f} €")
            for l in d["lignes"]:
                lignes.append(f"     {l['ticker']:<10} {l['quantite']:>6g} × {l['cours_eur']:>9,.2f} € = "
                              f"{l['valeur_eur']:>10,.2f} €   ({l['pv_pct']:+.1f} %)")
    tx = conn.execute("SELECT date, portefeuille, sens, ticker, quantite, montant_eur, pnl_eur FROM transactions "
                      "ORDER BY id DESC LIMIT 10").fetchall()
    if tx:
        lignes.append("\nDernières transactions :")
        for d, nom, sens, t, q, m, pnl in tx:
            lignes.append(f" {d}  {nom:<6} {sens:<6} {q:>6g} {t:<10} {m:>10,.2f} €" + (f"  ({pnl:+,.2f} €)" if pnl is not None else ""))
    return "\n".join(lignes)
