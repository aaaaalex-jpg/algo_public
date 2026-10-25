"""
Configuration centrale de l'algorithme.

C'est le SEUL fichier à modifier pour changer l'univers d'actions,
les pondérations ou les seuils. Le reste du code lit tout ici.
"""
from pathlib import Path

# ---------------------------------------------------------------------------
# Chemins (tout reste dans le dossier du projet, sur ton Mac)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAPPORTS_DIR = BASE_DIR / "rapports"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "algo.db"

# Historique de prix téléchargé au premier lancement (servira au backtest
# walk-forward 2010-2024 de la phase 7)
DATE_DEBUT_HISTORIQUE = "2010-01-01"

# ---------------------------------------------------------------------------
# Univers d'actions
#   region : "US" -> poche CTO, "EU" -> poche PEA (éligibilité à confirmer
#            auprès de ton courtier : siège dans l'UE / l'EEE)
#   ia     : True si l'entreprise est fortement exposée au thème IA
# ---------------------------------------------------------------------------
UNIVERS = {
    # ----------------------------- États-Unis -----------------------------
    "AAPL":  {"nom": "Apple",              "region": "US", "secteur": "Tech",             "ia": False},
    "MSFT":  {"nom": "Microsoft",          "region": "US", "secteur": "Tech",             "ia": True},
    "GOOGL": {"nom": "Alphabet",           "region": "US", "secteur": "Tech",             "ia": True},
    "AMZN":  {"nom": "Amazon",             "region": "US", "secteur": "Tech",             "ia": True},
    "META":  {"nom": "Meta Platforms",     "region": "US", "secteur": "Tech",             "ia": True},
    "ORCL":  {"nom": "Oracle",             "region": "US", "secteur": "Tech",             "ia": True},
    "CRM":   {"nom": "Salesforce",         "region": "US", "secteur": "Tech",             "ia": False},
    "ADBE":  {"nom": "Adobe",              "region": "US", "secteur": "Tech",             "ia": False},
    "NVDA":  {"nom": "Nvidia",             "region": "US", "secteur": "Semi-conducteurs", "ia": True},
    "AVGO":  {"nom": "Broadcom",           "region": "US", "secteur": "Semi-conducteurs", "ia": True},
    "AMD":   {"nom": "AMD",                "region": "US", "secteur": "Semi-conducteurs", "ia": True},
    "QCOM":  {"nom": "Qualcomm",           "region": "US", "secteur": "Semi-conducteurs", "ia": False},
    "AMAT":  {"nom": "Applied Materials",  "region": "US", "secteur": "Semi-conducteurs", "ia": False},
    "LRCX":  {"nom": "Lam Research",       "region": "US", "secteur": "Semi-conducteurs", "ia": False},
    "MU":    {"nom": "Micron",             "region": "US", "secteur": "Semi-conducteurs", "ia": True},
    "JPM":   {"nom": "JPMorgan Chase",     "region": "US", "secteur": "Finance",          "ia": False},
    "V":     {"nom": "Visa",               "region": "US", "secteur": "Finance",          "ia": False},
    "MA":    {"nom": "Mastercard",         "region": "US", "secteur": "Finance",          "ia": False},
    "UNH":   {"nom": "UnitedHealth",       "region": "US", "secteur": "Santé",            "ia": False},
    "LLY":   {"nom": "Eli Lilly",          "region": "US", "secteur": "Santé",            "ia": False},
    "JNJ":   {"nom": "Johnson & Johnson",  "region": "US", "secteur": "Santé",            "ia": False},
    "PG":    {"nom": "Procter & Gamble",   "region": "US", "secteur": "Consommation",     "ia": False},
    "KO":    {"nom": "Coca-Cola",          "region": "US", "secteur": "Consommation",     "ia": False},
    "COST":  {"nom": "Costco",             "region": "US", "secteur": "Consommation",     "ia": False},
    "XOM":   {"nom": "ExxonMobil",         "region": "US", "secteur": "Énergie",          "ia": False},
    "CVX":   {"nom": "Chevron",            "region": "US", "secteur": "Énergie",          "ia": False},
    "NEE":   {"nom": "NextEra Energy",     "region": "US", "secteur": "Énergie",          "ia": False},
    "CAT":   {"nom": "Caterpillar",        "region": "US", "secteur": "Industrie",        "ia": False},
    "GE":    {"nom": "GE Aerospace",       "region": "US", "secteur": "Industrie",        "ia": False},
    # ------------------------------- Europe -------------------------------
    "ASML.AS":   {"nom": "ASML",               "region": "EU", "secteur": "Semi-conducteurs", "ia": True},
    "IFX.DE":    {"nom": "Infineon",           "region": "EU", "secteur": "Semi-conducteurs", "ia": False},
    "STMPA.PA":  {"nom": "STMicroelectronics", "region": "EU", "secteur": "Semi-conducteurs", "ia": False},
    "SAP.DE":    {"nom": "SAP",                "region": "EU", "secteur": "Tech",             "ia": True},
    "CAP.PA":    {"nom": "Capgemini",          "region": "EU", "secteur": "Tech",             "ia": False},
    "DSY.PA":    {"nom": "Dassault Systèmes",  "region": "EU", "secteur": "Tech",             "ia": False},
    "MC.PA":     {"nom": "LVMH",               "region": "EU", "secteur": "Luxe",             "ia": False},
    "RMS.PA":    {"nom": "Hermès",             "region": "EU", "secteur": "Luxe",             "ia": False},
    "OR.PA":     {"nom": "L'Oréal",            "region": "EU", "secteur": "Consommation",     "ia": False},
    "TTE.PA":    {"nom": "TotalEnergies",      "region": "EU", "secteur": "Énergie",          "ia": False},
    "ENR.DE":    {"nom": "Siemens Energy",     "region": "EU", "secteur": "Énergie",          "ia": False},
    "IBE.MC":    {"nom": "Iberdrola",          "region": "EU", "secteur": "Énergie",          "ia": False},
    "SU.PA":     {"nom": "Schneider Electric", "region": "EU", "secteur": "Industrie",        "ia": False},
    "AIR.PA":    {"nom": "Airbus",             "region": "EU", "secteur": "Industrie",        "ia": False},
    "SAF.PA":    {"nom": "Safran",             "region": "EU", "secteur": "Industrie",        "ia": False},
    "SIE.DE":    {"nom": "Siemens",            "region": "EU", "secteur": "Industrie",        "ia": False},
    "AI.PA":     {"nom": "Air Liquide",        "region": "EU", "secteur": "Industrie",        "ia": False},
    "DG.PA":     {"nom": "Vinci",              "region": "EU", "secteur": "Industrie",        "ia": False},
    "SAN.PA":    {"nom": "Sanofi",             "region": "EU", "secteur": "Santé",            "ia": False},
    "NOVO-B.CO": {"nom": "Novo Nordisk",       "region": "EU", "secteur": "Santé",            "ia": False, "devise": "DKK"},
    "BNP.PA":    {"nom": "BNP Paribas",        "region": "EU", "secteur": "Finance",          "ia": False},
    "ALV.DE":    {"nom": "Allianz",            "region": "EU", "secteur": "Finance",          "ia": False},
}


def poche(ticker: str) -> str:
    """PEA pour les actions européennes, CTO pour les américaines."""
    return "PEA" if UNIVERS[ticker]["region"] == "EU" else "CTO"


def devise(ticker: str) -> str:
    """Devise de cotation : USD pour les US, EUR pour l'Europe (sauf exception indiquée)."""
    info = UNIVERS[ticker]
    return info.get("devise") or ("USD" if info["region"] == "US" else "EUR")


# ---------------------------------------------------------------------------
# Données macro
# ---------------------------------------------------------------------------
# Séries Yahoo Finance (cours de clôture quotidiens)
MACRO_YAHOO = {
    "VIX": "^VIX",          # indice de volatilité ("indice de la peur")
    "US10Y": "^TNX",        # taux US à 10 ans (en %)
    "EURUSD": "EURUSD=X",   # taux de change euro / dollar
    "SP500": "^GSPC",       # indice S&P 500
    "STOXX50": "^STOXX50E", # indice Euro Stoxx 50
    "EURDKK": "EURDKK=X",   # euro / couronne danoise (pour Novo Nordisk)
    "BENCH_US": "SPY",      # ETF S&P 500, dividendes inclus (référence de la poche CTO)
    "BENCH_EU": "EXW1.DE",  # ETF Euro Stoxx 50, dividendes inclus (référence de la poche PEA)
}
# Séries de la Réserve fédérale de Saint-Louis (FRED), sans clé API
MACRO_FRED = {
    "FED_FUNDS": "FEDFUNDS",  # taux directeur effectif de la Fed (mensuel)
    "CPI_US": "CPIAUCSL",     # indice des prix US (pour calculer l'inflation)
}
# Séries de la Banque centrale européenne (portail de données BCE)
MACRO_BCE = {
    "BCE_DEPOT": "FM/B.U2.EUR.4F.KR.DFR.LEV",  # taux de dépôt de la BCE
    "HICP_ZE": "ICP/M.U2.N.000000.4.ANR",      # inflation zone euro (sur 1 an, en %)
}

# ---------------------------------------------------------------------------
# Fondamentaux récupérés chaque jour (instantané Yahoo Finance)
# ---------------------------------------------------------------------------
CHAMPS_FONDAMENTAUX_NUM = [
    "trailingPE", "forwardPE", "enterpriseToEbitda", "priceToSalesTrailing12Months",
    "priceToBook", "returnOnEquity", "returnOnAssets", "profitMargins",
    "operatingMargins", "revenueGrowth", "earningsGrowth", "debtToEquity",
    "currentRatio", "quickRatio", "freeCashflow", "marketCap",
]
CHAMPS_FONDAMENTAUX_TXT = ["currency", "sector", "industry"]

# ---------------------------------------------------------------------------
# Module 2 : scoring fondamental
# Chaque indicateur est classé en percentile au sein de l'univers (0 à 100).
#   "+" = plus c'est haut, mieux c'est ; "-" = plus c'est bas, mieux c'est
#   "neg_pire" = une valeur négative (pertes, fonds propres négatifs) obtient 0
# ---------------------------------------------------------------------------
PILIERS = {
    "valorisation": {
        "poids": 0.25,
        "indicateurs": {
            "forwardPE": "-",
            "trailingPE": "-",
            "enterpriseToEbitda": "-",
            "priceToSalesTrailing12Months": "-",
            "fcf_yield": "+",   # rendement du flux de trésorerie libre (FCF / capitalisation)
        },
    },
    "qualite": {
        "poids": 0.30,
        "indicateurs": {
            "returnOnEquity": "+",
            "returnOnAssets": "+",
            "profitMargins": "+",
            "operatingMargins": "+",
        },
    },
    "croissance": {
        "poids": 0.25,
        "indicateurs": {
            "revenueGrowth": "+",
            "earningsGrowth": "+",
        },
    },
    "bilan": {
        "poids": 0.20,
        "indicateurs": {
            "debtToEquity": "-",
            "currentRatio": "+",
            "quickRatio": "+",
        },
    },
}
# Indicateurs pour lesquels une valeur négative est la pire possible
NEGATIF_PIRE = {"forwardPE", "trailingPE", "enterpriseToEbitda", "debtToEquity"}

# Bonus sectoriel (points ajoutés au score fondamental, plafonné à 100)
BONUS_SECTEURS = {"Tech": 5, "Semi-conducteurs": 5, "Énergie": 5}
BONUS_IA = 3  # points supplémentaires si l'entreprise est marquée "ia"

# ESG : pas de source gratuite fiable aujourd'hui -> poids 0 (à brancher plus tard)
POIDS_ESG = 0.0

# Si moins de 60 % des indicateurs sont disponibles, le score est ramené vers 50
COMPLETUDE_MIN = 0.6

# ---------------------------------------------------------------------------
# Module 3 : timing technique
# ---------------------------------------------------------------------------
NB_JOURS_MIN = 260            # environ 1 an de cotations nécessaire
POIDS_TECHNIQUE = {"tendance": 0.40, "repli": 0.35, "momentum": 0.25}
PLAFOND_SANS_TENDANCE = 35    # règle d'or : pas de tendance haussière -> score plafonné

# ---------------------------------------------------------------------------
# Score combiné provisoire (modules 2 + 3)
# Ton moteur final : conviction 45 %, timing 30 %, sentiment 15 %, hedge 10 %.
# Tant que sentiment et hedge n'existent pas, on renormalise 45/30 -> 60/40.
# ---------------------------------------------------------------------------
POIDS_COMBINE = {"fondamental": 0.60, "technique": 0.40}

SEUILS = {
    "candidat_fond": 65,   # fondamental minimum pour être "candidat"
    "candidat_tech": 60,   # timing minimum pour être "candidat"
    "eviter_fond": 45,     # en dessous : "à éviter pour l'instant"
}

# ---------------------------------------------------------------------------
# Contexte de marché ("cash is a position")
# ---------------------------------------------------------------------------
VIX_CALME = 20
VIX_STRESS = 28
ALERTE_EURUSD_5J = 2.0   # alerte si l'euro/dollar bouge de plus de 2 % en 5 jours

# ---------------------------------------------------------------------------
# Thèse rédigée par ton LLM local (optionnel : option --these)
# ---------------------------------------------------------------------------
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODELE = "qwen3.8:27b"
NB_THESES = 3

# ---------------------------------------------------------------------------
# Portefeuilles
#   "reel"   : ce que tu possèdes vraiment (tu enregistres tes achats/ventes)
#   "fictif" : géré automatiquement par l'algo avec les mêmes montants de départ,
#              pour mesurer en conditions réelles ce qu'il aurait fait
# ---------------------------------------------------------------------------
CAPITAL_INITIAL = {"CTO": 10_000.0, "PEA": 10_000.0}   # en euros, au démarrage (exemple : mets tes montants)

# Taille des positions (en % de la valeur totale de la poche)
#   score combiné 60 -> 3 %, 70 -> 6 %, 80 et plus -> 9 %
TAILLE_BASE = 0.06
TAILLE_MAX = 0.10           # jamais plus de 10 % de la poche sur une seule action
MONTANT_MIN_ORDRE = 500.0   # en euros : en dessous, on ne passe pas l'ordre
MAX_POSITIONS_PAR_POCHE = 12

# ---------------------------------------------------------------------------
# Budget d'ordres (achats + ventes sur 365 jours glissants, par portefeuille)
# ---------------------------------------------------------------------------
BUDGET_TRADES_AN = 50       # budget normal
SEUIL_ALERTE_BUDGET = 40    # à partir de 40 ordres, on devient plus sélectif :
SCORE_FIN_BUDGET = 75       #   achat seulement si score combiné >= 75
# Au-delà des 50 ordres, uniquement en cas de forte nécessité :
#   - gros risque  : les ventes de protection (rupture de thèse, stop, garde-fous)
#                    ne sont JAMAIS bloquées par le budget
#   - grosse opportunité : achat autorisé si score combiné >= SCORE_EXCEPTIONNEL,
#                    dans la limite de RESERVE_EXCEPTIONNELLE ordres en plus
SCORE_EXCEPTIONNEL = 85
RESERVE_EXCEPTIONNELLE = 10  # plafond absolu des achats : 50 + 10 = 60 ordres/an

# ---------------------------------------------------------------------------
# Règles de vente ("diamond hands intelligent")
# MM200 = moyenne des 200 derniers cours de clôture (environ 10 mois de Bourse).
# Le stop à -15 % et les garde-fous s'appliquent en plus, quelle que soit la règle.
#   marge          : vente si le cours passe X % sous sa MM200...
#   confirmation   : ... pendant N séances d'affilée (1 = dès la première)
#   seuil_gagnant  : une action qui a pris +X % depuis l'achat devient un "gagnant" :
#   recul_gagnant  :   on ne la vend plus que si elle perd X % depuis son plus haut
#   partielle_seuil / partielle_part : vente d'une partie de la ligne (une seule fois)
#                    quand elle atteint +X % depuis l'achat
# ---------------------------------------------------------------------------
REGLES_VENTE = {
    "A": {"nom": "3 % sous la MM200", "marge": 0.03, "confirmation": 1},
    "B": {"nom": "8 % sous la MM200", "marge": 0.08, "confirmation": 1},
    "C": {"nom": "3 % sous la MM200 pendant 10 séances", "marge": 0.03, "confirmation": 10},
    "D": {"nom": "C + gagnants (+50 %) vendus à -30 % de leur plus haut", "marge": 0.03, "confirmation": 10,
          "seuil_gagnant": 0.50, "recul_gagnant": 0.30},
    "E": {"nom": "D + vente de 30 % de la ligne à +50 %", "marge": 0.03, "confirmation": 10,
          "seuil_gagnant": 0.50, "recul_gagnant": 0.30, "partielle_seuil": 0.50, "partielle_part": 0.30},
}
REGLE_VENTE = "A"           # règle appliquée par le portefeuille fictif et les alertes du réel
# Règles mises en concours par le backtest walk-forward. Concours du 24/10/2026 : avec les
# 5 règles, +16,2 %/an hors échantillon ; avec A seule, +18,7 %/an. Trop de choix = le
# walk-forward court après le bruit. On teste donc A seule ; mettre ["A", "B", "C", "D", "E"]
# pour refaire le concours (par exemple dans un an, avec plus de recul).
VENTES_TESTEES = ["A"]

# ---------------------------------------------------------------------------
# Garde-fous (limiter les pertes)
# ---------------------------------------------------------------------------
# 1. Stop par ligne : vente si une action perd 15 % par rapport à son prix de revient
#    (change compris pour les actions US), même si la thèse semble intacte.
STOP_LIGNE = 0.15

# 2. Perte maximale du portefeuille ("drawdown") : recul depuis son plus haut,
#    apports exclus. Trois niveaux, du plus léger au plus fort :
DRAWDOWN_VIGILANCE = 0.05   # -5 %  : achats seulement si score >= 75, tailles divisées par 2
DRAWDOWN_PROTECTION = 0.08  # -8 %  : plus aucun achat, on laisse le cash au repos
DRAWDOWN_URGENCE = 0.10     # -10 % : on vend les lignes qui perdent 8 % ou plus,
PERTE_LIGNE_URGENCE = 0.08  #         et celles classées « À éviter »
SCORE_VIGILANCE = 75
FACTEUR_TAILLE_VIGILANCE = 0.5
#    Après Protection ou Urgence : pause d'un mois (21 séances) sans achat, puis on repart
#    du niveau du jour, avec une reprise prudente (Vigilance) pendant encore 21 séances.
#    Sans cette pause, un portefeuille revenu en cash resterait bloqué pour toujours.
PAUSE_GARDE_FOU = 21

# 3. Diversification : jamais plus de 30 % d'une poche dans un même secteur
MAX_SECTEUR = 0.30

# 3 bis. Écrêtage : les limites ci-dessus ne valent qu'à l'achat ; ensuite, un gagnant peut grossir
#    sans limite (Micron a atteint 43 % du CTO en juin 2026, avant une chute de -39 %). Si une ligne
#    dépasse ECRETAGE_SEUIL de sa poche, on en revend une partie pour la ramener à ECRETAGE_CIBLE.
#    Backtest (24/10/2026) : chute des semi-conducteurs 2026 ramenée de -22 % à -13 %, pour -0,3 pt/an.
#    L'écrêtage est une vente de protection : jamais bloqué par le budget d'ordres.
ECRETAGE_SEUIL = 0.20
ECRETAGE_CIBLE = 0.15

# 4. Entrées progressives : au plus 3 nouveaux achats par jour et par portefeuille
#    (évite de tout investir le même jour, au mauvais moment)
MAX_ACHATS_PAR_JOUR = 3

# 5. Alerte si le portefeuille perd plus de 3 % en une séance
ALERTE_PERTE_JOUR = 0.03

# Coûts simulés (portefeuille fictif) : frais de courtage, change, glissement de prix
FRAIS_COURTAGE = {"CTO": 0.002, "PEA": 0.002}   # 0,2 % par ordre
FRAIS_CHANGE = 0.001        # 0,1 % pour convertir des euros en dollars (CTO)
GLISSEMENT = 0.001          # on achète 0,1 % plus cher / vend 0,1 % moins cher que l'ouverture

# Taux de change de secours si la donnée manque (unités de devise pour 1 euro)
TAUX_DEFAUT = {"USD": 1.10, "DKK": 7.46}

# Référence de chaque poche (pour savoir si l'algo bat le marché)
BENCHMARK = {"CTO": ("BENCH_US", "USD", "SP500"), "PEA": ("BENCH_EU", "EUR", "STOXX50")}
