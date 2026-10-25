"""
Tests de vérification.

    python verifier.py            teste tout le système avec des données simulées (hors ligne)
    python verifier.py --reseau   teste aussi les vraies sources : Yahoo, FRED, BCE et Ollama

Rien n'est écrit dans ta vraie base : les tests utilisent un dossier temporaire.
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
from modules import db, rapport  # noqa: E402
from modules.scoring_fondamental import calculer_scores_fondamentaux  # noqa: E402
from modules.timing_technique import calculer_scores_techniques, rsi  # noqa: E402

RESULTATS: list[tuple[str, bool, str]] = []


def verifier(nom: str, condition: bool, detail: str = "") -> None:
    RESULTATS.append((nom, bool(condition), detail))
    print(f"  {'✅' if condition else '❌'} {nom}{(' — ' + detail) if detail else ''}")


def prix_simules(ticker: str, jours: int, tendance: float, graine: int) -> pd.DataFrame:
    rng = np.random.default_rng(graine)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=jours)
    rendements = rng.normal(tendance / 252, 0.018, jours)
    close = 100 * np.exp(np.cumsum(rendements))
    return pd.DataFrame({
        "Open": close * (1 + rng.normal(0, 0.003, jours)),
        "High": close * (1 + np.abs(rng.normal(0, 0.01, jours))),
        "Low": close * (1 - np.abs(rng.normal(0, 0.01, jours))),
        "Close": close,
        "Volume": rng.integers(1_000_000, 5_000_000, jours).astype(float),
    }, index=dates)


def fondamentaux_simules(graine: int) -> dict:
    rng = np.random.default_rng(graine)
    return {
        "trailingPE": rng.uniform(-10, 60), "forwardPE": rng.uniform(8, 45),
        "enterpriseToEbitda": rng.uniform(5, 40), "priceToSalesTrailing12Months": rng.uniform(1, 15),
        "priceToBook": rng.uniform(1, 20), "returnOnEquity": rng.uniform(-0.1, 0.6),
        "returnOnAssets": rng.uniform(0, 0.25), "profitMargins": rng.uniform(-0.05, 0.4),
        "operatingMargins": rng.uniform(0, 0.5), "revenueGrowth": rng.uniform(-0.1, 0.5),
        "earningsGrowth": rng.uniform(-0.3, 0.8), "debtToEquity": rng.uniform(0, 250),
        "currentRatio": rng.uniform(0.6, 3), "quickRatio": rng.uniform(0.4, 2.5),
        "freeCashflow": rng.uniform(-1e9, 5e10), "marketCap": rng.uniform(2e10, 3e12),
        "currency": "USD", "sector": "Test", "industry": "Test",
    }


def test_hors_ligne() -> None:
    print("\n1. Calculs de base")
    serie = pd.Series(np.linspace(100, 200, 100))
    verifier("RSI d'une série qui ne fait que monter ≈ 100", rsi(serie).iloc[-1] > 99)
    serie_baisse = pd.Series(np.linspace(200, 100, 100))
    verifier("RSI d'une série qui ne fait que baisser ≈ 0", rsi(serie_baisse).iloc[-1] < 1)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = db.connexion(tmp / "test.db")

        print("\n2. Base de données")
        tickers = list(config.UNIVERS)[:12]
        tendances = np.linspace(-0.25, 0.45, len(tickers))
        for i, t in enumerate(tickers):
            n = db.upsert_prix(conn, t, prix_simules(t, 600, tendances[i], i))
            db.enregistrer_fondamentaux(conn, t, pd.Timestamp.today().strftime("%Y-%m-%d"), fondamentaux_simules(i))
        db.upsert_prix(conn, tickers[0], prix_simules(tickers[0], 600, tendances[0], 0))  # 2e passage = mise à jour
        nb = conn.execute("SELECT COUNT(*) FROM prix").fetchone()[0]
        verifier("Insertion des prix sans doublons", nb == 600 * len(tickers), f"{nb} lignes")
        fond = db.charger_fondamentaux_recents(conn)
        verifier("Lecture des fondamentaux", len(fond) == len(tickers))

        dates = pd.bdate_range(end=pd.Timestamp.today(), periods=300)
        db.upsert_macro(conn, "VIX", "test", pd.Series(np.linspace(14, 18, 300), index=dates))
        db.upsert_macro(conn, "SP500", "test", pd.Series(np.linspace(4000, 5000, 300), index=dates))
        db.upsert_macro(conn, "STOXX50", "test", pd.Series(np.linspace(5000, 4500, 300), index=dates))
        db.upsert_macro(conn, "EURUSD", "test", pd.Series(np.r_[np.full(295, 1.08), np.linspace(1.08, 1.11, 5)], index=dates))

        print("\n3. Scoring fondamental (module 2)")
        sf = calculer_scores_fondamentaux(fond)
        verifier("Scores entre 0 et 100", sf["score_fondamental"].between(0, 100).all())
        verifier("Aucun score manquant", sf["score_fondamental"].notna().all())
        perte = fond.index[pd.to_numeric(fond["trailingPE"]) <= 0]
        if len(perte):
            verifier("Une entreprise en perte obtient 0 sur son PER", (sf.loc[perte, "p_trailingPE"] == 0).all())

        print("\n4. Timing technique (module 3)")
        st = calculer_scores_techniques(db.charger_prix(conn))
        verifier("Toutes les actions ont un score technique", len(st) == len(tickers))
        verifier("Scores entre 0 et 100", st["score_technique"].between(0, 100).all())
        sans_tendance = st[~st["tendance_haussiere"].astype(bool)]
        verifier("Règle d'or : score plafonné sans tendance haussière",
                 (sans_tendance["score_technique"] <= config.PLAFOND_SANS_TENDANCE).all(),
                 f"{len(sans_tendance)} action(s) concernée(s)")

        print("\n5. Rapport")
        chemin = rapport.executer(conn, dossier=tmp / "rapports")
        verifier("Page HTML créée", chemin.exists() and chemin.stat().st_size > 2000, chemin.name)
        nb_scores = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        verifier("Scores du jour enregistrés en base", nb_scores == len(tickers))
        ctx = rapport.contexte_marche(conn)
        verifier("Alerte EUR/USD déclenchée (+2,8 % en 5 jours)", any("EUR/USD" in a for a in ctx["alertes"]))
        conn.close()


def test_portefeuille() -> None:
    import contextlib
    import io

    from modules import portefeuille as pf

    print("\n6. Portefeuille fictif sur 60 séances simulées")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = db.connexion(tmp / "pf.db")
        tickers = [t for t in config.UNIVERS][:10] + ["ASML.AS", "MC.PA", "SAP.DE", "NOVO-B.CO", "SU.PA", "AIR.PA"]
        series = {t: prix_simules(t, 700, np.random.default_rng(i).uniform(-0.1, 0.6), 100 + i) for i, t in enumerate(tickers)}
        for i, t in enumerate(tickers):
            db.upsert_prix(conn, t, series[t].iloc[:640])
            db.enregistrer_fondamentaux(conn, t, "2026-01-01", fondamentaux_simules(200 + i))
        dates = series[tickers[0]].index
        vix = np.full(700, 15.0)
        vix[660:670] = 35.0  # 10 séances de stress : aucun achat attendu
        for nom, val in {"VIX": vix, "SP500": np.linspace(4000, 5200, 700), "STOXX50": np.linspace(4000, 4800, 700),
                         "EURUSD": np.full(700, 1.10), "EURDKK": np.full(700, 7.46),
                         "BENCH_US": np.linspace(400, 520, 700), "BENCH_EU": np.linspace(40, 48, 700)}.items():
            db.upsert_macro(conn, nom, "test", pd.Series(val, index=dates))

        jours_defensifs = set(d.strftime("%Y-%m-%d") for d in dates[660:670])
        cash_negatif = False
        for k in range(640, 700):
            for t in tickers:
                db.upsert_prix(conn, t, series[t].iloc[[k]])
            with contextlib.redirect_stdout(io.StringIO()):
                rapport.executer(conn, dossier=tmp / "rapports")
            cash_negatif |= any(r[0] < -0.01 for r in conn.execute("SELECT montant FROM cash"))

        tx = pd.read_sql_query("SELECT * FROM transactions WHERE portefeuille = 'fictif'", conn)
        ordres = pd.read_sql_query("SELECT * FROM ordres WHERE portefeuille = 'fictif'", conn)
        verifier("Le portefeuille fictif a passé des ordres", len(tx) > 0, f"{len(tx)} transactions")
        verifier("Jamais de cash négatif", not cash_negatif)
        execs = ordres[ordres["statut"] == "execute"]
        verifier("Ordres exécutés à la séance suivante (pas le jour de la décision)",
                 (execs["date_execution"] > execs["date_ordre"]).all())
        verifier(f"Budget de {config.BUDGET_TRADES_AN} trades/an respecté", len(tx) <= config.BUDGET_TRADES_AN)
        achats_stress = ordres[(ordres["sens"] == "achat") & ordres["date_ordre"].isin(jours_defensifs)]
        verifier("Aucun achat décidé en contexte défensif (cash is a position)", achats_stress.empty)
        pos = pf.positions(conn, "fictif")
        verifier("PEA = actions européennes uniquement",
                 all(config.UNIVERS[t]["region"] == "EU" for t in pos[pos["poche"] == "PEA"].index))
        nb_max = pos.groupby("poche").size().max() if len(pos) else 0
        verifier("Nombre de positions par poche plafonné", nb_max <= config.MAX_POSITIONS_PAR_POCHE)
        v = pf.valoriser(conn, "fictif", pf.date_marche(conn), enregistrer=False)
        total = sum(v[p]["total_eur"] for p in pf.POCHES)
        verifier("Valeur totale cohérente", total > 0.5 * sum(config.CAPITAL_INITIAL.values()), f"{total:,.0f} €")
        verifier("Référence (benchmark) calculée", all(v[p]["benchmark_eur"] for p in pf.POCHES))
        tailles = [l["valeur_eur"] / v[p]["total_eur"] for p in pf.POCHES for l in v[p]["lignes"]]
        verifier("Taille maximale par position respectée à l'achat (≈ 10 %)", all(x < 0.16 for x in tailles))

        print("\n7. Portefeuille réel (saisies manuelles)")
        jour = pf.date_marche(conn)
        pf.apport(conn, "PEA", 1000, jour=jour, portefeuilles=("reel",))
        verifier("Apport enregistré", abs(pf._cash(conn, "reel", "PEA") - (config.CAPITAL_INITIAL["PEA"] + 1000)) < 0.01)
        pf.transaction_reelle(conn, "achat", "ASML.AS", 2, 500.0, jour=jour, frais_eur=2)
        verifier("Achat réel : cash débité", abs(pf._cash(conn, "reel", "PEA") - (config.CAPITAL_INITIAL["PEA"] + 1000 - 1002)) < 0.01)
        msg = pf.transaction_reelle(conn, "vente", "ASML.AS", 2, 550.0, jour=jour, frais_eur=2)
        verifier("Vente réelle : plus-value calculée", "+96.00" in msg, msg.split("—")[-1].strip())
        try:
            pf.transaction_reelle(conn, "achat", "AAPL", 10_000, 200.0, jour=jour)
            verifier("Achat refusé si cash insuffisant", False)
        except RuntimeError:
            verifier("Achat refusé si cash insuffisant", True)
        html_ok = "Portefeuille fictif" in (tmp / "rapports" / "dernier_rapport.html").read_text()
        verifier("Section portefeuille présente dans le rapport", html_ok)
        conn.close()


def test_garde_fous() -> None:
    """Budget de 50 ordres avec dérogations, stop par ligne, drawdown, secteurs, entrées progressives."""
    from modules import portefeuille as pf

    print("\n8. Budget et garde-fous")
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connexion(Path(tmp) / "gf.db")
        dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=300)
        jour = dates[-1].strftime("%Y-%m-%d")
        tickers = ["AAPL", "MSFT", "GOOGL", "ORCL", "CRM", "ADBE", "JPM", "V", "KO", "PG", "XOM", "LLY", "CAT"]
        plat = pd.DataFrame({"Open": 100.0, "High": 100.0, "Low": 100.0, "Close": 100.0, "Volume": 1e6}, index=dates)
        for t in tickers:
            db.upsert_prix(conn, t, plat)
        for nom, val in {"EURUSD": 1.10, "EURDKK": 7.46, "BENCH_US": 500.0, "BENCH_EU": 50.0}.items():
            db.upsert_macro(conn, nom, "test", pd.Series(val, index=dates))
        pf.initialiser(conn, jour=jour)

        def remise_a_zero():
            for table in ("ordres", "decisions_jour", "transactions", "positions", "valorisations", "apports"):
                conn.execute(f"DELETE FROM {table}")
            for p, m in config.CAPITAL_INITIAL.items():
                conn.execute("UPDATE cash SET montant = ? WHERE poche = ?", (m, p))
            conn.commit()

        def classement(scores: dict, signal=rapport.SIGNAL_CANDIDAT) -> pd.DataFrame:
            return pd.DataFrame({"signal": signal, "score_combine": pd.Series(scores), "score_fondamental": 70.0,
                                 "cours": 100.0, "mm200": 95.0})

        def achats():
            return pd.read_sql_query("SELECT * FROM ordres WHERE sens = 'achat' AND portefeuille = 'fictif'", conn)

        def position(t, pru_eur):
            conn.execute("INSERT INTO positions VALUES ('fictif', ?, ?, 10, ?, ?, 70)", (t, config.poche(t), pru_eur, jour))
            conn.commit()

        ctx = {"regime": "Favorable"}
        normal = {"niveau": 0, "libelle": "Normal", "regle": "", "drawdown": 0.0, "perte_jour": 0.0}

        # entrées progressives
        remise_a_zero()
        pf.decider_fictif(conn, classement({t: 80 for t in ["AAPL", "JPM", "KO", "XOM", "LLY", "CAT"]}), ctx, jour, normal)
        verifier(f"Au plus {config.MAX_ACHATS_PAR_JOUR} nouveaux achats par jour", len(achats()) == config.MAX_ACHATS_PAR_JOUR,
                 f"{len(achats())} achats")

        # plafond sectoriel
        remise_a_zero()
        ancien, config.MAX_ACHATS_PAR_JOUR = config.MAX_ACHATS_PAR_JOUR, 99
        pf.decider_fictif(conn, classement({t: 80 for t in ["AAPL", "MSFT", "GOOGL", "ORCL", "CRM", "ADBE"]}), ctx, jour, normal)
        config.MAX_ACHATS_PAR_JOUR = ancien
        tech = achats()["montant_eur"].sum()
        verifier(f"Secteur plafonné à {config.MAX_SECTEUR * 100:.0f} % de la poche",
                 tech <= config.MAX_SECTEUR * config.CAPITAL_INITIAL["CTO"] + 1,
                 f"Tech {tech / config.CAPITAL_INITIAL['CTO'] * 100:.1f} % du CTO")

        # budget de 50 ordres : au-delà, seulement les grosses opportunités
        remise_a_zero()
        for _ in range(config.BUDGET_TRADES_AN):
            conn.execute("INSERT INTO transactions (portefeuille, date, ticker, poche, sens, motif) "
                         "VALUES ('fictif', ?, 'KO', 'CTO', 'achat', 'test')", (jour,))
        conn.commit()
        pf.decider_fictif(conn, classement({"AAPL": 80, "JPM": 90}), ctx, jour, normal)
        a = achats()
        verifier("Budget atteint : achat ordinaire (score 80) refusé", "AAPL" not in set(a["ticker"]))
        verifier(f"Budget atteint : grosse opportunité (score ≥ {config.SCORE_EXCEPTIONNEL}) acceptée hors budget",
                 list(a["ticker"]) == ["JPM"] and "hors budget" in a["motif"].iloc[0])
        conn.execute("DELETE FROM decisions_jour")
        for _ in range(config.RESERVE_EXCEPTIONNELLE):
            conn.execute("INSERT INTO transactions (portefeuille, date, ticker, poche, sens, motif) "
                         "VALUES ('fictif', ?, 'KO', 'CTO', 'achat', 'test')", (jour,))
        conn.commit()
        pf.decider_fictif(conn, classement({"V": 95}), ctx, jour, normal)
        verifier(f"Plafond absolu de {config.BUDGET_TRADES_AN + config.RESERVE_EXCEPTIONNELLE} ordres respecté",
                 "V" not in set(achats()["ticker"]))
        conn.execute("DELETE FROM decisions_jour")
        position("ORCL", 140.0)  # 100 $ = 90,9 € : -35 %
        conn.commit()
        pf.decider_fictif(conn, classement({"ORCL": 70}), ctx, jour, normal)
        vente = conn.execute("SELECT motif FROM ordres WHERE sens = 'vente' AND ticker = 'ORCL'").fetchone()
        verifier("Vente de protection jamais bloquée par le budget (gros risque)",
                 vente is not None and "hors budget" in vente[0], vente[0] if vente else "aucune vente")

        # stop par ligne
        remise_a_zero()
        position("AAPL", 110.0)   # 90,9 € / 110 € : -17 % -> stop
        position("MSFT", 95.0)    # -4 % : volatilité normale, on garde
        pf.decider_fictif(conn, classement({"AAPL": 70, "MSFT": 70}), ctx, jour, normal)
        ventes = {r[0]: r[1] for r in conn.execute("SELECT ticker, motif FROM ordres WHERE sens = 'vente'")}
        verifier(f"Stop à -{config.STOP_LIGNE * 100:.0f} % déclenché", "AAPL" in ventes and "stop" in ventes["AAPL"])
        verifier("Pas de vente sur une baisse de 4 % (diamond hands)", "MSFT" not in ventes)

        # drawdown : apports neutralisés
        remise_a_zero()
        for d, total in ((dates[-4], 100_000.0), (dates[-3], 95_000.0), (dates[-2], 105_000.0)):
            conn.execute("INSERT INTO valorisations VALUES ('fictif', ?, 'CTO', 0, ?, ?, NULL)", (d.strftime("%Y-%m-%d"), total, total))
        conn.execute("INSERT INTO apports VALUES ('fictif', ?, 'CTO', 10000)", (dates[-2].strftime("%Y-%m-%d"),))
        conn.commit()
        g = pf.etat_garde_fous(conn, "fictif")
        verifier("Un apport n'est pas compté comme un gain", abs(g["drawdown"] - 0.05) < 1e-6,
                 f"recul {g['drawdown'] * 100:.1f} % (attendu 5,0 %)")
        verifier("Recul de 5 % : niveau Vigilance", g["niveau"] == 1, g["libelle"])

        # 95 % x 100 580 / 105 000 = 0,91 -> recul de 9 % depuis le plus haut
        conn.execute("INSERT INTO valorisations VALUES ('fictif', ?, 'CTO', 0, 100580, 100580, NULL)", (jour,))
        conn.commit()
        g = pf.etat_garde_fous(conn, "fictif")
        verifier("Recul de 9 % : niveau Protection", g["niveau"] == 2, f"{g['libelle']}, -{g['drawdown'] * 100:.1f} %")
        verifier("Perte de la séance mesurée", abs(g["perte_jour"] - (1 - 100.58 / 105)) < 1e-6, f"-{g['perte_jour'] * 100:.1f} %")
        pf.decider_fictif(conn, classement({"JPM": 90}), ctx, jour, g)
        verifier("Protection : aucun achat, même à score 90", achats().empty)

        conn.execute("DELETE FROM decisions_jour")
        position("KO", 99.0)   # -8,2 % -> vendu en Urgence
        position("PG", 92.0)   # -1,2 % -> conservé
        urgence = {**g, "niveau": 3, "libelle": "Urgence"}
        pf.decider_fictif(conn, classement({"KO": 70, "PG": 70}), ctx, jour, urgence)
        ventes = {r[0] for r in conn.execute("SELECT ticker FROM ordres WHERE sens = 'vente'")}
        verifier(f"Urgence : vente des lignes à -{config.PERTE_LIGNE_URGENCE * 100:.0f} % et plus", "KO" in ventes)
        verifier("Urgence : les lignes saines sont conservées", "PG" not in ventes)

        # vigilance : seulement les bons scores, tailles divisées par 2
        remise_a_zero()
        conn.execute("UPDATE cash SET montant = 30000 WHERE portefeuille = 'fictif' AND poche = 'CTO'")  # indépendant du capital choisi
        conn.commit()
        vigilance = {**normal, "niveau": 1, "libelle": "Vigilance"}
        pf.decider_fictif(conn, classement({"JPM": 80, "KO": 70}), ctx, jour, vigilance)
        a = achats()
        attendu = pf.taille_cible(80, 30000) * config.FACTEUR_TAILLE_VIGILANCE
        verifier("Vigilance : score < 75 écarté, taille divisée par 2",
                 list(a["ticker"]) == ["JPM"] and abs(a["montant_eur"].iloc[0] - attendu) < 1,
                 f"{a['montant_eur'].iloc[0]:,.0f} €" if len(a) else "aucun achat")

        # pas de blocage éternel : après une chute puis 100 % de cash, l'algo doit pouvoir réinvestir
        suivi = pf.SuiviGardeFous()
        niveaux = [suivi.maj(v)[0] for v in [1.0, 0.95, 0.88] + [0.88] * (3 * config.PAUSE_GARDE_FOU)]
        p = config.PAUSE_GARDE_FOU
        verifier(f"Après Urgence : pause de {p} séances sans achat", min(niveaux[2:2 + p]) >= 2)
        verifier("Puis reprise prudente (Vigilance)", niveaux[2 + p] == 1 and max(niveaux[2 + p:2 + 2 * p]) == 1)
        verifier("Puis retour à la normale : pas de blocage éternel en cash", niveaux[-1] == 0)

        # écrêtage : une ligne trop grosse est ramenée à 15 % de sa poche
        remise_a_zero()
        conn.execute("INSERT INTO positions VALUES ('fictif', 'AAPL', 'CTO', 90, 80.0, ?, 70)", (jour,))  # ≈ 8 182 €
        conn.execute("UPDATE cash SET montant = 13000 WHERE portefeuille = 'fictif' AND poche = 'CTO'")
        conn.commit()
        pf.decider_fictif(conn, classement({"AAPL": 70}), ctx, jour, normal)
        e = conn.execute("SELECT fraction, motif FROM ordres WHERE ticker = 'AAPL' AND sens = 'vente'").fetchone()
        valeur = 90 * 100 / 1.10
        attendu = 1 - config.ECRETAGE_CIBLE * (valeur + 13000) / valeur
        verifier(f"Écrêtage : ligne à 39 % ramenée à {config.ECRETAGE_CIBLE * 100:.0f} %",
                 e is not None and "écrêtage" in e[1] and abs(e[0] - attendu) < 1e-6,
                 f"vente de {e[0] * 100:.0f} % de la ligne" if e else "aucune vente")
        pf.executer_ordres_en_attente(conn)  # pas de séance suivante en base : l'ordre reste en attente
        conn.execute("UPDATE ordres SET statut = 'annule'")
        conn.execute("DELETE FROM decisions_jour")
        conn.execute("UPDATE positions SET quantite = 30 WHERE ticker = 'AAPL'")  # ≈ 2 727 € : 15 % du CTO
        conn.execute("UPDATE cash SET montant = 15000 WHERE portefeuille = 'fictif' AND poche = 'CTO'")
        conn.commit()
        pf.decider_fictif(conn, classement({"AAPL": 70}), ctx, jour, normal)
        verifier("Écrêtage : rien à faire sous le seuil de 20 %",
                 conn.execute("SELECT COUNT(*) FROM ordres WHERE statut = 'en_attente' AND sens = 'vente'").fetchone()[0] == 0)

        # alertes sur le portefeuille réel
        conn.execute("INSERT INTO positions VALUES ('reel', 'AAPL', 'CTO', 10, 110.0, ?, 70)", (jour,))
        conn.commit()
        al = pf.alertes_reel(conn, classement({"AAPL": 70}), jour)
        verifier("Portefeuille réel : alerte de stop", any("stop" in x for x in al))
        conn.close()


def test_backtest() -> None:
    """Backtest : mêmes calculs que le rapport du jour, aucune donnée du futur, règles respectées."""
    import contextlib
    import io
    import logging
    import sqlite3

    from modules import backtest as bt
    from modules.timing_technique import calculer_scores_techniques

    print("\n9. Backtest walk-forward (5 ans simulés)")
    logging.getLogger("algo").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = db.connexion(tmp / "bt.db")
        tickers = list(config.UNIVERS)[:10] + ["ASML.AS", "MC.PA", "SAP.DE", "SU.PA", "AIR.PA", "TTE.PA"]
        n = 1300
        for i, t in enumerate(tickers):
            db.upsert_prix(conn, t, prix_simules(t, n, np.random.default_rng(300 + i).uniform(-0.05, 0.35), 300 + i))
        dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
        rng = np.random.default_rng(5)
        vix = np.full(n, 16.0)
        vix[700:740] = 34.0
        for nom, val in {"VIX": vix, "SP500": 4000 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n))),
                         "STOXX50": 4000 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n))),
                         "EURUSD": 1.10 + 0.05 * np.sin(np.arange(n) / 120), "EURDKK": np.full(n, 7.46),
                         "BENCH_US": 400 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n))),
                         "BENCH_EU": 40 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n)))}.items():
            db.upsert_macro(conn, nom, "test", pd.Series(val, index=dates))

        d = bt.preparer_donnees(conn)
        st = calculer_scores_techniques(db.charger_prix(conn, depuis=(dates[-1] - pd.DateOffset(years=2)).strftime("%Y-%m-%d")))
        ecart = max(abs(d["score"][-1, d["tickers"].index(t)] - st.loc[t, "score_technique"]) for t in st.index)
        verifier("Mêmes scores techniques que le rapport du jour", ecart < 1.0, f"écart max {ecart:.2f} point")

        debut = bt.debut_par_defaut(d)
        run = bt.simuler(d, bt.reglage_actuel(), debut)
        o = run["ordres"]
        verifier("Le backtest passe des ordres", len(o) > 0, f"{len(o)} ordres")
        verifier("Ordres exécutés après la décision (jamais le jour même)", (o["date"] > o["decision"]).all())
        verifier("Valeur du portefeuille toujours positive", (run["courbe"]["total"] > 0).all())
        dates_o = o.loc[o["sens"] == "achat", "date"].sort_values().reset_index(drop=True)
        max_12m = max(((dates_o > x - pd.Timedelta(days=365)) & (dates_o <= x)).sum() for x in dates_o)
        verifier(f"Achats : jamais plus de {config.BUDGET_TRADES_AN + config.RESERVE_EXCEPTIONNELLE} sur 12 mois "
                 "(les ventes de protection ne sont jamais bloquées)",
                 max_12m <= config.BUDGET_TRADES_AN + config.RESERVE_EXCEPTIONNELLE, f"max {max_12m}")
        defensifs = set(d["dates"][d["defensif"]])
        achats_defensifs = o[(o["sens"] == "achat") & o["decision"].isin(defensifs)]
        verifier("Aucun achat décidé en contexte défensif", achats_defensifs.empty)

        with contextlib.redirect_stdout(io.StringIO()):
            wf = bt.walk_forward(d, debut)
        premiere_annee_test = wf["annees"][0]["annee"]
        verifier("Walk-forward : la 1re année testée vient après 3 ans d'entraînement",
                 premiere_annee_test >= debut.year + bt.ANNEES_TRAIN, f"entraînement dès {debut.year}, test dès {premiere_annee_test}")

        # pas de fuite du futur : on modifie tous les cours après une date, le passé ne doit pas bouger
        coupure = pd.Timestamp(f"{wf['annees'][-1]['annee']}-01-01")
        conn2 = sqlite3.connect(tmp / "bt2.db")
        conn.backup(conn2)
        conn2.execute("UPDATE prix SET open = open * 0.6, high = high * 0.6, low = low * 0.6, close = close * 0.6 "
                      "WHERE date >= ?", (coupure.strftime("%Y-%m-%d"),))
        conn2.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            wf2 = bt.walk_forward(bt.preparer_donnees(conn2), debut)
        avant = wf["oos"].index < coupure - pd.Timedelta(days=3)
        identiques = np.allclose(wf["oos"][avant].to_numpy(), wf2["oos"].reindex(wf["oos"].index)[avant].to_numpy())
        verifier("Aucune donnée du futur : modifier l'avenir ne change pas le passé", identiques)
        verifier("Les réglages choisis avant la coupure ne changent pas",
                 [a["reglage"] for a in wf["annees"][:-1]] == [a["reglage"] for a in wf2["annees"][:-1]])
        conn2.close()

        with contextlib.redirect_stdout(io.StringIO()):
            chemin = bt.executer(conn, dossier=tmp / "rapports")
        texte = chemin.read_text()
        verifier("Rapport de backtest créé", "Backtest walk-forward" in texte and "Même univers" in texte, chemin.name)
        conn.close()
    logging.getLogger("algo").setLevel(logging.INFO)


def test_regles_vente() -> None:
    """Les 5 règles de vente, et la même décision dans le backtest et dans le portefeuille fictif."""
    import sqlite3

    from modules import backtest as bt
    from modules import portefeuille as pf
    from modules import regles_vente as rv

    print("\n10. Règles de vente")

    def parcours(cours_suite, code, prix_entree=100.0, mm200=100.0):
        """Fait vivre une ligne séance par séance ; renvoie la 1re décision de vente (n° de séance, motif, part)."""
        r = config.REGLES_VENTE[code]
        etat = rv.EtatLigne(prix_entree=prix_entree, plus_haut=prix_entree)
        moyennes = mm200 if isinstance(mm200, list) else [mm200] * len(cours_suite)
        for k, (c, m) in enumerate(zip(cours_suite, moyennes)):
            rv.maj_etat(etat, c, m, r)
            motif, part = rv.decider(etat, c, c / prix_entree - 1, r)
            if motif:
                return k, motif, part
        return None, None, 0.0

    k, motif, _ = parcours([99, 96.5], "A")
    verifier("A : vente dès la 1re clôture à plus de 3 % sous la MM200", k == 1 and "tendance" in motif)
    k, _, _ = parcours([96.5] * 9, "C")
    verifier("C : pas de vente après 9 séances sous la MM200", k is None)
    k, motif, _ = parcours([96.5] * 10, "C")
    verifier("C : vente à la 10e séance", k == 9 and "10 séances" in motif)
    k, _, _ = parcours([96.5] * 3, "B")
    verifier("B : pas de vente à 3,5 % sous la MM200 (marge de 8 %)", k is None)
    hausse = list(range(101, 201))   # de 100 à 200 : un "gagnant" ; sa MM200 suit avec retard
    mm_hausse = [c * 0.9 for c in hausse]
    k, _, _ = parcours(hausse + [150] * 15, "D", mm200=mm_hausse + [170] * 15)
    verifier("D : un gagnant à -25 % de son plus haut est gardé, même sous sa MM200", k is None)
    k, motif, _ = parcours(hausse + [150, 139], "D", mm200=mm_hausse + [170, 170])
    verifier("D : un gagnant à -30 % de son plus haut est vendu", k == len(hausse) + 1 and "gagnant" in motif)
    k, motif, part = parcours(list(range(101, 152)), "E", mm200=90)
    verifier("E : vente de 30 % de la ligne à +50 %", k == 49 and part == 0.3 and "bénéfice" in motif)
    etat = rv.EtatLigne(prix_entree=100, plus_haut=160, partielle_faite=True)
    verifier("E : la vente partielle n'a lieu qu'une fois", rv.decider(etat, 160, 0.6, config.REGLES_VENTE["E"])[0] is None)
    etat = rv.EtatLigne(prix_entree=100, plus_haut=100)
    verifier("Stop à -15 % actif quelle que soit la règle",
             all("stop" in (rv.decider(etat, 84, -0.16, config.REGLES_VENTE[c])[0] or "") for c in config.REGLES_VENTE))

    # même décision dans le backtest et dans le portefeuille fictif, sur les mêmes cours
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = db.connexion(tmp / "rv.db")
        tickers = ["AAPL", "MSFT", "NVDA", "JPM", "ASML.AS", "MC.PA", "SAP.DE", "TTE.PA"]
        n = 1000
        for i, t in enumerate(tickers):
            db.upsert_prix(conn, t, prix_simules(t, n, np.random.default_rng(700 + i).uniform(0.1, 0.6), 700 + i))
        dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
        for nom, val in {"VIX": np.full(n, 15.0), "SP500": np.linspace(3000, 5000, n), "STOXX50": np.linspace(3000, 4500, n),
                         "EURUSD": 1.10 + 0.04 * np.sin(np.arange(n) / 90), "EURDKK": np.full(n, 7.46),
                         "BENCH_US": np.linspace(300, 500, n), "BENCH_EU": np.linspace(30, 45, n)}.items():
            db.upsert_macro(conn, nom, "test", pd.Series(val, index=dates))
        d = bt.preparer_donnees(conn)
        ancienne = config.REGLE_VENTE
        compares, accords = 0, 0
        for code in ("A", "C", "E"):
            config.REGLE_VENTE = code
            run = bt.simuler(d, {"candidat_tech": 55, "vente": code}, bt.debut_par_defaut(d))
            o = run["ordres"]
            # Urgence et écrêtage dépendent de tout le portefeuille : testés à part (section 8)
            ventes = o[(o["sens"] == "vente") & ~o["motif"].str.contains("Urgence|écrêtage")]
            for _, v in ventes.head(6).iterrows():
                achat = o[(o["ticker"] == v["ticker"]) & (o["sens"] == "achat") & (o["date"] < v["date"])].iloc[-1]
                if ((o["ticker"] == v["ticker"]) & (o["sens"] == "vente") & (o["date"] > achat["date"])
                        & (o["date"] < v["date"])).any():
                    continue  # une vente partielle a déjà eu lieu : cas couvert par les tests ci-dessus
                c2 = sqlite3.connect(":memory:")
                conn.backup(c2)
                pf.initialiser(c2, forcer=True)
                jour_achat = achat["date"].strftime("%Y-%m-%d")
                pru = achat["prix"] / achat["taux"] * (1 + achat["frais"])
                c2.execute("INSERT INTO positions VALUES ('fictif', ?, ?, 10, ?, ?, 60)",
                           (v["ticker"], v["poche"], pru, jour_achat))
                c2.execute("INSERT INTO transactions (portefeuille, date, ticker, poche, sens, quantite, prix_local, motif) "
                           "VALUES ('fictif', ?, ?, ?, 'achat', 10, ?, 'test')", (jour_achat, v["ticker"], v["poche"], achat["prix"]))
                jour_d = v["decision"].strftime("%Y-%m-%d")
                veille = d["dates"][d["dates"].searchsorted(v["decision"]) - 1].strftime("%Y-%m-%d")
                live_jour = pf.motif_vente(c2, "fictif", v["ticker"], None, jour_d)[0]
                live_veille = pf.motif_vente(c2, "fictif", v["ticker"], None, veille)[0]
                compares += 1
                accords += (live_jour is not None and live_jour.split(" ")[0] == v["motif"].split(" ")[0]
                            and (live_veille is None or veille <= jour_achat))
                c2.close()
        config.REGLE_VENTE = ancienne
        verifier("Backtest et portefeuille fictif : même vente, le même jour, pour la même raison",
                 compares >= 5 and accords == compares, f"{accords}/{compares} ventes identiques")
        conn.close()


def test_reseau() -> None:
    import requests

    from modules import collecte

    print("\n11. Sources réelles (connexion internet)")
    for t in ("AAPL", "ASML.AS"):
        try:
            df = collecte._historique_yahoo(t, (pd.Timestamp.today() - pd.Timedelta(days=30)).strftime("%Y-%m-%d"))
            verifier(f"Yahoo Finance : cours de {t}", not df.empty, f"{len(df)} jours, dernier {df.index[-1].date()}")
        except Exception as e:  # noqa: BLE001
            verifier(f"Yahoo Finance : cours de {t}", False, str(e))
    try:
        infos = collecte._infos_yahoo("MSFT")
        verifier("Yahoo Finance : fondamentaux de MSFT", "trailingPE" in infos or "forwardPE" in infos)
    except Exception as e:  # noqa: BLE001
        verifier("Yahoo Finance : fondamentaux de MSFT", False, str(e))
    try:
        s = collecte._serie_fred("FEDFUNDS")
        verifier("FRED : taux de la Fed", not s.empty, f"dernier {s.iloc[-1]:.2f} % ({s.index[-1].date()})")
    except Exception as e:  # noqa: BLE001
        verifier("FRED : taux de la Fed", False, str(e))
    try:
        s = collecte._serie_bce(config.MACRO_BCE["BCE_DEPOT"])
        verifier("BCE : taux de dépôt", not s.empty, f"dernier {s.iloc[-1]:.2f} % ({s.index[-1].date()})")
    except Exception as e:  # noqa: BLE001
        verifier("BCE : taux de dépôt", False, str(e))
    try:
        r = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        modeles = [m["name"] for m in r.json().get("models", [])]
        verifier("Ollama joignable (optionnel, pour --these)", True, ", ".join(modeles) or "aucun modèle")
        verifier(f"Modèle {config.OLLAMA_MODELE} installé", config.OLLAMA_MODELE in modeles)
    except Exception:  # noqa: BLE001
        verifier("Ollama joignable (optionnel, pour --these)", False, "lance l'app Ollama")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--reseau", action="store_true", help="teste aussi les sources de données réelles")
    args = p.parse_args()

    print("Vérification de l'algorithme")
    print(f"  Python {sys.version.split()[0]} · pandas {pd.__version__} · numpy {np.__version__}")
    test_hors_ligne()
    test_portefeuille()
    test_garde_fous()
    test_backtest()
    test_regles_vente()
    if args.reseau:
        test_reseau()

    echecs = [r for r in RESULTATS if not r[1]]
    print(f"\n{'✅' if not echecs else '⚠️'}  {len(RESULTATS) - len(echecs)}/{len(RESULTATS)} vérifications réussies")
    sys.exit(1 if echecs else 0)
