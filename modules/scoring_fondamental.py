"""
Module 2 : scoring fondamental.

Note chaque action sur 100 en la comparant aux autres actions de l'univers
(classement en percentiles), selon 4 piliers :
  - valorisation : est-elle chère ou bon marché ? (PER, VE/EBITDA, prix/ventes, rendement FCF)
  - qualité      : est-elle rentable ? (ROE, ROA, marges)
  - croissance   : croît-elle ? (chiffre d'affaires, bénéfices)
  - bilan        : est-elle solide ? (endettement, liquidité)
Puis ajoute le bonus sectoriel (tech, semi-conducteurs, énergie, thème IA).

Limite importante : la comparaison se fait entre tous les secteurs. Une banque
et une société de logiciels n'ont pas les mêmes ratios "normaux" ; c'est une
amélioration prévue (classement à l'intérieur de chaque secteur).
"""
import numpy as np
import pandas as pd

import config


def _percentile(serie: pd.Series, sens: str, negatif_pire: bool) -> pd.Series:
    """Classe une série en percentile 0-100 (100 = le meilleur de l'univers)."""
    s = pd.to_numeric(serie, errors="coerce").astype(float)
    masque_negatif = (s <= 0) if negatif_pire else pd.Series(False, index=s.index)
    valide = s.where(~masque_negatif)
    if valide.notna().sum() < 2:
        rang = pd.Series(np.where(valide.notna(), 50.0, np.nan), index=s.index)
    else:
        rang = valide.rank(pct=True, ascending=(sens == "+")) * 100
    rang = rang.where(~masque_negatif, 0.0)  # pertes / fonds propres négatifs -> 0
    return rang


def preparer(fond: pd.DataFrame) -> pd.DataFrame:
    """Ajoute les indicateurs dérivés (ex. rendement du FCF)."""
    f = fond.copy()
    fcf = pd.to_numeric(f.get("freeCashflow"), errors="coerce")
    cap = pd.to_numeric(f.get("marketCap"), errors="coerce")
    f["fcf_yield"] = fcf / cap.where(cap > 0)
    return f


def calculer_scores_fondamentaux(fond: pd.DataFrame) -> pd.DataFrame:
    """fond : DataFrame indexé par ticker (relevé le plus récent de chaque action)."""
    tickers = [t for t in fond.index if t in config.UNIVERS]
    f = preparer(fond.loc[tickers])
    res = pd.DataFrame(index=f.index)

    nb_total, nb_dispo = 0, pd.Series(0, index=f.index)
    for pilier, spec in config.PILIERS.items():
        notes = []
        for indicateur, sens in spec["indicateurs"].items():
            colonne = f[indicateur] if indicateur in f.columns else pd.Series(np.nan, index=f.index)
            p = _percentile(colonne, sens, indicateur in config.NEGATIF_PIRE)
            res[f"p_{indicateur}"] = p
            notes.append(p)
            nb_total += 1
            nb_dispo = nb_dispo + p.notna().astype(int)
        res[pilier] = pd.concat(notes, axis=1).mean(axis=1, skipna=True).fillna(50.0)

    res["completude"] = nb_dispo / nb_total

    brut = sum(res[p] * spec["poids"] for p, spec in config.PILIERS.items())
    # Si les données sont trop incomplètes, on ramène le score vers 50 (neutre)
    facteur = (res["completude"] / config.COMPLETUDE_MIN).clip(upper=1.0)
    brut = 50 + (brut - 50) * facteur

    bonus = pd.Series(0.0, index=f.index)
    for t in f.index:
        info = config.UNIVERS[t]
        bonus[t] = config.BONUS_SECTEURS.get(info["secteur"], 0) + (config.BONUS_IA if info["ia"] else 0)
    res["bonus"] = bonus
    res["score_fondamental"] = (brut + bonus).clip(0, 100).round(1)
    for p in config.PILIERS:
        res[p] = res[p].round(1)

    # on garde les valeurs brutes utiles pour les explications du rapport
    for c in ["forwardPE", "trailingPE", "returnOnEquity", "profitMargins",
              "revenueGrowth", "earningsGrowth", "debtToEquity", "fcf_yield"]:
        if c in f.columns:
            res[f"v_{c}"] = pd.to_numeric(f[c], errors="coerce")
    return res
