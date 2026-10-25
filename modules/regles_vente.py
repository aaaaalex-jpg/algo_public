"""
Règles de vente, écrites une seule fois et utilisées à l'identique par :
  - le portefeuille fictif et les alertes du portefeuille réel (modules/portefeuille.py)
  - le backtest (modules/backtest.py)

Pour décider, la règle a besoin de se souvenir, pour chaque ligne détenue :
  - du plus haut atteint depuis l'achat (règles D et E)
  - du nombre de séances d'affilée passées sous la MM200 (règles C, D et E)
  - de la vente partielle déjà faite ou non (règle E, une seule fois par ligne)

Le backtest tient cette mémoire au fil de la simulation ; le portefeuille fictif
la recalcule chaque matin à partir des cours enregistrés depuis la date d'achat
(fonction etat_depuis_historique), avec exactement la même fonction maj_etat.
"""
from dataclasses import dataclass

import pandas as pd

import config


@dataclass
class EtatLigne:
    prix_entree: float         # prix d'achat, en devise de cotation
    plus_haut: float           # plus haut cours de clôture depuis l'achat
    sous_mm200: int = 0        # séances d'affilée sous la MM200 (moins la marge)
    partielle_faite: bool = False


def regle(code: str | None = None) -> dict:
    return config.REGLES_VENTE[code or config.REGLE_VENTE]


def maj_etat(etat: EtatLigne, cours: float, mm200: float, r: dict) -> None:
    """À appeler une fois par séance, avec le cours de clôture du jour."""
    etat.plus_haut = max(etat.plus_haut, cours)
    sous = pd.notna(mm200) and cours < mm200 * (1 - r["marge"])
    etat.sous_mm200 = etat.sous_mm200 + 1 if sous else 0


def decider(etat: EtatLigne, cours: float, perf_eur: float, r: dict, niveau_garde_fou: int = 0,
            tendance_ok: bool = True) -> tuple[str | None, float]:
    """Renvoie (motif, part à vendre) ; (None, 0) s'il faut garder la ligne.

    perf_eur : plus ou moins-value depuis l'achat, en euros (change compris), pour le stop.
    Priorité : stop de protection > règle de tendance > garde-fou Urgence > vente partielle."""
    if perf_eur <= -config.STOP_LIGNE:
        return f"stop de protection ({perf_eur * 100:.1f} % depuis l'achat, seuil -{config.STOP_LIGNE * 100:.0f} %)", 1.0

    gain_max = etat.plus_haut / etat.prix_entree - 1
    if r.get("seuil_gagnant") is not None and gain_max >= r["seuil_gagnant"]:
        recul = 1 - cours / etat.plus_haut
        if recul >= r["recul_gagnant"]:
            return (f"gagnant protégé : -{recul * 100:.0f} % depuis son plus haut "
                    f"(seuil -{r['recul_gagnant'] * 100:.0f} %)"), 1.0
    elif etat.sous_mm200 >= r["confirmation"]:
        duree = f" depuis {etat.sous_mm200} séances" if r["confirmation"] > 1 else ""
        return f"rupture de tendance (plus de {r['marge'] * 100:.0f} % sous la MM200{duree})", 1.0

    if niveau_garde_fou >= 3:
        if perf_eur <= -config.PERTE_LIGNE_URGENCE:
            return f"garde-fou Urgence : ligne à {perf_eur * 100:.1f} % depuis l'achat", 1.0
        if not tendance_ok:
            return "garde-fou Urgence : plus de tendance haussière", 1.0

    if (r.get("partielle_seuil") is not None and not etat.partielle_faite
            and cours / etat.prix_entree - 1 >= r["partielle_seuil"]):
        return (f"prise de bénéfice : vente de {r['partielle_part'] * 100:.0f} % de la ligne "
                f"(+{(cours / etat.prix_entree - 1) * 100:.0f} %)"), r["partielle_part"]
    return None, 0.0


def etat_depuis_historique(closes: pd.Series, date_entree: str, prix_entree: float,
                           r: dict, partielle_faite: bool = False) -> EtatLigne | None:
    """Reconstitue la mémoire d'une ligne à partir des cours enregistrés.

    closes : cours de clôture (index = dates, triées), historique antérieur compris
             pour que la MM200 soit calculable dès l'achat."""
    closes = closes.dropna()
    if closes.empty:
        return None
    mm200 = closes.rolling(200).mean()
    depuis = closes.index >= pd.Timestamp(date_entree)
    if not depuis.any():
        return None
    etat = EtatLigne(prix_entree=prix_entree, plus_haut=prix_entree, partielle_faite=partielle_faite)
    for c, m in zip(closes[depuis], mm200[depuis]):
        maj_etat(etat, float(c), m, r)
    return etat
