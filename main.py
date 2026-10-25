"""
Point d'entrée de l'algorithme.

À lancer depuis le dossier algo, avec l'environnement conda "trading" actif.

Routine quotidienne (faite automatiquement par le planificateur) :
    python main.py tout [--these] [--ouvrir]   collecte + scores + rapport + portefeuilles

Commandes séparées :
    python main.py init                    crée la base de données
    python main.py collecte                télécharge prix, macro et fondamentaux
    python main.py score [--ouvrir]        calcule les scores et produit le rapport
    python main.py statut                  état de la base

Ton portefeuille :
    python main.py init-portefeuille [--forcer]          crée (ou remet à zéro) le réel et le fictif
    python main.py portefeuille                          affiche les deux portefeuilles
    python main.py achat ASML.AS 2 612.40 [--frais 1.5] [--date 2026-10-26]
    python main.py vente ASML.AS 2 640.00 [--frais 1.5]
    python main.py apport PEA 500                        ajoute 500 € au PEA (réel et fictif)

Backtest :
    python main.py backtest [--ouvrir]                   rejoue les règles depuis 2011 (walk-forward)
    python main.py backtest --apport-cto 400 --apport-pea 350   avec des apports mensuels
"""
import argparse
import logging
import sys
import time
from pathlib import Path

# permet de lancer "python main.py" depuis n'importe où
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from modules import backtest, collecte, db, portefeuille, rapport  # noqa: E402


def configurer_logs() -> logging.Logger:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("algo")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        fichier = logging.FileHandler(config.LOGS_DIR / "algo.log", encoding="utf-8")
        fichier.setFormatter(fmt)
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(fichier)
        log.addHandler(console)
    return log


def cmd_collecte(conn, log, sans_fondamentaux: bool) -> None:
    t0 = time.time()
    log.info("▶ Collecte des prix")
    b1 = collecte.collecter_prix(conn)
    log.info("▶ Collecte des données macro")
    b2 = collecte.collecter_macro(conn)
    erreurs = b1["erreurs"] + b2["erreurs"]
    if not sans_fondamentaux:
        log.info("▶ Collecte des fondamentaux")
        b3 = collecte.collecter_fondamentaux(conn)
        erreurs += b3["erreurs"]
    log.info(f"✔ Collecte terminée en {time.time() - t0:.0f} s — {len(erreurs)} erreur(s)")
    if erreurs:
        log.info("  Erreurs (non bloquantes) : " + " | ".join(erreurs[:10]))


def cmd_statut(conn) -> None:
    s = db.statistiques(conn)
    print("\nÉtat de la base :", config.DB_PATH)
    print(f"  Prix          : {s['prix_lignes']:,} lignes, {s['prix_actions']} actions, dernière date {s['prix_derniere_date']}")
    print(f"  Macro         : {s['macro_series']} séries")
    print(f"  Fondamentaux  : {s['fondamentaux_actions']} actions, dernier relevé {s['fondamentaux_dernier_releve']}")
    print(f"  Scores        : dernière date {s['scores_derniere_date']}")
    print(f"  Dernière exécution : {s['derniere_execution']}\n")


def construire_parseur() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Algorithme local de recommandation d'investissement")
    sp = p.add_subparsers(dest="commande", required=True)

    sp.add_parser("init", help="crée la base de données")
    sp.add_parser("statut", help="état de la base")
    c = sp.add_parser("collecte", help="télécharge les données")
    c.add_argument("--sans-fondamentaux", action="store_true")
    for nom in ("score", "tout"):
        s = sp.add_parser(nom, help="scores + rapport" + (" (après collecte)" if nom == "tout" else ""))
        s.add_argument("--these", action="store_true", help="thèse rédigée par le LLM local (Ollama)")
        s.add_argument("--ouvrir", action="store_true", help="ouvre le rapport dans le navigateur")
        if nom == "tout":
            s.add_argument("--sans-fondamentaux", action="store_true")

    i = sp.add_parser("init-portefeuille", help="crée les portefeuilles réel et fictif")
    i.add_argument("--forcer", action="store_true", help="remet à zéro (efface l'historique des portefeuilles)")
    sp.add_parser("portefeuille", help="affiche les portefeuilles")
    for sens in ("achat", "vente"):
        a = sp.add_parser(sens, help=("enregistre un achat" if sens == "achat" else "enregistre une vente") + " passé chez ton courtier")
        a.add_argument("ticker", help="ex. ASML.AS, AAPL")
        a.add_argument("quantite", type=float)
        a.add_argument("prix", type=float, help="prix unitaire dans la devise de cotation")
        a.add_argument("--frais", type=float, default=0.0, help="frais de courtage en euros")
        a.add_argument("--date", default=None, help="AAAA-MM-JJ (par défaut : aujourd'hui)")
    ap = sp.add_parser("apport", help="ajoute de l'argent sur une poche")
    ap.add_argument("poche", choices=["CTO", "PEA"])
    ap.add_argument("montant", type=float)
    ap.add_argument("--reel-seulement", action="store_true", help="n'ajoute pas l'apport au portefeuille fictif")
    b = sp.add_parser("backtest", help="rejoue les règles sur l'historique (walk-forward)")
    b.add_argument("--debut", default=None, help="AAAA-MM-JJ (par défaut : dès que l'historique le permet)")
    b.add_argument("--apport-cto", type=float, default=0.0, help="apport mensuel simulé sur le CTO, en euros")
    b.add_argument("--apport-pea", type=float, default=0.0, help="apport mensuel simulé sur le PEA, en euros")
    b.add_argument("--ouvrir", action="store_true", help="ouvre le rapport dans le navigateur")
    return p


def main() -> int:
    args = construire_parseur().parse_args()
    log = configurer_logs()
    conn = db.connexion()
    try:
        c = args.commande
        if c == "init":
            log.info(f"✔ Base prête : {config.DB_PATH}")
        elif c == "statut":
            cmd_statut(conn)
        elif c == "collecte":
            cmd_collecte(conn, log, args.sans_fondamentaux)
        elif c == "score":
            rapport.executer(conn, avec_these=args.these, ouvrir=args.ouvrir)
        elif c == "tout":
            log.info("=" * 60 + "\nExécution complète\n" + "=" * 60)
            try:
                cmd_collecte(conn, log, args.sans_fondamentaux)
            except Exception as e:  # noqa: BLE001 - on calcule quand même avec les données existantes
                log.error(f"Collecte interrompue : {e} — calcul avec les données déjà en base")
                db.journaliser(conn, "collecte", "erreur", str(e))
            rapport.executer(conn, avec_these=args.these, ouvrir=args.ouvrir)
        elif c == "init-portefeuille":
            crees = portefeuille.initialiser(conn, forcer=args.forcer)
            if crees:
                log.info(f"✔ Portefeuilles créés : {', '.join(crees)} — capital {config.CAPITAL_INITIAL}")
            else:
                log.info("Les portefeuilles existent déjà (ajoute --forcer pour tout remettre à zéro).")
        elif c == "portefeuille":
            print(portefeuille.resume_texte(conn))
        elif c in ("achat", "vente"):
            log.info("✔ " + portefeuille.transaction_reelle(conn, c, args.ticker, args.quantite, args.prix,
                                                            jour=args.date, frais_eur=args.frais))
        elif c == "backtest":
            backtest.executer(conn, debut=args.debut, apport_cto=args.apport_cto, apport_pea=args.apport_pea,
                              ouvrir=args.ouvrir)
        elif c == "apport":
            cibles = ("reel",) if args.reel_seulement else portefeuille.PORTEFEUILLES
            portefeuille.apport(conn, args.poche, args.montant, portefeuilles=cibles)
            log.info(f"✔ Apport de {args.montant:,.2f} € enregistré sur le {args.poche} ({', '.join(cibles)})")
        return 0
    except RuntimeError as e:  # erreur "attendue" (ex. base vide) : message simple
        log.error(f"❌ {e}")
        db.journaliser(conn, args.commande, "erreur", str(e))
        return 1
    except Exception as e:  # noqa: BLE001
        log.exception(f"Erreur : {e}")
        db.journaliser(conn, args.commande, "erreur", str(e))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
