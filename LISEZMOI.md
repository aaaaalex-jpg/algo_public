# Algorithme de recommandation — Phases 1 à 3, portefeuilles, garde-fous et backtest

Système 100 % local : il collecte les données chaque matin, note chaque action et
produit un rapport. **Il ne passe aucun ordre** : il propose, tu décides.

## Ce qui est en place

| Module | Rôle | Fichier |
|---|---|---|
| 1. Data Pipeline | Prix de 51 actions US/EU, macro (VIX, taux Fed/BCE, inflation US/ZE, taux US 10 ans, EUR/USD, indices), fondamentaux du jour — tout dans SQLite | `modules/collecte.py`, `modules/db.py` |
| 2. Scoring fondamental | Note /100 : valorisation 25 %, qualité 30 %, croissance 25 %, bilan 20 % + bonus Tech / Semi-conducteurs / Énergie / IA | `modules/scoring_fondamental.py` |
| 3. Timing technique | Tendance, repli, momentum. Règle d'or : pas de tendance haussière → score plafonné à 35 | `modules/timing_technique.py` |
| Rapport du matin | Score combiné (fondamental 60 % + timing 40 %), signal, raisons, zone d'entrée, niveau d'invalidation, contexte de marché, alerte EUR/USD | `modules/rapport.py` |
| Portefeuilles | **Réel** (tes saisies) et **fictif** (géré par l'algo avec le même capital), comparés à leur référence | `modules/portefeuille.py` |
| Planification | Lancement automatique du lundi au vendredi à 6 h 30 | `planification/` |

Tous les réglages (univers, pondérations, seuils) sont dans **`config.py`**.

## Installation

```bash
conda activate trading
cd ~/trading/algo
python verifier.py              # tests hors ligne
python verifier.py --reseau     # tests des vraies sources (Yahoo, FRED, BCE, Ollama)
```

## Utilisation

```bash
python main.py collecte         # 1re fois : télécharge l'historique depuis 2010 (5 à 10 min)
python main.py score --ouvrir   # calcule les scores et ouvre le rapport
python main.py tout --ouvrir    # les deux à la suite
python main.py score --these    # + thèse rédigée par ton LLM local (Ollama doit être lancé)
python main.py statut           # état de la base
```

Rapport du jour : `rapports/dernier_rapport.html` (historique : `rapports/rapport_AAAA-MM-JJ.html`).

## Ton portefeuille

```bash
python main.py init-portefeuille          # crée le réel et le fictif (montants de CAPITAL_INITIAL dans config.py)
python main.py portefeuille               # affiche les deux
python main.py achat ASML.AS 2 612.40 --frais 1.5    # enregistre un achat réel (prix en devise de cotation)
python main.py vente ASML.AS 2 640 --frais 1.5       # enregistre une vente réelle
python main.py apport PEA 500             # ajoute 500 € (au réel ET au fictif, pour rester comparables)
```

**Le portefeuille fictif** applique tes règles tout seul, chaque matin :
- achète les « Candidats à étudier » : taille de 3 à 9 % de la poche selon le score, 10 % maximum,
  12 positions maximum par poche, 500 € minimum par ordre ;
- **cash is a position** : aucun achat quand le contexte de marché est « Défensif » ;
- vend sur **rupture de thèse** (score fondamental < 45), selon la **règle de vente** active (`REGLE_VENTE` dans `config.py`, aujourd'hui A : cours 3 % sous la MM200), ou sur un **garde-fou** (voir plus bas). Les 5 règles A à E sont décrites dans `config.py` ; le concours du 24/10/2026 a retenu A ;
- **budget de 50 ordres par an**, avec dérogations pour les cas de forte nécessité (voir plus bas) ;
- les ordres sont exécutés à **l'ouverture de la séance suivante**, avec frais (0,2 %), change (0,1 % sur
  le CTO) et glissement de prix (0,1 %) : il ne profite jamais d'un prix qu'il ne pouvait pas connaître.

Chaque poche est comparée à sa **référence** (ETF S&P 500 pour le CTO, ETF Euro Stoxx 50 pour le PEA,
dividendes inclus). Le dimensionnement par Kelly fractionnel arrivera après le backtest, qui fournira les
probabilités de gain nécessaires.

## Budget d'ordres

- **50 ordres par an** (achats + ventes, sur 12 mois glissants), par portefeuille.
- À partir de **40**, l'algo devient sélectif : achat seulement si le score est ≥ 75.
- Au-delà de **50**, uniquement en cas de forte nécessité :
  - **gros risque** : les ventes de protection ne sont **jamais** bloquées par le budget ;
  - **grosse opportunité** : achat autorisé si le score est ≥ 85, dans la limite de 10 ordres en plus (60 au maximum).
- Les ordres hors budget sont marqués comme tels dans le rapport.

## Garde-fous

| Garde-fou | Règle |
|---|---|
| Stop par ligne | Vente si une action perd **15 %** depuis l'achat (change compris), même si la thèse semble intacte |
| Recul du portefeuille −5 % | **Vigilance** : achats seulement si score ≥ 75, tailles divisées par 2 |
| Recul du portefeuille −8 % | **Protection** : plus aucun achat |
| Recul du portefeuille −10 % | **Urgence** : plus aucun achat, vente des lignes à −8 % ou plus et de celles classées « À éviter » |
| Écrêtage | Une ligne qui dépasse **20 %** de sa poche est ramenée à **15 %** (vente partielle, jamais bloquée par le budget) |
| Après Protection ou Urgence | **Pause d'un mois** (21 séances), puis on repart du niveau du jour avec une **reprise prudente** (tailles ÷ 2) pendant un mois |
| Diversification | Jamais plus de **30 %** d'une poche dans un même secteur |
| Entrées progressives | Au plus **3 nouveaux achats par jour** |
| Alerte | Si le portefeuille perd plus de **3 %** en une séance |

Le recul est mesuré depuis le plus haut du portefeuille, **apports exclus** (un apport n'est pas un gain).
Le portefeuille fictif applique ces règles tout seul ; pour ton portefeuille réel, le rapport t'alerte
et adapte les montants suggérés, mais c'est toi qui décides. Tous les seuils sont dans `config.py`.

## Backtest walk-forward

```bash
python main.py backtest --ouvrir                              # environ 1 à 3 minutes
python main.py backtest --apport-cto 400 --apport-pea 350     # avec des apports mensuels simulés
```

Rejoue les règles jour par jour depuis 2011, exactement comme le portefeuille fictif : décision le soir,
exécution le lendemain à l'ouverture, frais, garde-fous, budget d'ordres. Douze réglages sont testés ;
chaque année, celui qui a le mieux marché sur les 3 années précédentes est appliqué à l'année suivante,
qu'il n'a jamais vue. Le rapport `rapports/dernier_backtest.html` compare l'algo :
- aux **ETF de référence** (S&P 500 et Euro Stoxx 50) ;
- au **même univers acheté et conservé** : la vraie question, car l'univers est choisi aujourd'hui
  (biais du survivant) ;
- et ajoute un **Monte Carlo** (1 000 années simulées à partir des mois réels de l'algo).

Le backtest ne couvre pas le score fondamental (pas d'historique gratuit fiable des ratios).

## Automatisation

```bash
bash planification/installer.sh        # du lundi au vendredi à 6 h 30
bash planification/desinstaller.sh     # pour arrêter
```

## Les signaux

- **Candidat à étudier** : fondamental ≥ 65, timing ≥ 60, tendance haussière.
- **Qualité — attendre un meilleur point d'entrée** : bonne entreprise, mauvais moment.
- **Neutre** : rien de remarquable.
- **À éviter pour l'instant** : fondamental faible, ou pas de tendance haussière.

## Limites à connaître

- **Les fondamentaux sont un instantané du jour** (Yahoo Finance). Ils ne peuvent pas être
  backtestés honnêtement sur le passé. Le système enregistre chaque jour un relevé : au bout de
  quelques mois, on pourra mesurer si le score fondamental a une vraie valeur prédictive.
- **Comparaison tous secteurs confondus** : une banque et un éditeur de logiciels n'ont pas les
  mêmes ratios « normaux ». Amélioration prévue : classement à l'intérieur de chaque secteur.
- **ESG** : pas de source gratuite fiable, poids 0 pour l'instant.
- **Éligibilité PEA** : déduite du pays (Europe → PEA). À confirmer avec ton courtier.
- Les données viennent d'internet (Yahoo, FRED, BCE) ; les calculs et la base restent sur ton Mac.

## Prochaines phases

4. Sentiment (FinBERT sur flux RSS) · 5. Couverture EUR/USD (affichage de l'effet de change) ·
6. Moteur de décision (pondération 45/30/15/10, Kelly fractionnel) · tableau de bord.

*Outil d'aide à la décision personnel. Ce n'est pas un conseil en investissement.*
