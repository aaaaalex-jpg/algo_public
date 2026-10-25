"""
Module 3 : timing technique.

Pour chaque action, regarde si le MOMENT est bon pour entrer.
Règle d'or : n'acheter que sur faiblesse dans une tendance haussière.

Trois composantes (0 à 100) :
  - tendance : le cours est-il au-dessus de sa moyenne 200 jours ?
               la moyenne 50 jours est-elle au-dessus de la 200 ?
               la moyenne 200 jours monte-t-elle ?
  - repli    : y a-t-il une "faiblesse" exploitable ? (RSI modéré,
               léger recul depuis le plus haut, proche de la moyenne 50 jours)
  - momentum : la hausse des 6 derniers mois, comparée aux autres actions

Si la tendance n'est pas haussière, le score est plafonné (règle d'or).
"""
import numpy as np
import pandas as pd

import config


def rsi(close: pd.Series, periode: int = 14) -> pd.Series:
    """RSI de Wilder : < 30 survendu, > 70 suracheté."""
    delta = close.diff()
    hausse = delta.clip(lower=0).ewm(alpha=1 / periode, adjust=False).mean()
    baisse = (-delta.clip(upper=0)).ewm(alpha=1 / periode, adjust=False).mean()
    rs = hausse / baisse.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def _score_rsi(v: float) -> float:
    """100 entre 35 et 50 (repli sain), décroît au-delà (suracheté) et en deçà (chute)."""
    if 35 <= v <= 50:
        return 100.0
    if v > 50:
        return max(0.0, 100 - (v - 50) * 4)   # 0 à partir de RSI 75
    return max(0.0, 100 - (35 - v) * 6)       # 0 en dessous de RSI ~18


def _score_recul(dd: float) -> float:
    """dd = recul depuis le plus haut 52 semaines (négatif, en %).
    Idéal : entre -5 % et -15 %. Trop près du plus haut = pas de faiblesse,
    trop loin = la tendance est peut-être en train de se casser."""
    if -15 <= dd <= -5:
        return 100.0
    if dd > -5:
        return max(0.0, 30 + dd * (-70 / 5))   # 30 au plus haut, 100 à -5 %
    return max(0.0, 100 - (-15 - dd) * (100 / 15))  # 0 à -30 %


def _score_distance_mm50(ecart: float) -> float:
    """ecart = (cours / MM50 - 1) en %. Idéal : entre -3 % et +3 % (sur le support)."""
    a = abs(ecart)
    if a <= 3:
        return 100.0
    return max(0.0, 100 - (a - 3) * 8)


def indicateurs_action(prix: pd.DataFrame) -> dict | None:
    """prix : DataFrame d'une action, colonnes open/high/low/close, trié par date."""
    prix = prix.dropna(subset=["close"])
    if len(prix) < config.NB_JOURS_MIN:
        return None
    c = prix["close"]
    mm20, mm50, mm200 = c.rolling(20).mean(), c.rolling(50).mean(), c.rolling(200).mean()
    r = rsi(c)
    haut_52s = prix["high"].rolling(252, min_periods=200).max()
    bas_60j = prix["low"].rolling(60).min()

    dernier = c.iloc[-1]
    m50, m200 = mm50.iloc[-1], mm200.iloc[-1]
    pente_mm200 = (m200 / mm200.iloc[-21] - 1) * 100
    recul = (dernier / haut_52s.iloc[-1] - 1) * 100
    ecart_mm50 = (dernier / m50 - 1) * 100
    perf_6m = (dernier / c.iloc[-127] - 1) * 100 if len(c) > 127 else np.nan

    au_dessus_mm200 = dernier > m200
    croisement_ok = m50 > m200
    tendance = 40 * au_dessus_mm200 + 30 * croisement_ok + 30 * (pente_mm200 > 0)
    tendance_haussiere = bool(au_dessus_mm200 and croisement_ok)

    repli = (_score_rsi(r.iloc[-1]) + _score_recul(recul) + _score_distance_mm50(ecart_mm50)) / 3

    return {
        "date_cours": prix["date"].iloc[-1].strftime("%Y-%m-%d") if "date" in prix else None,
        "cours": float(dernier),
        "mm20": float(mm20.iloc[-1]),
        "mm50": float(m50),
        "mm200": float(m200),
        "rsi": float(r.iloc[-1]),
        "recul_52s": float(recul),
        "ecart_mm50": float(ecart_mm50),
        "pente_mm200": float(pente_mm200),
        "perf_6m": float(perf_6m),
        "haut_52s": float(haut_52s.iloc[-1]),
        "bas_60j": float(bas_60j.iloc[-1]),
        "tendance": float(tendance),
        "tendance_haussiere": tendance_haussiere,
        "repli": float(repli),
    }


def calculer_scores_techniques(prix_long: pd.DataFrame) -> pd.DataFrame:
    """prix_long : sortie de db.charger_prix (toutes les actions)."""
    lignes = {}
    for t, groupe in prix_long.groupby("ticker"):
        if t not in config.UNIVERS:
            continue
        ind = indicateurs_action(groupe.sort_values("date").reset_index(drop=True))
        if ind is not None:
            lignes[t] = ind
    if not lignes:
        return pd.DataFrame()
    res = pd.DataFrame.from_dict(lignes, orient="index")

    # momentum : la performance 6 mois classée en percentile dans l'univers
    res["momentum"] = (res["perf_6m"].rank(pct=True) * 100).fillna(50)

    p = config.POIDS_TECHNIQUE
    brut = p["tendance"] * res["tendance"] + p["repli"] * res["repli"] + p["momentum"] * res["momentum"]
    res["score_technique"] = np.where(
        res["tendance_haussiere"], brut, np.minimum(brut, config.PLAFOND_SANS_TENDANCE)
    ).round(1)

    # zone d'entrée indicative et niveau d'invalidation technique
    res["entree"] = [
        (f"entre {r.mm50:,.2f} (MM50) et {r.cours:,.2f}" if r.cours >= r.mm50
         else f"autour de {r.cours:,.2f} (support 60 j : {r.bas_60j:,.2f})")
        for r in res.itertuples()
    ]
    res["invalidation"] = [f"clôture sous {r.mm200:,.2f} (MM200)" for r in res.itertuples()]
    for c in ["tendance", "repli", "momentum"]:
        res[c] = res[c].round(1)
    return res
