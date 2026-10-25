"""
Rapport du matin.

Combine le score fondamental (module 2) et le timing technique (module 3),
attribue un signal à chaque action, rédige les raisons, et produit :
  - un tableau dans le terminal
  - une page HTML : rapports/rapport_AAAA-MM-JJ.html (+ rapports/dernier_rapport.html)
  - l'enregistrement des scores du jour dans la base (table scores)

Les signaux sont des pistes d'étude, pas des ordres : c'est toi qui décides.
"""
import html
import json
import logging
import shutil
import webbrowser
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import config
from modules import db
from modules.scoring_fondamental import calculer_scores_fondamentaux
from modules.timing_technique import calculer_scores_techniques

log = logging.getLogger("algo")

SIGNAL_CANDIDAT = "Candidat à étudier"
SIGNAL_ATTENDRE = "Qualité — attendre un meilleur point d'entrée"
SIGNAL_NEUTRE = "Neutre"
SIGNAL_EVITER = "À éviter pour l'instant"
ORDRE_SIGNAUX = [SIGNAL_CANDIDAT, SIGNAL_ATTENDRE, SIGNAL_NEUTRE, SIGNAL_EVITER]


# ------------------------------------------------------------------ calculs --
def construire_classement(conn) -> pd.DataFrame:
    fond = db.charger_fondamentaux_recents(conn)
    prix = db.charger_prix(conn, depuis=(pd.Timestamp.today() - pd.DateOffset(years=2)).strftime("%Y-%m-%d"))
    if fond.empty and prix.empty:
        raise RuntimeError("La base est vide : lance d'abord  python main.py collecte")

    sf = calculer_scores_fondamentaux(fond) if not fond.empty else pd.DataFrame()
    st = calculer_scores_techniques(prix) if not prix.empty else pd.DataFrame()

    tickers = [t for t in config.UNIVERS if t in sf.index or t in st.index]
    res = pd.DataFrame(index=pd.Index(tickers, name="ticker"))
    res["nom"] = [config.UNIVERS[t]["nom"] for t in tickers]
    res["poche"] = [config.poche(t) for t in tickers]
    res["secteur"] = [config.UNIVERS[t]["secteur"] for t in tickers]
    res = res.join(sf, how="left").join(st, how="left")
    for c in ["score_fondamental", "score_technique", "tendance_haussiere", "entree", "invalidation"]:
        if c not in res.columns:
            res[c] = np.nan

    w = config.POIDS_COMBINE
    res["score_combine"] = (
        w["fondamental"] * res["score_fondamental"].fillna(50)
        + w["technique"] * res["score_technique"].fillna(0)
    ).round(1)
    res["signal"] = [_signal(r) for r in res.itertuples()]
    res["raisons"] = [_raisons(r) for r in res.itertuples()]

    res["_ordre"] = res["signal"].map({s: i for i, s in enumerate(ORDRE_SIGNAUX)})
    res = res.sort_values(["_ordre", "score_combine"], ascending=[True, False]).drop(columns="_ordre")
    return res


def _signal(r) -> str:
    s = config.SEUILS
    fond = r.score_fondamental if pd.notna(r.score_fondamental) else 50
    tech = r.score_technique if pd.notna(r.score_technique) else 0
    tendance_ok = bool(r.tendance_haussiere) if pd.notna(r.tendance_haussiere) else False
    if fond >= s["candidat_fond"] and tech >= s["candidat_tech"] and tendance_ok:
        return SIGNAL_CANDIDAT
    if fond >= s["candidat_fond"]:
        return SIGNAL_ATTENDRE
    if fond < s["eviter_fond"] or not tendance_ok:
        return SIGNAL_EVITER
    return SIGNAL_NEUTRE


def _pct(v) -> str:
    return f"{v * 100:+.0f} %" if pd.notna(v) else "n.d."


def _raisons(r) -> list[str]:
    """Phrases courtes qui expliquent le score (le "voilà pourquoi")."""
    x = []
    g = lambda nom: getattr(r, nom, np.nan)

    # fondamental
    if pd.notna(g("qualite")):
        if r.qualite >= 70:
            x.append(f"rentabilité élevée (ROE {_pct(g('v_returnOnEquity'))}, marge nette {_pct(g('v_profitMargins'))})")
        elif r.qualite <= 30:
            x.append("rentabilité faible par rapport à l'univers")
        if r.croissance >= 70:
            x.append(f"croissance forte (CA {_pct(g('v_revenueGrowth'))}, bénéfices {_pct(g('v_earningsGrowth'))})")
        elif r.croissance <= 30:
            x.append(f"croissance inférieure au reste de l'univers (CA {_pct(g('v_revenueGrowth'))})")
        pe = g("v_forwardPE")
        if r.valorisation >= 70:
            x.append(f"valorisation attractive (PER prévisionnel {pe:.1f})" if pd.notna(pe) else "valorisation attractive")
        elif r.valorisation <= 30:
            x.append(f"valorisation exigeante (PER prévisionnel {pe:.1f})" if pd.notna(pe) else "valorisation exigeante")
        if r.bilan <= 30:
            x.append("bilan plus endetté que la moyenne")
        elif r.bilan >= 70:
            x.append("bilan solide")
        if g("bonus") and g("bonus") > 0:
            x.append(f"bonus sectoriel +{g('bonus'):.0f} ({r.secteur}{', thème IA' if config.UNIVERS[r.Index]['ia'] else ''})")
        if g("completude") < config.COMPLETUDE_MIN:
            x.append(f"données fondamentales incomplètes ({g('completude') * 100:.0f} %)")
    else:
        x.append("pas de données fondamentales")

    # technique
    if pd.notna(g("cours")):
        if r.tendance_haussiere:
            x.append("tendance haussière confirmée (au-dessus de la MM200, MM50 > MM200)")
        else:
            x.append("pas de tendance haussière (règle d'or : on n'entre pas)")
        x.append(f"recul de {abs(r.recul_52s):.1f} % depuis le plus haut 52 sem.")
        etat_rsi = "zone de repli" if 35 <= r.rsi <= 50 else ("suracheté" if r.rsi >= 70 else ("survendu" if r.rsi <= 30 else "neutre"))
        x.append(f"RSI {r.rsi:.0f} ({etat_rsi})")
        x.append(f"performance 6 mois {r.perf_6m:+.1f} %")
    else:
        x.append("historique de cours insuffisant")
    return x


# ------------------------------------------------------- contexte de marché --
def contexte_marche(conn, jour: str | None = None) -> dict:
    """Contexte à la date de la dernière séance (jamais de donnée postérieure)."""
    ctx = {"alertes": []}
    jour = jour or conn.execute("SELECT MAX(date) FROM prix").fetchone()[0]

    def serie(nom):
        s = db.charger_macro(conn, nom)
        return s[s.index <= pd.Timestamp(jour)] if (jour and not s.empty) else s

    vix = serie("VIX")
    if not vix.empty:
        ctx["vix"] = round(float(vix.iloc[-1]), 1)

    indices_ok = []
    for nom in ("SP500", "STOXX50"):
        s = serie(nom)
        if len(s) >= 200:
            au_dessus = bool(s.iloc[-1] > s.rolling(200).mean().iloc[-1])
            ctx[f"{nom}_au_dessus_mm200"] = au_dessus
            indices_ok.append(au_dessus)

    v = ctx.get("vix")
    if indices_ok and v is not None:
        if all(indices_ok) and v < config.VIX_CALME:
            ctx["regime"] = "Favorable"
        elif (not any(indices_ok)) or v >= config.VIX_STRESS:
            ctx["regime"] = "Défensif"
        else:
            ctx["regime"] = "Neutre"
    else:
        ctx["regime"] = "Inconnu (données macro manquantes)"

    eurusd = serie("EURUSD")
    if len(eurusd) >= 6:
        ctx["eurusd"] = round(float(eurusd.iloc[-1]), 4)
        var5 = (eurusd.iloc[-1] / eurusd.iloc[-6] - 1) * 100
        ctx["eurusd_var_5j"] = round(float(var5), 2)
        if abs(var5) >= config.ALERTE_EURUSD_5J:
            sens = "hausse de l'euro (tes actions US perdent de la valeur en euros)" if var5 > 0 \
                else "baisse de l'euro (tes actions US gagnent de la valeur en euros)"
            ctx["alertes"].append(f"EUR/USD : {var5:+.2f} % en 5 jours — {sens}.")

    for nom, cle in (("FED_FUNDS", "fed"), ("BCE_DEPOT", "bce"), ("US10Y", "us10y"), ("HICP_ZE", "inflation_ze")):
        s = serie(nom)
        if not s.empty:
            ctx[cle] = round(float(s.iloc[-1]), 2)
    cpi = serie("CPI_US")
    if len(cpi) >= 13:
        ctx["inflation_us"] = round(float((cpi.iloc[-1] / cpi.iloc[-13] - 1) * 100), 2)

    if ctx["regime"] == "Défensif":
        ctx["alertes"].append("Marché défensif : « cash is a position » — accumuler les apports plutôt que forcer des entrées.")
    return ctx


# ----------------------------------------------------- thèse par LLM local --
def rediger_these(r, modele: str | None = None) -> str:
    """Demande à ton LLM local (Ollama) de rédiger une thèse d'investissement."""
    modele = modele or config.OLLAMA_MODELE
    donnees = {
        "action": f"{r['nom']} ({r.name})", "secteur": r["secteur"], "poche": r["poche"],
        "score_fondamental": r["score_fondamental"], "score_technique": r["score_technique"],
        "piliers": {p: r[p] for p in config.PILIERS},
        "raisons": r["raisons"], "entree": r.get("entree"), "invalidation": r.get("invalidation"),
    }
    prompt = (
        "Tu es un analyste financier rigoureux. À partir UNIQUEMENT des données ci-dessous, "
        "rédige en français une thèse d'investissement de 5 phrases maximum : pourquoi cette action "
        "ressort, ce qui pourrait casser la thèse (risques), et quoi surveiller. N'invente aucun chiffre "
        "absent des données. Termine par : « À valider par une analyse personnelle. »\n\n"
        + json.dumps(donnees, ensure_ascii=False, default=str, indent=2)
    )
    corps = {"model": modele, "prompt": prompt, "stream": False, "think": False,
             "options": {"temperature": 0.3}}
    url = f"{config.OLLAMA_URL}/api/generate"
    rep = requests.post(url, json=corps, timeout=600)
    if rep.status_code >= 400 and "think" in rep.text.lower():
        corps.pop("think")
        rep = requests.post(url, json=corps, timeout=600)
    rep.raise_for_status()
    texte = rep.json().get("response", "").strip()
    if "</think>" in texte:  # au cas où le modèle afficherait son raisonnement
        texte = texte.split("</think>")[-1].strip()
    return texte


# ------------------------------------------------------------------ sorties --
def afficher_console(res: pd.DataFrame, ctx: dict, n: int = 15) -> None:
    print("\n" + "=" * 92)
    print(f" RAPPORT DU {date.today().strftime('%d/%m/%Y')} — contexte de marché : {ctx.get('regime')}"
          f"   (VIX {ctx.get('vix', 'n.d.')}, EUR/USD {ctx.get('eurusd', 'n.d.')})")
    print("=" * 92)
    for a in ctx["alertes"]:
        print(f" ⚠️  {a}")
    print(f"\n {'#':>2}  {'Action':<22} {'Poche':<5} {'Fond.':>5} {'Tech.':>5} {'Total':>6}  Signal")
    print(" " + "-" * 90)
    for i, (t, r) in enumerate(res.head(n).iterrows(), 1):
        fond = f"{r['score_fondamental']:.0f}" if pd.notna(r["score_fondamental"]) else "-"
        tech = f"{r['score_technique']:.0f}" if pd.notna(r["score_technique"]) else "-"
        print(f" {i:>2}  {(r['nom'] + ' (' + t + ')')[:22]:<22} {r['poche']:<5} {fond:>5} {tech:>5} "
              f"{r['score_combine']:>6.1f}  {r['signal']}")
    candidats = res[res["signal"] == SIGNAL_CANDIDAT]
    print(f"\n {len(candidats)} candidat(s) à étudier sur {len(res)} actions analysées.")
    print(" Outil d'aide à la décision personnel — pas un conseil en investissement.\n")


def _classe_signal(s: str) -> str:
    return {SIGNAL_CANDIDAT: "vert", SIGNAL_ATTENDRE: "jaune", SIGNAL_NEUTRE: "gris", SIGNAL_EVITER: "rouge"}.get(s, "gris")


def _txt(v) -> str:
    return v if isinstance(v, str) and v else "n.d."


def _barre(v) -> str:
    if pd.isna(v):
        return '<span class="nd">n.d.</span>'
    return (f'<span class="jauge"><span class="barre"><span style="width:{max(0, min(100, v)):.0f}%"></span></span>'
            f'<b>{v:.0f}</b></span>')


def ecrire_html(res: pd.DataFrame, ctx: dict, chemin: Path, section_portefeuille: str = "") -> None:
    e = html.escape
    candidats = res[res["signal"] == SIGNAL_CANDIDAT]

    cartes = []
    for t, r in candidats.iterrows():
        these = f'<p class="these">{e(r["these"])}</p>' if isinstance(r.get("these"), str) and r["these"] else ""
        raisons = "".join(f"<li>{e(x)}</li>" for x in r["raisons"])
        cartes.append(f"""
        <article class="carte">
          <header><h3>{e(r['nom'])} <small>{e(t)} · {e(r['poche'])} · {e(r['secteur'])}</small></h3>
            <div class="score">{r['score_combine']:.0f}<small>/100</small></div></header>
          <div class="sous-scores">
            <div>Fondamental {_barre(r['score_fondamental'])}</div>
            <div>Timing {_barre(r['score_technique'])}</div>
          </div>
          <ul>{raisons}</ul>
          <p class="niveaux"><b>Zone d'entrée indicative :</b> {e(_txt(r.get('entree')))}<br>
             <b>Thèse technique invalidée si :</b> {e(_txt(r.get('invalidation')))}</p>
          {these}
        </article>""")
    if not cartes:
        cartes.append('<p class="vide">Aucun candidat aujourd\'hui. Rester en cash est aussi une décision.</p>')

    lignes = []
    for i, (t, r) in enumerate(res.iterrows(), 1):
        lignes.append(f"""<tr>
          <td>{i}</td><td><b>{e(r['nom'])}</b><br><small>{e(t)}</small></td><td>{e(r['poche'])}</td>
          <td>{e(r['secteur'])}</td>
          <td>{_barre(r['score_fondamental'])}</td><td>{_barre(r['score_technique'])}</td>
          <td class="num">{r['score_combine']:.0f}</td>
          <td><span class="badge {_classe_signal(r['signal'])}">{e(r['signal'])}</span></td>
          <td class="raisons">{e(' · '.join(r['raisons']))}</td></tr>""")

    def m(cle, suffixe=""):
        v = ctx.get(cle)
        return f"{v}{suffixe}" if v is not None else "n.d."

    alertes = "".join(f'<div class="alerte">⚠️ {e(a)}</div>' for a in ctx["alertes"])
    page = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rapport du {date.today().strftime('%d/%m/%Y')}</title>
<style>
:root {{ --fond:#f6f5f1; --carte:#fff; --texte:#1d1d1b; --doux:#6b6b66; --ligne:#e4e2db;
        --vert:#1f7a4d; --jaune:#a86b00; --rouge:#b23a2e; --gris:#77776f; --accent:#24496b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fond:#141413; --carte:#1e1e1c; --texte:#ecebe6;
        --doux:#a3a29b; --ligne:#33322e; --vert:#5cc28d; --jaune:#e0a83a; --rouge:#e3776b; --gris:#a3a29b; --accent:#8fb4d9; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--fond); color:var(--texte); font:15px/1.5 -apple-system, "SF Pro Text", "Helvetica Neue", sans-serif; }}
main {{ max-width:1200px; margin:0 auto; padding:28px 16px 60px; }}
h1 {{ font-size:26px; margin:0 0 4px; }} h2 {{ font-size:18px; margin:32px 0 12px; }}
.sous-titre {{ color:var(--doux); margin:0 0 20px; }}
.contexte {{ display:flex; flex-wrap:wrap; gap:10px; }}
.indic {{ background:var(--carte); border:1px solid var(--ligne); border-radius:10px; padding:10px 14px; min-width:120px; }}
.indic small {{ display:block; color:var(--doux); font-size:12px; }} .indic b {{ font-size:17px; }}
.alerte {{ margin-top:10px; padding:10px 14px; border-radius:10px; background:color-mix(in srgb, var(--jaune) 14%, transparent); }}
.cartes {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(340px, 1fr)); gap:14px; }}
.carte {{ background:var(--carte); border:1px solid var(--ligne); border-left:4px solid var(--vert); border-radius:12px; padding:16px 18px; }}
.carte header {{ display:flex; justify-content:space-between; align-items:flex-start; gap:10px; }}
.carte h3 {{ margin:0; font-size:17px; }} .carte h3 small {{ display:block; font-weight:400; color:var(--doux); font-size:12px; }}
.score {{ font-size:30px; font-weight:700; color:var(--vert); line-height:1; }} .score small {{ font-size:13px; color:var(--doux); }}
.sous-scores {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:12px 0 4px; font-size:13px; color:var(--doux); }}
.carte ul {{ margin:8px 0; padding-left:18px; }} .carte li {{ margin:2px 0; }}
.niveaux {{ font-size:13px; background:var(--fond); border-radius:8px; padding:8px 10px; }}
.these {{ font-style:italic; border-top:1px solid var(--ligne); padding-top:10px; }}
.vide {{ color:var(--doux); }}
.tableau {{ overflow-x:auto; background:var(--carte); border:1px solid var(--ligne); border-radius:12px; }}
table {{ border-collapse:collapse; width:100%; min-width:900px; font-size:13.5px; }}
th, td {{ padding:9px 10px; border-bottom:1px solid var(--ligne); text-align:left; vertical-align:top; }}
th {{ font-size:12px; color:var(--doux); font-weight:600; position:sticky; top:0; background:var(--carte); }}
td small {{ color:var(--doux); }} .num {{ font-weight:700; font-variant-numeric:tabular-nums; }}
.raisons {{ color:var(--doux); font-size:12.5px; max-width:420px; }}
.jauge {{ white-space:nowrap; }}
.barre {{ display:inline-block; width:60px; height:6px; border-radius:3px; background:var(--ligne); margin-right:6px; vertical-align:middle; overflow:hidden; }}
.barre span {{ display:block; height:100%; background:var(--accent); }}
.badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:600; white-space:nowrap; }}
.badge.vert {{ color:var(--vert); background:color-mix(in srgb, var(--vert) 14%, transparent); }}
.badge.jaune {{ color:var(--jaune); background:color-mix(in srgb, var(--jaune) 14%, transparent); }}
.badge.rouge {{ color:var(--rouge); background:color-mix(in srgb, var(--rouge) 14%, transparent); }}
.badge.gris {{ color:var(--gris); background:color-mix(in srgb, var(--gris) 14%, transparent); }}
.nd {{ color:var(--doux); }}
h3 {{ font-size:14.5px; margin:16px 0 6px; }}
h2 .total {{ font-weight:400; color:var(--doux); font-size:15px; margin-left:6px; }}
.indic small + small {{ margin-top:2px; }}
.pos {{ color:var(--vert); }} .neg {{ color:var(--rouge); }}
footer {{ margin-top:30px; color:var(--doux); font-size:12.5px; }}
</style></head>
<body><main>
<h1>Rapport du {date.today().strftime('%d/%m/%Y')}</h1>
<p class="sous-titre">{len(res)} actions analysées · {len(candidats)} candidat(s) à étudier · score = fondamental 60 % + timing 40 %</p>

<section class="contexte">
  <div class="indic"><small>Contexte de marché</small><b>{e(ctx.get('regime', 'n.d.'))}</b></div>
  <div class="indic"><small>VIX</small><b>{m('vix')}</b></div>
  <div class="indic"><small>EUR/USD (5 j)</small><b>{m('eurusd')}</b> <small>{(f"{ctx['eurusd_var_5j']:+.2f} %" if ctx.get('eurusd_var_5j') is not None else '')}</small></div>
  <div class="indic"><small>Taux Fed</small><b>{m('fed', ' %')}</b></div>
  <div class="indic"><small>Taux BCE (dépôt)</small><b>{m('bce', ' %')}</b></div>
  <div class="indic"><small>Inflation US / ZE</small><b>{m('inflation_us', ' %')} / {m('inflation_ze', ' %')}</b></div>
  <div class="indic"><small>Taux US 10 ans</small><b>{m('us10y', ' %')}</b></div>
</section>
{alertes}

<h2>Candidats à étudier</h2>
<section class="cartes">{''.join(cartes)}</section>

{section_portefeuille}

<h2>Classement complet</h2>
<div class="tableau"><table>
<thead><tr><th>#</th><th>Action</th><th>Poche</th><th>Secteur</th><th>Fondamental</th><th>Timing</th>
<th>Score</th><th>Signal</th><th>Pourquoi</th></tr></thead>
<tbody>{''.join(lignes)}</tbody></table></div>

<footer>
<p><b>Comment lire ce rapport.</b> Le score fondamental compare chaque action aux autres de l'univers
(valorisation, qualité, croissance, bilan) à partir d'un instantané Yahoo Finance du jour ; il n'a pas encore été
validé historiquement. Le timing applique la règle « acheter sur faiblesse dans une tendance haussière ».
Garde-fous actifs : stop à -{config.STOP_LIGNE * 100:.0f} % par ligne, recul maximal du portefeuille (-{config.DRAWDOWN_VIGILANCE * 100:.0f} / -{config.DRAWDOWN_PROTECTION * 100:.0f} / -{config.DRAWDOWN_URGENCE * 100:.0f} %), {config.MAX_SECTEUR * 100:.0f} % maximum par secteur, budget de {config.BUDGET_TRADES_AN} ordres par an. Les modules sentiment, couverture de change, dimensionnement (Kelly) et backtest walk-forward ne sont pas encore actifs.</p>
<p>Outil d'aide à la décision personnel, généré localement sur ton Mac. Ce n'est pas un conseil en investissement :
chaque décision reste la tienne.</p>
</footer>
</main></body></html>"""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(page, encoding="utf-8")


# ---------------------------------------------------------------- exécution --
def executer(conn, avec_these: bool = False, ouvrir: bool = False, dossier: Path | None = None) -> Path:
    res = construire_classement(conn)
    ctx = contexte_marche(conn)
    jour = date.today().isoformat()

    res["these"] = None
    if avec_these:
        for t in res[res["signal"] == SIGNAL_CANDIDAT].head(config.NB_THESES).index:
            try:
                log.info(f"  Rédaction de la thèse pour {t} avec {config.OLLAMA_MODELE}…")
                res.at[t, "these"] = rediger_these(res.loc[t])
            except Exception as ex:  # noqa: BLE001
                log.warning(f"  Thèse impossible pour {t} (Ollama est-il lancé ?) : {ex}")

    details_cols = ["cours", "mm50", "mm200", "rsi", "recul_52s", "perf_6m", "tendance_haussiere"]
    res["details"] = [
        {c: (r[c].item() if hasattr(r[c], "item") else r[c]) for c in details_cols if c in res.columns and pd.notna(r[c])}
        for _, r in res.iterrows()
    ]
    db.enregistrer_scores(conn, jour, res)

    from modules import portefeuille
    try:
        suivi = portefeuille.mettre_a_jour(conn, res, ctx)
        section = portefeuille.html_section(conn, suivi)
    except Exception as ex:  # noqa: BLE001 - le rapport doit sortir même si le suivi échoue
        log.exception(f"  Suivi du portefeuille impossible : {ex}")
        suivi, section = {}, ""

    dossier = Path(dossier or config.RAPPORTS_DIR)
    chemin = dossier / f"rapport_{jour}.html"
    ecrire_html(res, ctx, chemin, section)
    shutil.copyfile(chemin, dossier / "dernier_rapport.html")
    afficher_console(res, ctx)
    portefeuille.afficher_console(conn, suivi)
    db.journaliser(conn, "rapport", "ok", str(chemin))
    log.info(f"Rapport enregistré : {chemin}")
    if ouvrir:
        webbrowser.open(chemin.resolve().as_uri())
    return chemin
