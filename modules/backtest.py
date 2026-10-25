"""
Module 7 : backtest walk-forward.

Rejoue les règles de l'algorithme jour par jour sur l'historique (depuis 2011),
exactement comme le portefeuille fictif les applique chaque matin :
  - décision le soir avec les seules données connues ce jour-là,
    exécution à l'ouverture de la séance suivante, avec frais, change et glissement
  - "cash is a position" (aucun achat en contexte défensif)
  - ventes sur rupture de tendance, stop par ligne, garde-fous de recul (drawdown)
  - budget de 50 ordres/an avec dérogations, 30 % maximum par secteur,
    3 nouveaux achats par jour, 12 positions par poche, 10 % maximum par ligne

Walk-forward : plusieurs réglages sont testés (seuil de timing, règle de vente A à E).
Pour chaque année testée, on choisit le réglage qui a le mieux marché sur les
3 années PRÉCÉDENTES, puis on mesure ce qu'il donne sur l'année suivante, qu'il
n'a jamais vue. On enchaîne ces années "hors échantillon" : c'est la seule
performance qui compte.

Limites (affichées dans le rapport) :
  - pas de fondamentaux historiques : le backtest mesure le timing technique,
    la sélection par tendance et la gestion du risque, pas le score fondamental
  - biais du survivant : l'univers est choisi aujourd'hui (Nvidia, Eli Lilly...).
    D'où la comparaison avec "le même univers acheté et conservé", qui isole
    ce qu'apportent vraiment les règles
"""
import bisect
import html
import json
import logging
import math
import shutil
import webbrowser
from datetime import date
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

import config
from modules import db, regles_vente as rv
from modules.portefeuille import SuiviGardeFous, taille_cible
from modules.timing_technique import _score_distance_mm50, _score_recul, _score_rsi, rsi

log = logging.getLogger("algo")

# Réglages testés par le walk-forward (le réglage actuel de config.py en fait partie)
GRILLE = {"candidat_tech": [55, 60, 65, 70], "vente": config.VENTES_TESTEES}
ANNEES_TRAIN = 3
CRITERE_DD_MIN = 0.02     # pour le classement : un recul inférieur à 2 % compte comme 2 %
MONTE_CARLO_TIRAGES = 1000
MONTE_CARLO_BLOC = 21     # tirages par blocs d'un mois, pour garder les périodes agitées groupées


def reglage_actuel() -> dict:
    return {"candidat_tech": config.SEUILS["candidat_tech"], "vente": config.REGLE_VENTE}


def _nom_reglage(p: dict) -> str:
    return f"timing ≥ {p['candidat_tech']}, vente {p['vente']}"


# ------------------------------------------------------------------ données --
def _indicateurs_ticker(df: pd.DataFrame) -> pd.DataFrame:
    """Mêmes calculs que modules/timing_technique.py, mais pour CHAQUE jour de l'historique."""
    df = df.dropna(subset=["close"]).set_index("date").sort_index()
    c = df["close"]
    h = df["high"].fillna(c)
    mm50, mm200 = c.rolling(50).mean(), c.rolling(200).mean()
    r = rsi(c)
    haut = h.rolling(252, min_periods=200).max()
    pente = (mm200 / mm200.shift(20) - 1) * 100
    recul = (c / haut - 1) * 100
    ecart = (c / mm50 - 1) * 100
    perf6 = (c / c.shift(126) - 1) * 100
    th = (c > mm200) & (mm50 > mm200)
    tendance = 40.0 * (c > mm200) + 30.0 * (mm50 > mm200) + 30.0 * (pente > 0)
    repli = (r.map(_score_rsi) + recul.map(_score_recul) + ecart.map(_score_distance_mm50)) / 3
    out = pd.DataFrame({"open": df["open"], "close": c, "mm200": mm200, "tendance": tendance,
                        "th": th.astype(float), "repli": repli, "perf6": perf6})
    trop_court = np.arange(1, len(df) + 1) < config.NB_JOURS_MIN
    out.loc[trop_court, ["tendance", "th", "repli", "perf6"]] = np.nan
    return out


def preparer_donnees(conn) -> dict:
    """Charge l'historique et calcule les indicateurs de chaque jour (une seule fois)."""
    prix = db.charger_prix(conn, tickers=list(config.UNIVERS))
    if prix.empty:
        raise RuntimeError("Pas de cours en base : lance d'abord  python main.py collecte")
    ind = {t: _indicateurs_ticker(g) for t, g in prix.groupby("ticker") if t in config.UNIVERS}
    ind = {t: v for t, v in ind.items() if len(v) >= config.NB_JOURS_MIN}
    tickers = [t for t in config.UNIVERS if t in ind]
    cal = pd.DatetimeIndex(sorted(set().union(*[v.index for v in ind.values()])))

    def large(champ, remplir=True):
        w = pd.concat({t: ind[t][champ] for t in tickers}, axis=1).reindex(cal)
        return w.ffill(limit=10) if remplir else w

    tendance, repli, perf6, th = large("tendance"), large("repli"), large("perf6"), large("th")
    momentum = (perf6.rank(axis=1, pct=True) * 100).fillna(50)
    brut = (config.POIDS_TECHNIQUE["tendance"] * tendance + config.POIDS_TECHNIQUE["repli"] * repli
            + config.POIDS_TECHNIQUE["momentum"] * momentum)
    score = brut.where(th == 1, np.minimum(brut, config.PLAFOND_SANS_TENDANCE)).round(1)
    valide = tendance.notna() & repli.notna()

    def macro(nom):
        s = db.charger_macro(conn, nom)
        return s if not s.empty else None

    def sur_cal(s, defaut=np.nan):
        return (s.reindex(cal.union(s.index)).ffill().reindex(cal) if s is not None
                else pd.Series(defaut, index=cal)).astype(float)

    # contexte de marché : même règle que le rapport du matin (seul "Défensif" bloque les achats)
    au_dessus = []
    for nom in ("SP500", "STOXX50"):
        s = macro(nom)
        if s is not None and len(s) >= 200:
            au_dessus.append(sur_cal((s > s.rolling(200).mean()).astype(float).where(s.rolling(200).mean().notna())))
    vix = sur_cal(macro("VIX"))
    if au_dessus:
        aucun_au_dessus = pd.concat(au_dessus, axis=1).fillna(1).eq(0).all(axis=1)
    else:
        aucun_au_dessus = pd.Series(False, index=cal)
    defensif = (aucun_au_dessus | (vix >= config.VIX_STRESS)).to_numpy()

    fx = {"EUR": np.ones(len(cal))}
    for dev, serie in (("USD", "EURUSD"), ("DKK", "EURDKK")):
        fx[dev] = sur_cal(macro(serie)).fillna(config.TAUX_DEFAUT[dev]).to_numpy()

    references = {}
    for p, (serie, dev, secours) in config.BENCHMARK.items():
        s = macro(serie)
        if s is None or s.index[0] > cal[0] + pd.Timedelta(days=400):
            s = macro(secours)  # l'ETF n'existe pas assez loin dans le passé : indice sans dividendes
        if s is not None:
            references[p] = sur_cal(s).to_numpy() / fx[dev]

    return {
        "dates": cal, "tickers": tickers,
        "open": large("open", remplir=False).to_numpy(), "close": large("close").to_numpy(),
        "cote": large("close", remplir=False).notna().to_numpy(),
        "mm200": large("mm200").to_numpy(), "th": (th == 1).to_numpy(), "score": score.to_numpy(),
        "valide": valide.to_numpy(), "defensif": defensif, "fx": fx, "references": references,
        "poche": [config.poche(t) for t in tickers], "devise": [config.devise(t) for t in tickers],
        "secteur": [config.UNIVERS[t]["secteur"] for t in tickers],
    }


def debut_par_defaut(d: dict) -> pd.Timestamp:
    """Premier jour où au moins la moitié des actions ont assez d'historique."""
    part = d["valide"].mean(axis=1)
    i = int(np.argmax(part >= 0.5)) if (part >= 0.5).any() else 0
    return d["dates"][i]


# --------------------------------------------------------------- simulation --
def simuler(d: dict, reglage: dict, debut, fin=None, apports: dict | None = None) -> dict:
    """Rejoue les règles du portefeuille fictif entre deux dates. Renvoie la courbe et les ordres."""
    dates = d["dates"]
    i0 = int(dates.searchsorted(pd.Timestamp(debut)))
    i1 = int(dates.searchsorted(pd.Timestamp(fin), side="right")) if fin else len(dates)
    apports = apports or {}
    poche, devise, secteur = d["poche"], d["devise"], d["secteur"]
    ouv, clo, mm200, th, score, valide = d["open"], d["close"], d["mm200"], d["th"], d["score"], d["valide"]
    fx = d["fx"]
    seuil_tech, r_vente = reglage["candidat_tech"], rv.regle(reglage["vente"])
    cote = d["cote"]

    cash = {p: float(v) for p, v in config.CAPITAL_INITIAL.items()}
    pos: dict[int, list] = {}          # j -> [quantité, prix de revient €, poche]
    etats: dict[int, rv.EtatLigne] = {}  # mémoire de chaque ligne pour la règle de vente
    attente: list[dict] = []
    dates_ordres: list[np.datetime64] = []
    ordres: list[dict] = []
    courbe = []
    precedent = None
    indice = 1.0
    suivi = SuiviGardeFous()
    mois = None

    def nb_ordres_12m(i):
        limite = dates[i] - pd.Timedelta(days=365)
        return len(dates_ordres) - bisect.bisect_right(dates_ordres, limite) + len(attente)

    for i in range(i0, i1):
        jour = dates[i]
        # 1. exécution des ordres décidés les jours précédents (ventes d'abord)
        reste = []
        for o in sorted(attente, key=lambda o: o["sens"] != "vente"):
            j = o["j"]
            p, taux = poche[j], fx[devise[j]][i]
            prix_ouv = ouv[i, j]
            if not (prix_ouv > 0):
                if i - o["i"] <= 10:
                    reste.append(o)
                continue
            frais = config.FRAIS_COURTAGE[p] + (config.FRAIS_CHANGE if devise[j] != "EUR" else 0)
            if o["sens"] == "achat":
                px = prix_ouv * (1 + config.GLISSEMENT) / taux
                q = math.floor(min(o["montant"], cash[p]) / (px * (1 + frais)))
                if q < 1:
                    continue
                cout = q * px * (1 + frais)
                cash[p] -= cout
                pos[j] = [q, cout / q, p]
                etats[j] = rv.EtatLigne(prix_entree=prix_ouv * (1 + config.GLISSEMENT),
                                        plus_haut=prix_ouv * (1 + config.GLISSEMENT))
                pnl = None
            else:
                if j not in pos:
                    continue
                q, pru, _ = pos[j]
                qv = q if o.get("part", 1.0) >= 1 else math.floor(q * o["part"])
                if qv < 1:
                    if o["motif"].startswith("prise"):
                        etats[j].partielle_faite = True
                    continue
                net = qv * prix_ouv * (1 - config.GLISSEMENT) / taux * (1 - frais)
                cash[p] += net
                pnl = net - qv * pru
                if qv >= q:
                    pos.pop(j)
                    etats.pop(j, None)
                else:
                    pos[j][0] = q - qv
                    if o["motif"].startswith("prise"):
                        etats[j].partielle_faite = True
            dates_ordres.append(jour)
            ordres.append({"date": jour, "decision": dates[o["i"]], "ticker": d["tickers"][j], "poche": p,
                           "sens": o["sens"], "motif": o["motif"], "hors_budget": o["hors"], "pnl": pnl,
                           "prix": prix_ouv * (1 + config.GLISSEMENT if o["sens"] == "achat" else 1 - config.GLISSEMENT),
                           "taux": taux, "frais": frais})
        attente = reste

        # 2. apports mensuels (premier jour de chaque mois)
        flux = 0.0
        if apports and mois is not None and jour.month != mois:
            for p, m in apports.items():
                cash[p] += m
                flux += m
        mois = jour.month

        # 3. valorisation au cours de clôture
        titres = {p: 0.0 for p in cash}
        for j, (q, _, p) in pos.items():
            titres[p] += q * clo[i, j] / fx[devise[j]][i]
        total = sum(titres.values()) + sum(cash.values())
        r = (total - flux) / precedent - 1 if precedent else 0.0
        precedent = total
        indice *= 1 + r
        niveau, recul, _ = suivi.maj(indice)
        courbe.append((jour, total, flux, r, sum(titres.values()) / total if total else 0.0, niveau))
        if i == i1 - 1:
            break

        # 4. décisions du soir (exécutées demain)
        en_attente = {o["j"] for o in attente}
        for j, (q, pru, p) in list(pos.items()):
            c = clo[i, j]
            if cote[i, j]:  # mémoire mise à jour uniquement les jours où l'action a coté
                rv.maj_etat(etats[j], c, mm200[i, j], r_vente)
            if j in en_attente:
                continue
            perf = c / fx[devise[j]][i] / pru - 1
            motif, part = rv.decider(etats[j], c, perf, r_vente, niveau, bool(th[i, j]))
            if motif:
                hors = nb_ordres_12m(i) >= config.BUDGET_TRADES_AN
                if hors and part < 1:
                    continue  # une prise de bénéfice partielle n'est pas une urgence : elle respecte le budget
                attente.append({"j": j, "sens": "vente", "montant": None, "motif": motif, "i": i,
                                "hors": hors, "part": part})
                en_attente.add(j)

        # écrêtage : une ligne devenue trop grosse est ramenée à ECRETAGE_CIBLE de sa poche
        seuil_e, cible_e = reglage.get("ecretage", (config.ECRETAGE_SEUIL, config.ECRETAGE_CIBLE))
        if seuil_e:
            for j, (q, _, p) in list(pos.items()):
                tot = titres[p] + cash[p]
                v = q * clo[i, j] / fx[devise[j]][i]
                if j in en_attente or tot <= 0 or v / tot <= seuil_e or v - cible_e * tot < config.MONTANT_MIN_ORDRE:
                    continue
                attente.append({"j": j, "sens": "vente", "montant": None, "i": i, "part": 1 - cible_e * tot / v,
                                "motif": f"écrêtage : ligne à {v / tot * 100:.0f} % de la poche, ramenée à {cible_e * 100:.0f} %",
                                "hors": nb_ordres_12m(i) >= config.BUDGET_TRADES_AN})
                en_attente.add(j)

        if d["defensif"][i] or niveau >= 2:
            continue
        achats_attente = [o for o in attente if o["sens"] == "achat"]
        total_p = {p: titres[p] + cash[p] for p in cash}
        cash_libre = {p: cash[p] - sum(o["montant"] for o in achats_attente if poche[o["j"]] == p) for p in cash}
        nb_pos = {p: sum(1 for v in pos.values() if v[2] == p) + sum(1 for o in achats_attente if poche[o["j"]] == p)
                  for p in cash}
        expo: dict[tuple, float] = {}
        for j, (q, _, p) in pos.items():
            expo[(p, secteur[j])] = expo.get((p, secteur[j]), 0.0) + q * clo[i, j] / fx[devise[j]][i]
        for o in achats_attente:
            cle = (poche[o["j"]], secteur[o["j"]])
            expo[cle] = expo.get(cle, 0.0) + o["montant"]

        ligne_score = score[i]
        candidats = np.where(valide[i] & th[i] & (ligne_score >= seuil_tech))[0]
        candidats = candidats[np.argsort(-ligne_score[candidats], kind="stable")]
        nb_achats = 0
        for j in candidats:
            if j in pos or j in en_attente:
                continue
            if nb_achats >= config.MAX_ACHATS_PAR_JOUR:
                break
            s, p = float(ligne_score[j]), poche[j]
            n = nb_ordres_12m(i)
            hors = False
            if n >= config.BUDGET_TRADES_AN:
                if s >= config.SCORE_EXCEPTIONNEL and n < config.BUDGET_TRADES_AN + config.RESERVE_EXCEPTIONNELLE:
                    hors = True
                else:
                    continue
            elif n >= config.SEUIL_ALERTE_BUDGET and s < config.SCORE_FIN_BUDGET:
                continue
            if niveau == 1 and s < config.SCORE_VIGILANCE:
                continue
            if nb_pos[p] >= config.MAX_POSITIONS_PAR_POCHE:
                continue
            montant = taille_cible(s, total_p[p]) * (config.FACTEUR_TAILLE_VIGILANCE if niveau == 1 else 1)
            montant = min(montant, cash_libre[p], config.MAX_SECTEUR * total_p[p] - expo.get((p, secteur[j]), 0.0))
            if montant < config.MONTANT_MIN_ORDRE:
                continue
            attente.append({"j": j, "sens": "achat", "montant": montant, "motif": f"score {s:.0f}", "i": i, "hors": hors})
            en_attente.add(j)
            cash_libre[p] -= montant
            nb_pos[p] += 1
            expo[(p, secteur[j])] = expo.get((p, secteur[j]), 0.0) + montant
            nb_achats += 1

    courbe = pd.DataFrame(courbe, columns=["date", "total", "flux", "rendement", "investi", "niveau"]).set_index("date")
    return {"courbe": courbe, "ordres": pd.DataFrame(ordres), "reglage": reglage}


# ------------------------------------------------------------------- mesures --
def mesures(rendements: pd.Series) -> dict:
    r = rendements.dropna()
    if len(r) < 2:
        return {}
    annees = max((r.index[-1] - r.index[0]).days / 365.25, 1 / 365.25)
    par_an = len(r) / annees
    indice = (1 + r).cumprod()
    cagr = indice.iloc[-1] ** (1 / annees) - 1
    vol = r.std() * math.sqrt(par_an)
    recul_max = float((1 - indice / indice.cummax()).max())
    return {"rendement_annuel": float(cagr), "rendement_total": float(indice.iloc[-1] - 1),
            "volatilite": float(vol), "sharpe": float(r.mean() * par_an / vol) if vol > 0 else 0.0,
            "recul_max": recul_max, "calmar": float(cagr / max(recul_max, 1e-9)),
            "debut": r.index[0], "fin": r.index[-1]}


def stats_ordres(ordres: pd.DataFrame, debut, fin) -> dict:
    if ordres.empty:
        return {"ordres_par_an": 0.0, "taux_gain": None, "profit_factor": None, "hors_budget": 0, "nb": 0}
    o = ordres[(ordres["date"] >= pd.Timestamp(debut)) & (ordres["date"] <= pd.Timestamp(fin))]
    annees = max((pd.Timestamp(fin) - pd.Timestamp(debut)).days / 365.25, 1 / 365.25)
    ventes = o[o["sens"] == "vente"]["pnl"].dropna()
    gains, pertes = ventes[ventes > 0].sum(), -ventes[ventes < 0].sum()
    return {"nb": len(o), "ordres_par_an": len(o) / annees,
            "taux_gain": float((ventes > 0).mean()) if len(ventes) else None,
            "profit_factor": float(gains / pertes) if pertes > 0 else None,
            "hors_budget": int(o["hors_budget"].sum()) if len(o) else 0,
            "motifs_vente": o[o["sens"] == "vente"]["motif"].value_counts().to_dict()}


def rendements_reference(d: dict, debut, fin) -> pd.Series:
    """ETF de référence de chaque poche (S&P 500 / Euro Stoxx 50), pondérés par le capital de départ."""
    dates = d["dates"]
    m = (dates >= pd.Timestamp(debut)) & (dates <= pd.Timestamp(fin))
    valeur = 0
    for p, serie in d["references"].items():
        s = pd.Series(serie[m], index=dates[m]).ffill().bfill()
        valeur = valeur + config.CAPITAL_INITIAL[p] * s / s.iloc[0]
    return valeur.pct_change().fillna(0.0) if isinstance(valeur, pd.Series) else pd.Series(dtype=float)


def rendements_univers(d: dict, debut, fin) -> pd.Series:
    """Le même univers acheté le premier jour, à parts égales dans chaque poche, puis conservé."""
    dates = d["dates"]
    m = (dates >= pd.Timestamp(debut)) & (dates <= pd.Timestamp(fin))
    i0 = int(np.argmax(m))
    valeur = pd.Series(0.0, index=dates[m])
    for p in config.CAPITAL_INITIAL:
        js = [j for j in range(len(d["tickers"])) if d["poche"][j] == p and d["close"][i0, j] > 0]
        for j in js:
            eur = pd.Series(d["close"][m, j] / d["fx"][d["devise"][j]][m], index=dates[m]).ffill()
            valeur += config.CAPITAL_INITIAL[p] / len(js) * eur / eur.iloc[0]
    return valeur.pct_change().fillna(0.0)


# ------------------------------------------------------------- walk-forward --
def lancer_reglages(d: dict, debut, apports: dict | None = None) -> dict:
    """Simule chaque réglage de la grille une fois sur toute la période."""
    reglages = [dict(zip(GRILLE, v)) for v in product(*GRILLE.values())]
    if reglage_actuel() not in reglages:
        reglages.append(reglage_actuel())
    runs = {}
    for k, rg in enumerate(reglages, 1):
        log.info(f"  [{k}/{len(reglages)}] simulation : {_nom_reglage(rg)}")
        runs[_nom_reglage(rg)] = {**simuler(d, rg, debut, apports=apports), "reglage": rg}
    return runs


def walk_forward(d: dict, debut, apports: dict | None = None, runs: dict | None = None, ventes=None) -> dict:
    """Enchaîne les années hors échantillon. ventes : règles de vente autorisées (None = toutes)."""
    tous = runs if runs is not None else lancer_reglages(d, debut, apports)
    runs = {n: r for n, r in tous.items() if ventes is None or r["reglage"]["vente"] in ventes}

    premiere = pd.Timestamp(debut).year
    derniere = d["dates"][-1].year
    morceaux, annees = [], []
    for annee in range(premiere + ANNEES_TRAIN, derniere + 1):
        def fenetre(run, a0, a1):
            r = run["courbe"]["rendement"]
            return r[(r.index.year >= a0) & (r.index.year <= a1)]
        classement = {}
        for nom, run in runs.items():
            m = mesures(fenetre(run, annee - ANNEES_TRAIN, annee - 1))
            classement[nom] = m["rendement_annuel"] / max(m["recul_max"], CRITERE_DD_MIN) if m else -np.inf
        choisi = max(classement, key=classement.get)
        test = fenetre(runs[choisi], annee, annee)
        if test.empty:
            continue
        morceaux.append(test)
        o = runs[choisi]["ordres"]
        nb = 0 if o.empty else int((o["date"].dt.year == annee).sum())
        annees.append({"annee": annee, "reglage": choisi, "rendement": float((1 + test).prod() - 1),
                       "recul_max": mesures(test).get("recul_max", 0.0) if len(test) > 1 else 0.0, "ordres": nb,
                       "investi": float(runs[choisi]["courbe"]["investi"][test.index].mean())})
    if not morceaux:
        raise RuntimeError("Historique trop court pour un walk-forward (il faut au moins 4 ans de cours).")
    oos = pd.concat(morceaux).copy()
    oos.iloc[0] = 0.0  # le premier jour hors échantillon sert de point de départ
    ordres_oos = pd.concat([runs[a["reglage"]]["ordres"].pipe(
        lambda o, a=a: o[o["date"].dt.year == a["annee"]] if not o.empty else o) for a in annees], ignore_index=True)
    return {"runs": tous, "oos": oos, "annees": annees, "ordres_oos": ordres_oos}


def monte_carlo(rendements: pd.Series, tirages: int = MONTE_CARLO_TIRAGES, graine: int = 7) -> dict:
    """Rejoue 1 000 années possibles en piochant des mois réels au hasard (tirage par blocs)."""
    r = rendements.dropna().to_numpy()
    if len(r) < MONTE_CARLO_BLOC * 3:
        return {}
    par_an = int(round(len(r) / max((rendements.index[-1] - rendements.index[0]).days / 365.25, 1e-9)))
    rng = np.random.default_rng(graine)
    perfs, reculs = [], []
    for _ in range(tirages):
        blocs = []
        while sum(len(b) for b in blocs) < par_an:
            k = rng.integers(0, len(r) - MONTE_CARLO_BLOC)
            blocs.append(r[k:k + MONTE_CARLO_BLOC])
        x = np.concatenate(blocs)[:par_an]
        idx = np.cumprod(1 + x)
        perfs.append(idx[-1] - 1)
        reculs.append(float((1 - idx / np.maximum.accumulate(idx)).max()))
    perfs, reculs = np.array(perfs), np.array(reculs)
    return {"perf_p5": float(np.percentile(perfs, 5)), "perf_p50": float(np.percentile(perfs, 50)),
            "perf_p95": float(np.percentile(perfs, 95)), "proba_perte": float((perfs < 0).mean()),
            "proba_objectif": float((perfs >= 0.15).mean()), "recul_p50": float(np.percentile(reculs, 50)),
            "recul_p95": float(np.percentile(reculs, 95)), "proba_recul_10": float((reculs >= 0.10).mean())}


# ------------------------------------------------------------------ rapport --
def executer(conn, debut=None, apport_cto: float = 0.0, apport_pea: float = 0.0,
             ouvrir: bool = False, dossier: Path | None = None) -> Path:
    log.info("▶ Préparation de l'historique et des indicateurs")
    d = preparer_donnees(conn)
    debut = pd.Timestamp(debut) if debut else debut_par_defaut(d)
    apports = {p: m for p, m in (("CTO", apport_cto), ("PEA", apport_pea)) if m}
    log.info(f"  {len(d['tickers'])} actions, du {debut.date()} au {d['dates'][-1].date()}")
    log.info("▶ Walk-forward")
    runs = lancer_reglages(d, debut, apports)
    wf = walk_forward(d, debut, runs=runs)
    oos = wf["oos"]
    a0, a1 = oos.index[0], oos.index[-1]
    actuel = runs[_nom_reglage(reglage_actuel())]
    lignes = {"Algo — walk-forward (hors échantillon)": (oos, stats_ordres(wf["ordres_oos"], a0, a1))}
    if list(config.VENTES_TESTEES) != [config.REGLE_VENTE]:
        wf_ref = walk_forward(d, debut, runs=runs, ventes=[config.REGLE_VENTE])
        lignes[f"Algo — walk-forward, vente {config.REGLE_VENTE} seulement"] = (
            wf_ref["oos"][a0:a1], stats_ordres(wf_ref["ordres_oos"], a0, a1))
    lignes |= {
        "Algo — réglage actuel de config.py": (actuel["courbe"]["rendement"][a0:a1],
                                                stats_ordres(actuel["ordres"], a0, a1)),
        "ETF de référence (S&P 500 + Euro Stoxx 50)": (rendements_reference(d, a0, a1), None),
        "Même univers acheté et conservé": (rendements_univers(d, a0, a1), None),
    }
    mc = monte_carlo(oos)
    resultats = {nom: {**mesures(r), "ordres": st} for nom, (r, st) in lignes.items()}

    afficher_console(resultats, wf, mc)
    dossier = Path(dossier or config.RAPPORTS_DIR)
    chemin = dossier / f"backtest_{date.today().isoformat()}.html"
    ecrire_html(chemin, resultats, {nom: r for nom, (r, _) in lignes.items()}, wf, mc, d)
    shutil.copyfile(chemin, dossier / "dernier_backtest.html")
    db.journaliser(conn, "backtest", "ok", str(chemin))
    log.info(f"Rapport de backtest : {chemin}")
    if ouvrir:
        webbrowser.open(chemin.resolve().as_uri())
    return chemin


def _p(v, signe=True) -> str:
    return "n.d." if v is None or (isinstance(v, float) and math.isnan(v)) else (f"{v * 100:+.1f} %" if signe else f"{v * 100:.1f} %")


def verdicts(resultats: dict) -> list[tuple[str, bool, str]]:
    algo = resultats["Algo — walk-forward (hors échantillon)"]
    ref = resultats["ETF de référence (S&P 500 + Euro Stoxx 50)"]
    univ = resultats["Même univers acheté et conservé"]
    v = [
        ("Rendement ≥ 15 %/an", algo["rendement_annuel"] >= 0.15, _p(algo["rendement_annuel"])),
        (f"Recul maximal < {config.DRAWDOWN_URGENCE * 100:.0f} %", algo["recul_max"] < config.DRAWDOWN_URGENCE,
         _p(-algo["recul_max"])),
        ("Sharpe > 1,2", algo["sharpe"] > 1.2, f"{algo['sharpe']:.2f}"),
    ]
    if "rendement_annuel" in ref:
        v.append(("Bat les ETF de référence", algo["rendement_annuel"] > ref["rendement_annuel"],
                  f"{_p(algo['rendement_annuel'])} contre {_p(ref['rendement_annuel'])}"))
    if "calmar" in univ:
        v.append(("Mieux que garder le même univers (rendement/risque)", algo["calmar"] > univ["calmar"],
                  f"Calmar {algo['calmar']:.2f} contre {univ['calmar']:.2f}"))
    return v


def afficher_console(resultats: dict, wf: dict, mc: dict) -> None:
    print("\n" + "=" * 96)
    print(" BACKTEST WALK-FORWARD")
    print("=" * 96)
    print(f" {'':<46}{'Rend./an':>9}{'Recul max':>11}{'Sharpe':>8}{'Calmar':>8}{'Ordres/an':>11}")
    for nom, m in resultats.items():
        if "rendement_annuel" not in m:
            continue
        o = m.get("ordres")
        nb = "{:.0f}".format(o["ordres_par_an"]) if o else "-"
        print(f" {nom[:45]:<46}{_p(m['rendement_annuel']):>9}{_p(-m['recul_max']):>11}{m['sharpe']:>8.2f}"
              f"{m['calmar']:>8.2f}{nb:>11}")
    print("\n Par année (hors échantillon) :")
    for a in wf["annees"]:
        print(f"   {a['annee']}  {_p(a['rendement']):>8}  recul max {_p(-a['recul_max']):>8}  {a['ordres']:>3} ordres  "
              f"investi {a['investi'] * 100:>3.0f} %  ({a['reglage']})")
    if mc:
        print(f"\n Monte Carlo (1 000 années simulées) : rendement médian {_p(mc['perf_p50'])}, "
              f"1 année sur 20 pire que {_p(mc['perf_p5'])}, probabilité d'une année négative {mc['proba_perte'] * 100:.0f} %")
    print("\n Objectifs :")
    for nom, ok, val in verdicts(resultats):
        print(f"   {'✅' if ok else '❌'} {nom} — {val}")
    print("\n ⚠️  Sans fondamentaux historiques et avec un univers choisi aujourd'hui : lis les limites dans le rapport.\n")


def _graphique(series: dict[str, pd.Series]) -> str:
    """Courbe "100 € investis" en SVG, avec infobulle au survol."""
    noms = list(series)
    df = pd.concat({n: (1 + s).cumprod() * 100 for n, s in series.items()}, axis=1).ffill().dropna()
    df = df.resample("W-FRI").last().dropna()
    if df.empty:
        return ""
    L, H, g, dr, h, b = 1000, 380, 56, 170, 16, 34
    x0, x1, y0, y1 = g, L - dr, h, H - b
    vmin, vmax = float(df.min().min()), float(df.max().max())
    pas = next((x for x in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000) if (vmax - vmin) / x <= 7), 10000)
    lo, hi = math.floor(vmin / pas) * pas, math.ceil(vmax / pas) * pas
    n = len(df)
    X = lambda k: x0 + (x1 - x0) * k / max(n - 1, 1)
    Y = lambda v: y1 - (y1 - y0) * (v - lo) / (hi - lo)
    grille = "".join(
        f'<line x1="{x0}" x2="{x1}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="grille"/>'
        f'<text x="{x0 - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" class="axe">{f"{v:,.0f}".replace(",", " ")}</text>'
        for v in np.arange(lo, hi + pas / 2, pas))
    annees = "".join(
        f'<text x="{X(k):.1f}" y="{H - 12}" text-anchor="middle" class="axe">{d.year}</text>'
        for k, d in enumerate(df.index) if k == 0 or d.year != df.index[k - 1].year
        if (d.year % (1 if n < 300 else 2) == 0))
    traits, etiquettes = [], []
    fins = sorted(((Y(df[nm].iloc[-1]), nm) for nm in noms), key=lambda t: t[0])
    pos_lab, dernier_y = {}, -1e9
    for y, nm in fins:
        y = max(y, dernier_y + 15)
        pos_lab[nm], dernier_y = y, y
    for k, nm in enumerate(noms, 1):
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(df[nm]))
        traits.append(f'<polyline points="{pts}" class="serie s{k}"/>')
        court = nm.split(" (")[0].replace("Même univers conservé", "Univers conservé").replace("ETF de référence", "ETF")
        valeur = f"{df[nm].iloc[-1]:,.0f}".replace(",", " ")
        etiquettes.append(f'<text x="{x1 + 8}" y="{pos_lab[nm] + 4:.1f}" class="lab">{valeur} · {html.escape(court)}</text>')
    donnees = {"dates": [d.strftime("%d/%m/%Y") for d in df.index], "noms": noms,
               "valeurs": [[round(float(v), 1) for v in df[nm]] for nm in noms],
               "x0": x0, "x1": x1, "y0": y0, "y1": y1, "lo": lo, "hi": hi}
    legende = "".join(f'<span><i class="s{k}"></i>{html.escape(nm)}</span>' for k, nm in enumerate(noms, 1))
    return f"""
<div class="legende">{legende}</div>
<div class="graph" id="graph">
<svg viewBox="0 0 {L} {H}" role="img" aria-label="Valeur de 100 € investis au départ, chaque semaine">
{grille}{annees}{''.join(traits)}{''.join(etiquettes)}
<line id="viseur" y1="{y0}" y2="{y1}" class="viseur" visibility="hidden"/>
{''.join(f'<circle id="pt{k}" r="4" class="pt s{k}" visibility="hidden"/>' for k in range(1, len(noms) + 1))}
<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="transparent" id="zone"/>
</svg><div id="bulle" class="bulle" hidden></div></div>
<script>
(() => {{
  const D = {json.dumps(donnees)}, svg = document.querySelector('#graph svg'), bulle = document.getElementById('bulle');
  const n = D.dates.length, X = k => D.x0 + (D.x1 - D.x0) * k / Math.max(n - 1, 1),
        Y = v => D.y1 - (D.y1 - D.y0) * (v - D.lo) / (D.hi - D.lo);
  const montrer = ev => {{
    const r = svg.getBoundingClientRect(), x = (ev.clientX - r.left) * 1000 / r.width;
    const k = Math.max(0, Math.min(n - 1, Math.round((x - D.x0) / (D.x1 - D.x0) * (n - 1))));
    const v = document.getElementById('viseur'); v.setAttribute('x1', X(k)); v.setAttribute('x2', X(k)); v.setAttribute('visibility', 'visible');
    D.noms.forEach((nm, i) => {{ const c = document.getElementById('pt' + (i + 1));
      c.setAttribute('cx', X(k)); c.setAttribute('cy', Y(D.valeurs[i][k])); c.setAttribute('visibility', 'visible'); }});
    bulle.innerHTML = '<b>' + D.dates[k] + '</b>' + D.noms.map((nm, i) =>
      '<div><i class="s' + (i + 1) + '"></i>' + nm + ' <b>' + D.valeurs[i][k].toLocaleString('fr-FR') + ' €</b></div>').join('');
    bulle.hidden = false;
    const px = X(k) / 1000 * r.width; bulle.style.left = (px > r.width / 2 ? px - bulle.offsetWidth - 12 : px + 12) + 'px';
  }};
  const cacher = () => {{ bulle.hidden = true; document.getElementById('viseur').setAttribute('visibility', 'hidden');
    D.noms.forEach((_, i) => document.getElementById('pt' + (i + 1)).setAttribute('visibility', 'hidden')); }};
  const zone = document.getElementById('zone');
  zone.addEventListener('pointermove', montrer); zone.addEventListener('pointerleave', cacher);
}})();
</script>"""


def ecrire_html(chemin: Path, resultats: dict, series: dict, wf: dict, mc: dict, d: dict) -> None:
    e = html.escape
    lignes = []
    for nom, m in resultats.items():
        if "rendement_annuel" not in m:
            continue
        o = m.get("ordres") or {}
        lignes.append(f"""<tr><td><b>{e(nom)}</b></td><td class="num">{_p(m['rendement_annuel'])}</td>
          <td class="num">{_p(m['rendement_total'])}</td><td class="num">{_p(-m['recul_max'])}</td>
          <td class="num">{_p(m['volatilite'], False)}</td><td class="num">{m['sharpe']:.2f}</td><td class="num">{m['calmar']:.2f}</td>
          <td class="num">{(f"{o['ordres_par_an']:.0f}" if o else '–')}</td>
          <td class="num">{(_p(o['taux_gain'], False) if o and o.get('taux_gain') is not None else '–')}</td>
          <td class="num">{(f"{o['profit_factor']:.2f}" if o and o.get('profit_factor') else '–')}</td></tr>""")
    ref = series["ETF de référence (S&P 500 + Euro Stoxx 50)"]
    univ = series["Même univers acheté et conservé"]

    def annee_de(s, a):
        x = s[s.index.year == a]
        return (1 + x).prod() - 1 if len(x) else None

    lignes_annees = "".join(
        f"""<tr><td>{a['annee']}</td><td class="num {'pos' if a['rendement'] >= 0 else 'neg'}">{_p(a['rendement'])}</td>
        <td class="num">{_p(annee_de(ref, a['annee']))}</td><td class="num">{_p(annee_de(univ, a['annee']))}</td>
        <td class="num">{_p(-a['recul_max'])}</td><td class="num">{a['ordres']}</td><td class="num">{a['investi'] * 100:.0f} %</td>
        <td><small>{e(a['reglage'])}</small></td></tr>""" for a in wf["annees"])
    cartes = "".join(f"""<div class="indic {'ok' if ok else 'ko'}"><small>{e(nom)}</small><b>{'✅' if ok else '❌'} {e(val)}</b></div>"""
                     for nom, ok, val in verdicts(resultats))
    motifs = (resultats["Algo — walk-forward (hors échantillon)"].get("ordres") or {}).get("motifs_vente", {})
    motifs_txt = " · ".join(f"{e(k)} : {v}" for k, v in motifs.items()) or "aucune vente"
    mc_html = (f"""<section class="contexte">
      <div class="indic"><small>Année médiane</small><b>{_p(mc['perf_p50'])}</b></div>
      <div class="indic"><small>Mauvaise année (1 sur 20)</small><b>{_p(mc['perf_p5'])}</b></div>
      <div class="indic"><small>Très bonne année (1 sur 20)</small><b>{_p(mc['perf_p95'])}</b></div>
      <div class="indic"><small>Probabilité d'une année négative</small><b>{mc['proba_perte'] * 100:.0f} %</b></div>
      <div class="indic"><small>Probabilité d'atteindre +15 %</small><b>{mc['proba_objectif'] * 100:.0f} %</b></div>
      <div class="indic"><small>Probabilité d'un recul ≥ 10 % dans l'année</small><b>{mc['proba_recul_10'] * 100:.0f} %</b></div>
    </section>""" if mc else "<p>Historique trop court pour le Monte Carlo.</p>")
    graph = _graphique({"Algo (hors échantillon)": series["Algo — walk-forward (hors échantillon)"],
                        "ETF de référence": ref, "Même univers conservé": univ})
    a0, a1 = wf["oos"].index[0], wf["oos"].index[-1]
    page = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest walk-forward</title>
<style>
:root {{ --fond:#f6f5f1; --carte:#fcfcfb; --texte:#0b0b0b; --doux:#52514e; --ligne:#e4e2db;
        --vert:#1f7a4d; --rouge:#b23a2e; --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fond:#141413; --carte:#1a1a19; --texte:#f0efec; --doux:#c3c2b7;
        --ligne:#33322e; --vert:#5cc28d; --rouge:#e3776b; --s1:#3987e5; --s2:#d95926; --s3:#199e70; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--fond); color:var(--texte); font:15px/1.5 -apple-system, "SF Pro Text", "Helvetica Neue", sans-serif; }}
main {{ max-width:1200px; margin:0 auto; padding:28px 16px 60px; }}
h1 {{ font-size:26px; margin:0 0 4px; }} h2 {{ font-size:18px; margin:32px 0 12px; }}
.sous-titre {{ color:var(--doux); margin:0 0 20px; }}
.contexte {{ display:flex; flex-wrap:wrap; gap:10px; }}
.indic {{ background:var(--carte); border:1px solid var(--ligne); border-radius:10px; padding:10px 14px; min-width:170px; }}
.indic small {{ display:block; color:var(--doux); font-size:12px; }} .indic b {{ font-size:16px; }}
.indic.ok {{ border-left:4px solid var(--vert); }} .indic.ko {{ border-left:4px solid var(--rouge); }}
.tableau {{ overflow-x:auto; background:var(--carte); border:1px solid var(--ligne); border-radius:12px; }}
table {{ border-collapse:collapse; width:100%; min-width:760px; font-size:13.5px; }}
th, td {{ padding:9px 10px; border-bottom:1px solid var(--ligne); text-align:left; vertical-align:top; }}
th {{ font-size:12px; color:var(--doux); font-weight:600; }}
.num {{ font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap; }}
.pos {{ color:var(--vert); }} .neg {{ color:var(--rouge); }} td small {{ color:var(--doux); }}
.graph {{ position:relative; background:var(--carte); border:1px solid var(--ligne); border-radius:12px; padding:8px; }}
.graph svg {{ width:100%; height:auto; display:block; }}
.grille {{ stroke:var(--ligne); stroke-width:1; }} .axe {{ fill:var(--doux); font-size:12px; }}
.serie {{ fill:none; stroke-width:2; stroke-linejoin:round; }}
.serie.s1, .pt.s1 {{ stroke:var(--s1); }} .serie.s2, .pt.s2 {{ stroke:var(--s2); }} .serie.s3, .pt.s3 {{ stroke:var(--s3); }}
.pt {{ fill:var(--carte); stroke-width:2; }}
.lab {{ fill:var(--texte); font-size:12px; }} .viseur {{ stroke:var(--doux); stroke-width:1; stroke-dasharray:3 3; }}
.legende {{ display:flex; flex-wrap:wrap; gap:16px; margin:0 0 8px; color:var(--doux); font-size:13px; }}
.legende i, .bulle i {{ display:inline-block; width:12px; height:3px; border-radius:2px; margin-right:6px; vertical-align:middle; }}
i.s1 {{ background:var(--s1); }} i.s2 {{ background:var(--s2); }} i.s3 {{ background:var(--s3); }}
.bulle {{ position:absolute; top:16px; background:var(--carte); border:1px solid var(--ligne); border-radius:8px;
          padding:8px 10px; font-size:12.5px; pointer-events:none; box-shadow:0 4px 14px rgba(0,0,0,.12); white-space:nowrap; }}
.limites li {{ margin:4px 0; }} footer {{ margin-top:30px; color:var(--doux); font-size:12.5px; }}
</style></head><body><main>
<h1>Backtest walk-forward</h1>
<p class="sous-titre">Période hors échantillon : {a0.strftime('%d/%m/%Y')} → {a1.strftime('%d/%m/%Y')} · {len(d['tickers'])} actions ·
capital de départ {sum(config.CAPITAL_INITIAL.values()):,.0f} € (CTO {config.CAPITAL_INITIAL['CTO']:,.0f} € + PEA {config.CAPITAL_INITIAL['PEA']:,.0f} €)
· réglage choisi chaque année sur les {ANNEES_TRAIN} années précédentes</p>

<h2>Objectifs</h2>
<section class="contexte">{cartes}</section>

<h2>Valeur de 100 € investis</h2>
{graph}

<h2>Résumé</h2>
<div class="tableau"><table><thead><tr><th></th><th class="num">Rendement / an</th><th class="num">Total</th>
<th class="num">Recul max</th><th class="num">Volatilité</th><th class="num">Sharpe</th><th class="num">Calmar</th>
<th class="num">Ordres / an</th><th class="num">Ventes gagnantes</th><th class="num">Profit factor</th></tr></thead>
<tbody>{''.join(lignes)}</tbody></table></div>
<p class="sous-titre">Motifs de vente : {motifs_txt}</p>

<h2>Année par année (hors échantillon)</h2>
<div class="tableau"><table><thead><tr><th>Année</th><th class="num">Algo</th><th class="num">ETF de réf.</th>
<th class="num">Univers conservé</th><th class="num">Recul max</th><th class="num">Ordres</th><th class="num">Investi en moyenne</th>
<th>Réglage choisi sur les 3 années précédentes</th></tr></thead><tbody>{lignes_annees}</tbody></table></div>

<h2>Règles de vente testées</h2>
<p class="sous-titre">Dans tous les cas : stop à -{config.STOP_LIGNE * 100:.0f} % depuis l'achat et garde-fous du portefeuille.
Règle utilisée aujourd'hui par le portefeuille fictif : <b>{config.REGLE_VENTE}</b> ·
règles mises en concours dans ce backtest : <b>{', '.join(config.VENTES_TESTEES)}</b>.</p>
<ul class="limites">{''.join(f"<li><b>{k}</b> — {e(v['nom'])}</li>" for k, v in config.REGLES_VENTE.items())}</ul>

<h2>Monte Carlo — 1 000 années simulées</h2>
<p class="sous-titre">Des mois réels de l'algo, tirés au hasard et remis bout à bout : ce qui peut arriver sur une année.</p>
{mc_html}

<h2>Limites à garder en tête</h2>
<ul class="limites">
<li><b>Sans fondamentaux.</b> Il n'existe pas d'historique gratuit et fiable des ratios. Ce backtest mesure le timing
technique, la sélection par tendance et la gestion du risque. Le score fondamental, lui, sera mesuré sur les relevés
quotidiens que le système accumule depuis son installation.</li>
<li><b>Biais du survivant.</b> L'univers est choisi aujourd'hui, parmi des entreprises qui ont réussi. Toute stratégie
sur cet univers paraît meilleure qu'elle ne l'aurait été en vrai. C'est pourquoi la vraie question est la ligne
« même univers acheté et conservé » : l'algo fait-il mieux, à risque égal, que garder ces actions sans rien faire ?</li>
<li><b>Dividendes et fiscalité.</b> Les cours sont ajustés des dividendes (réinvestis), comme les ETF de référence.
La fiscalité (flat tax sur le CTO, PEA) n'est pas déduite.</li>
<li><b>Le passé ne garantit rien.</b> Un bon backtest est une condition nécessaire, pas suffisante. Les 6 mois de
portefeuille fictif en conditions réelles restent le juge final.</li>
</ul>
<footer>Généré localement le {date.today().strftime('%d/%m/%Y')}. Rendements nets de frais de courtage, de change et de
glissement de prix. Sharpe calculé sans taux sans risque. Outil personnel, pas un conseil en investissement.</footer>
</main></body></html>"""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(page, encoding="utf-8")
