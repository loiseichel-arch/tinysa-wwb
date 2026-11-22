#!/usr/bin/env python3
"""
tinySA -> Wireless Workbench (sessions, passes multiples, afficheur large)

Lancer :  python3 tinysa_wwb.py
Prérequis (une fois) :  pip3 install pyserial matplotlib
NB : non testé sur un vrai tinySA Ultra+ ZS-407 au moment de l'écriture.
     Utilise la case "Mode démo" pour tester sans appareil.
"""
import csv
import datetime
import glob
import json
import math
import os
import queue
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import numpy as np

POINTS = 450                       # points par balayage
PAS_MHZ = 0.025                    # pas de 25 kHz (minimum accepté par WWB)
DEBUT_DEFAUT = "470"
FIN_DEFAUT = "670"
CONFIG = os.path.join(os.path.expanduser("~"), ".tinysa_wwb.json")   # réglages mémorisés


# ---------- Communication avec le tinySA ----------

def trouver_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


def envoyer(ser, commande, timeout=30):
    """Envoie une commande et renvoie les lignes de réponse."""
    ser.reset_input_buffer()
    ser.write((commande + "\r").encode())
    buf = b""
    t0 = time.time()
    while not buf.endswith(b"ch> "):
        morceau = ser.read(4096)
        if morceau:
            buf += morceau
        elif time.time() - t0 > timeout:
            raise TimeoutError(f"Pas de réponse à : {commande}")
    lignes = buf.decode(errors="ignore").replace("\r", "").split("\n")
    return [l.strip() for l in lignes[1:] if l.strip() and l.strip() != "ch>"]


def un_balayage(ser, debut_mhz, fin_mhz):
    lignes = envoyer(ser, f"scan {int(debut_mhz * 1e6)} {int(fin_mhz * 1e6)} {POINTS} 3")
    freqs, niveaux = [], []
    for l in lignes:
        parts = l.split()
        if len(parts) >= 2:
            try:
                freqs.append(float(parts[0]) / 1e6)
                niveaux.append(float(parts[1]))
            except ValueError:
                pass
    return freqs, niveaux


def balayage_demo(debut_mhz, fin_mhz):
    pas = (fin_mhz - debut_mhz) / (POINTS - 1)
    freqs = [debut_mhz + i * pas for i in range(POINTS)]
    niveaux = []
    for f in freqs:
        bruit = -100 + random.uniform(-3, 3)
        pic = 40 * math.exp(-((f - 482) ** 2) / 8) + 30 * math.exp(-((f - 620.4) ** 2) / 0.05)
        # un émetteur intermittent à 495 MHz (1 s sur 3), pour voir l'effet du Max Hold
        if int(time.time()) % 3 == 0 and abs(f - 495) < 0.1:
            pic += 35
        niveaux.append(bruit + pic)
    return freqs, niveaux


def balayer(ser, debut_mhz, fin_mhz, demo):
    """Un balayage, réel ou simulé."""
    if demo:
        time.sleep(0.2)
        return balayage_demo(debut_mhz, fin_mhz)
    for essai in range(3):               # un balayage perdu ne doit pas faire échouer tout le scan
        try:
            return un_balayage(ser, debut_mhz, fin_mhz)
        except TimeoutError:
            if essai == 2:
                raise
            time.sleep(0.5)


# ---------- Logique de scan ----------

def generer_tranches(debut, fin):
    """Tranches de 450 points au pas exact de 25 kHz (11,25 MHz chacune).

    Chaque tranche démarre sur la grille 470 + k x 11,25 MHz (comme SoundBase)
    et s'arrête 449 pas plus loin : la tranche suivante reprend au pas suivant,
    sans trou ni doublon. La dernière tranche peut dépasser un peu 'fin'.
    """
    if fin <= debut:
        raise ValueError("fin <= début")
    largeur = POINTS * PAS_MHZ              # 11,25 MHz
    n = int(math.ceil((fin - debut) / largeur - 1e-9))
    res = []
    for k in range(n):
        a = round(debut + k * largeur, 6)
        res.append((a, round(a + (POINTS - 1) * PAS_MHZ, 6)))
    return res


def boucle_visualiseur(ser, p, msg, stop):
    """Balayages en boucle sur toutes les tranches. Rien n'est enregistré.
    Max Hold coché : on garde le maximum. Sinon : moyenne glissante (80 % ancien / 20 % nouveau)."""
    reset = p.get("reset") or threading.Event()
    etat = {}
    total = len(p["tranches"])
    passage = 0
    while not stop.is_set():
        passage += 1
        for idx, (debut, fin) in enumerate(p["tranches"]):
            if stop.is_set():
                break
            if reset.is_set():
                etat.clear()
                reset.clear()
            f, d = balayer(ser, debut, fin, p["demo"])
            if not f:
                time.sleep(0.2)
                continue
            prec = etat.get(idx)
            if prec is None or len(prec) != len(d):
                acc = list(d)
            elif p["maxhold"]:
                acc = [max(a, b) for a, b in zip(prec, d)]
            else:
                acc = [0.8 * a + 0.2 * b for a, b in zip(prec, d)]
            etat[idx] = acc
            msg("live", (idx, f, d, acc, passage))
            msg("status", f"Visualiseur : tranche {idx + 1}/{total}, passage {passage}")


def decrire_reglages(p):
    if not p.get("appliquer"):
        return "réglages de l'appareil inchangés"
    rbw = "auto" if p["rbw"] == "auto" else f"{p['rbw']} kHz"
    if p["lna"]:
        return f"RBW {rbw}, LNA on"
    att = "auto" if p["att"] == "auto" else f"{p['att']} dB"
    return f"RBW {rbw}, atténuation {att}, LNA off"


def appliquer_reglages(ser, p, msg):
    """Envoie LNA / atténuation / RBW au tinySA et affiche ses réponses."""
    cmds = [f"lna {'on' if p['lna'] else 'off'}"]
    if not p["lna"]:  # LNA et atténuateur s'excluent sur l'Ultra
        cmds.append(f"attenuate {p['att']}")
    cmds.append(f"rbw {p['rbw']}")
    for c in cmds:
        rep = envoyer(ser, c)
        if c == "rbw auto" and any("usage" in l.lower() for l in rep):
            c = "rbw 0"  # ancienne syntaxe : 0 = auto
            rep = envoyer(ser, c)
        msg("log", f"  {c}" + (f"  ->  {' '.join(rep)}" if rep else ""))


LISSAGE_PTS = 9      # moyenne glissante sur 9 points (225 kHz), pour l'export et le visualiseur


def lisser_valeurs(vals, n=LISSAGE_PTS):
    """Moyenne glissante sur n points d'une liste de niveaux (dBm), calculée en puissance (mW).
    La fenêtre se réduit aux bords. Rapide (sommes cumulées) : sert aussi au visualiseur."""
    if not vals:
        return []
    demi = n // 2
    cum = [0.0]
    for v in vals:
        cum.append(cum[-1] + 10 ** (v / 10))
    sortie = []
    for i, v in enumerate(vals):
        a = max(0, i - demi)
        b = min(len(vals), i + demi + 1)
        moy = (cum[b] - cum[a]) / (b - a)
        sortie.append(10 * math.log10(moy) if moy > 0 else v)
    return sortie


def lisser(donnees, n=LISSAGE_PTS):
    """Lisse le scan à l'export ({MHz: dBm} -> nouveau dictionnaire), morceau continu par morceau
    continu : jamais de calcul à travers un trou du scan. Un lissage abaisse un peu les pics étroits."""
    if not donnees:
        return {}
    freqs = sorted(donnees)
    morceaux, courant = [], [freqs[0]]
    for a, b in zip(freqs, freqs[1:]):
        if b - a > PAS_MHZ * 1.5:
            morceaux.append(courant)
            courant = []
        courant.append(b)
    morceaux.append(courant)
    sortie = {}
    for morceau in morceaux:
        for f, v in zip(morceau, lisser_valeurs([donnees[f] for f in morceau], n)):
            sortie[f] = v
    return sortie


# ---------- Canaux TNT ----------

def canal_tnt(n):
    """Canal TNT (UHF) n -> (début, fin) en MHz. Canal 21 = 470-478 MHz, 8 MHz de large."""
    debut = 470 + (n - 21) * 8
    return debut, debut + 8


def canal_de(mhz):
    if mhz < 470 or mhz >= 790:
        return None
    return int((mhz - 470) // 8) + 21


def lire_canaux(texte):
    """'22, 23 25' -> {22, 23, 25} (canaux 21 à 60 seulement ; le reste est ignoré)."""
    res = set()
    for m in re.findall(r"\d+", texte or ""):
        if 21 <= int(m) <= 60:
            res.add(int(m))
    return res


def dessiner_tnt(ax, ax_tnt, xmin, xmax, surlignes=(), afficher=True):
    """Repères TNT façon Wireless Workbench : une bande de cases numérotées sous l'axe des fréquences,
    des traits discrets dans le graphique, les canaux surlignés en rouge. Renvoie les éléments dessinés."""
    from matplotlib.patches import Rectangle
    arts = []
    if not afficher:
        return arts
    for n in range(21, 61):
        a, b = canal_tnt(n)
        if b < xmin or a > xmax:
            continue
        surligne = n in surlignes
        arts.append(ax_tnt.add_patch(Rectangle((a, 0.1), b - a, 0.8, facecolor="#7a2323" if surligne else "#242424",
                                               edgecolor="#8a8a8a", linewidth=0.8)))
        centre = (max(a, xmin) + min(b, xmax)) / 2
        arts.append(ax_tnt.text(centre, 0.5, str(n), ha="center", va="center", fontsize=9,
                                color="white", clip_on=True))
        arts.append(ax.axvline(a, color="#5a5a5a", alpha=0.35, linewidth=0.5))
        if surligne:
            arts.append(ax.axvspan(max(a, xmin), min(b, xmax), color="#ff4d4d", alpha=0.13, linewidth=0))
    return arts


def pics(donnees, n=5, ecart=1.0):
    """Les n pics les plus forts du scan lissé, espacés d'au moins 'ecart' MHz : [(MHz, dBm, canal)]."""
    lis = lisser(donnees)
    res = []
    for f in sorted(lis, key=lambda x: -lis[x]):
        if all(abs(f - g) >= ecart for g, _ in res):
            res.append((f, lis[f]))
            if len(res) == n:
                break
    return [(f, v, canal_de(f)) for f, v in res]


# ---------- Comparaison avec un scan Shure (calibration) ----------

def lire_shure(chemin):
    """Lit un scan Shure : .sdb3 (Wireless Workbench / ULX-D) ou CSV 'MHz,dBm'. Renvoie {MHz: dBm}."""
    if chemin.lower().endswith(".sdb3"):
        d = open(chemin, "rb").read()
        marque = b"@Binary:@Swp"
        i = d.find(marque)
        if i < 0 or not d.startswith(b"//@ShureScan"):
            raise ValueError("Fichier .sdb3 non reconnu.")
        entete = json.loads(d[:i].decode("utf-8", errors="ignore").split("\n", 1)[1])
        courbe = next(x["Curve"] for x in entete["BinarySchema"] if "Curve" in x)
        plage = courbe["FreqRanges"][0]            # en kHz
        pas = plage["StepFreq"]
        n = (plage["EndFreq"] - plage["StartFreq"]) // pas + 1
        corps = d[i + len(marque) + 8:]            # 8 octets d'en-tête de balayage, puis n valeurs int16
        if len(corps) < 2 * n:
            raise ValueError("Fichier .sdb3 incomplet.")
        facteur = entete.get("Scale Factor", 10)
        vals = struct.unpack(">%dh" % n, corps[:2 * n])
        return {round((plage["StartFreq"] + k * pas) / 1000, 3): v / facteur for k, v in enumerate(vals)}
    res = {}
    with open(chemin, encoding="utf-8", errors="ignore") as f:
        for ligne in f:
            ligne = ligne.strip()
            if not ligne:
                continue
            if ";" in ligne:
                morceaux = [x.replace(",", ".") for x in ligne.split(";")]
            else:
                morceaux = re.split(r"[,\t ]+", ligne)
            try:
                res[round(float(morceaux[0]), 3)] = float(morceaux[1])
            except (ValueError, IndexError):
                continue                           # en-tête ou ligne invalide
    if not res:
        raise ValueError("Aucune ligne 'MHz,dBm' lisible dans ce fichier.")
    return res


def _mediane(v):
    v = sorted(v)
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def comparer(tiny, shure):
    """Compare un scan tinySA et un scan Shure sur leurs fréquences communes (les deux lissés 9 pts).
    Décalage = Shure - tinySA : c'est ce qu'il faut ajouter au tinySA pour retrouver le niveau Shure."""
    t = {round(f, 3): v for f, v in lisser(tiny).items()}
    s = {round(f, 3): v for f, v in lisser(shure).items()}
    comm = sorted(set(t) & set(s))
    if len(comm) < 50:
        raise ValueError("Pas assez de fréquences en commun (il faut au moins 50 points entre les deux scans).")
    plancher_t = _mediane([t[f] for f in comm])
    plancher_s = _mediane([s[f] for f in comm])
    diffs = {f: s[f] - t[f] for f in comm}
    signal = [f for f in comm if t[f] > plancher_t + 10 or s[f] > plancher_s + 10]
    off_tout = _mediane(list(diffs.values()))
    off_sig = _mediane([diffs[f] for f in signal]) if len(signal) >= 10 else None
    quart = len(comm) // 4
    bandes = []
    for k in range(4):
        sous = comm[k * quart:(k + 1) * quart] if k < 3 else comm[3 * quart:]
        bandes.append((sous[0], sous[-1], _mediane([diffs[f] for f in sous])))
    return {
        "n": len(comm), "plage": (comm[0], comm[-1]),
        "plancher_tiny": plancher_t, "plancher_shure": plancher_s,
        "off_tout": off_tout, "off_signal": off_sig, "n_signal": len(signal),
        "offset": off_sig if off_sig is not None else off_tout,
        "bandes": bandes,
    }


# ---------- Réglages mémorisés / noms de fichiers ----------

def charger_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def sauver_config(cfg):
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass                                        # jamais bloquant


def nom_fichier(nom, ext):
    base = re.sub(r"[^\w\-]+", "_", (nom or "").strip()).strip("_") or "scan"
    return f"{base}_{datetime.date.today():%Y-%m-%d}.{ext}"


def ecrire_csv(donnees, chemin):
    """Format WWB : fréquence (MHz), niveau (dBm), sans en-tête."""
    with open(chemin, "w", newline="") as f:
        w = csv.writer(f)
        for mhz in sorted(donnees):
            w.writerow([f"{mhz:.4f}", f"{donnees[mhz]:.1f}"])




# ---------- Sessions et scans ----------

# couleurs vives, lisibles sur fond noir
PALETTE = ["#4da3ff", "#ffb347", "#6ee06e", "#ff6b6b", "#c792ea", "#ffd54f", "#4dd0e1", "#f48fb1"]
LISSAGE_K_MAX = 20           # curseur de lissage : k de 0 à 20  ->  1 + 2k points (1 à 41)
LISSAGE_K_DEFAUT = 13        # 27 points (675 kHz)
EXTENSION = ".tinysa"


def n_lissage(k):
    return 1 + 2 * int(round(float(k)))


def nouvelle_session():
    return {"version": 2, "nom": "Sans titre", "ref": None, "scans": []}


def creer_scan(session, nom, donnees, **meta):
    """Ajoute un scan ({MHz: dBm}) à la session. Nom unique, couleur suivante de la palette."""
    nom = (nom or "").strip() or f"Scan {len(session['scans']) + 1}"
    base, k = nom, 2
    pris = {s["nom"] for s in session["scans"]}
    while nom in pris:
        nom = f"{base} ({k})"
        k += 1
    freqs = sorted(donnees)
    scan = {
        "id": uuid.uuid4().hex[:8],
        "nom": nom,
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "couleur": PALETTE[len(session["scans"]) % len(PALETTE)],
        "visible": True,
        "gain": 0.0,
        "f": [round(x, 4) for x in freqs],
        "d": [round(donnees[x], 1) for x in freqs],
    }
    scan.update(meta)
    session["scans"].append(scan)
    return scan


def scan_donnees(scan):
    return dict(zip(scan["f"], scan["d"]))


def decrire_scan(scan):
    f = scan["f"]
    plage = f"{f[0]:g} - {f[-1]:g} MHz" if f else "vide"
    try:
        jour = datetime.datetime.fromisoformat(scan["date"]).strftime("%d/%m %H:%M")
    except (KeyError, ValueError):
        jour = ""
    extra = scan.get("resume", "")
    corr = []
    g = scan.get("gain", 0.0)
    if g:
        corr.append(f"gain {g:+.1f} dB".replace(".", ","))
    if scan.get("corr_f"):
        corr.append("corrigé par bande")
    return f"{plage}   {jour}" + (f"\n{extra}" if extra else "") + (f"\n{' + '.join(corr)}" if corr else "")


def sauver_session(session, chemin):
    """Écriture sûre : fichier temporaire puis remplacement (pas de fichier à moitié écrit)."""
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, chemin)


def charger_session(chemin):
    with open(chemin, encoding="utf-8") as f:
        s = json.load(f)
    if not isinstance(s, dict) or not isinstance(s.get("scans"), list):
        raise ValueError("Ce fichier n'est pas une session tinySA.")
    s.setdefault("nom", os.path.splitext(os.path.basename(chemin))[0])
    s.setdefault("ref", None)
    for sc in s["scans"]:
        sc.setdefault("gain", 0.0)
        sc.setdefault("id", uuid.uuid4().hex[:8])
        sc.setdefault("couleur", PALETTE[0])
        sc.setdefault("visible", True)
        if len(sc.get("f", [])) != len(sc.get("d", [])):
            raise ValueError(f"Scan « {sc.get('nom', '?')} » corrompu.")
    return s


def courbe(freqs, vals, n):
    """(xs, brut, lissé) avec un NaN à chaque trou : on ne relie jamais à travers un trou,
    et le lissage (n points, en puissance) est calculé morceau continu par morceau continu."""
    nan = float("nan")
    xs, brut, lis = [], [], []
    debut = 0
    for i in range(1, len(freqs) + 1):
        if i == len(freqs) or freqs[i] - freqs[i - 1] > PAS_MHZ * 1.5:
            f, v = freqs[debut:i], vals[debut:i]
            if xs:
                xs.append(nan)
                brut.append(nan)
                lis.append(nan)
            xs += f
            brut += v
            lis += lisser_valeurs(v, n)
            debut = i
    return xs, brut, lis


def scan_effectif(scan):
    """Fréquences et niveaux d'un scan avec son gain et sa correction par bande (ce qu'on voit et ce qu'on exporte)."""
    f = np.asarray(scan["f"], dtype=float)
    d = np.asarray(scan["d"], dtype=float) + float(scan.get("gain", 0.0))
    if scan.get("corr_f"):
        d = d + np.interp(f, scan["corr_f"], scan["corr_d"])      # hors plage : on garde les valeurs des bords
    return f.tolist(), d.tolist()


def donnees_effectives(scan):
    f, d = scan_effectif(scan)
    return dict(zip(f, d))


def correction_par_bande(tiny, ref, largeur=4.0):
    """Courbe de correction (dB) pour rapprocher 'tiny' de 'ref', fenêtre par fenêtre (4 MHz).
    Sur chaque fenêtre : médiane de l'écart sur les points où il y a du signal ; les fenêtres sans signal
    sont interpolées ; puis un léger lissage. Renvoie (fréquences, corrections, infos)."""
    t = {round(f, 3): v for f, v in lisser(tiny).items()}
    s = {round(f, 3): v for f, v in lisser(ref).items()}
    comm = sorted(set(t) & set(s))
    if len(comm) < 50:
        raise ValueError("Pas assez de fréquences en commun (il faut au moins 50 points entre les deux scans).")
    F = np.array(comm)
    T = np.array([t[f] for f in comm])
    S = np.array([s[f] for f in comm])
    D = S - T
    sig = (T > np.median(T) + 10) | (S > np.median(S) + 10)
    nb = max(1, int(math.ceil((F[-1] - F[0]) / largeur)))
    centres, valeurs, masques = [], [], []
    for k in range(nb):
        a = F[0] + k * largeur
        m = (F >= a) & (F < a + largeur) if k < nb - 1 else (F >= a)
        if not m.any():
            continue
        centres.append(float(F[m].mean()))
        masques.append(m)
        valeurs.append(float(np.median(D[m & sig])) if (m & sig).sum() >= 5 else None)
    avec_signaux = sum(v is not None for v in valeurs)
    if avec_signaux == 0:       # aucun signal : on cale le niveau typique de chaque fenêtre
        valeurs = [float(np.median(D[m])) for m in masques]
    ok = [i for i, v in enumerate(valeurs) if v is not None]
    vals = np.interp(centres, [centres[i] for i in ok], [valeurs[i] for i in ok])
    if len(vals) >= 3:
        vals = np.convolve(np.pad(vals, 1, mode="edge"), np.ones(3) / 3, mode="valid")
    infos = {"fenetres": len(centres), "avec_signaux": avec_signaux,
             "min": float(vals.min()), "max": float(vals.max())}
    return [round(c, 3) for c in centres], [round(float(v), 2) for v in vals], infos


# ---------- Travail en arrière-plan ----------

def travail(p, msg, stop):
    """Scan par passes complètes : on balaie toute la plage (tranche après tranche, de gauche à droite),
    puis on recommence. Chaque passe affine le cumul (Max Hold ou moyenne). 'Arrêter' garde tout."""
    ser = None
    try:
        if not p["demo"]:
            try:
                import serial
            except ImportError:
                raise RuntimeError("pyserial manquant : pip3 install pyserial")
            port = trouver_port()
            if not port:
                raise RuntimeError("tinySA introuvable (/dev/cu.usbmodem*). Branche-le en USB.")
            msg("log", f"Port : {port}")
            ser = serial.Serial(port, 115200, timeout=0.5)
            envoyer(ser, "pause")
            try:
                version = envoyer(ser, "version", timeout=3)
                if version:
                    msg("log", "Appareil : " + " ".join(version))
            except Exception:
                pass
            if p["appliquer"]:
                msg("log", "Application des réglages :")
                appliquer_reglages(ser, p, msg)

        reglages = decrire_reglages(p)
        msg("log", f"Réglages : {reglages}")
        msg("plan", p["tranches"])
        if p.get("viewer"):
            msg("log", "Visualiseur en marche (rien n'est enregistré). Clique sur Arrêter pour finir.")
            boucle_visualiseur(ser, p, msg, stop)
            msg("log", "Visualiseur arrêté.")
            return

        n_tr = len(p["tranches"])
        passes = p["passes"]                    # 0 = jusqu'à l'arrêt
        etat = {}                               # idx -> [freqs, maxi, somme, nb de balayages]
        passe = 0
        completes = 0
        t0 = time.time()
        while not stop.is_set() and (passes == 0 or passe < passes):
            passe += 1
            fini = True
            for idx, (debut, fin) in enumerate(p["tranches"]):
                if stop.is_set():
                    fini = False
                    break
                f, d = balayer(ser, debut, fin, p["demo"])
                if not f:
                    continue
                e = etat.get(idx)
                if e is None or len(e[1]) != len(d):
                    e = etat[idx] = [f, list(d), list(d), 1]
                else:
                    e[1] = [max(a, b) for a, b in zip(e[1], d)]
                    e[2] = [a + b for a, b in zip(e[2], d)]
                    e[3] += 1
                acc = e[1] if p["maxhold"] else [x / e[3] for x in e[2]]
                msg("live", (idx, f, d, acc, passe))
                sur = f"/{passes}" if passes else ""
                msg("status", f"Passe {passe}{sur}  -  tranche {idx + 1}/{n_tr}  -  {int(time.time() - t0)} s")
                if passes:
                    msg("progress", ((passe - 1) * n_tr + idx + 1) / (passes * n_tr))
                else:
                    msg("progress", (idx + 1) / n_tr)
            if fini:
                completes += 1
        if not etat:
            raise RuntimeError("Aucune donnée reçue du tinySA (vérifie le câble).")
        donnees = {}
        for idx in sorted(etat):
            f, mx, so, nb = etat[idx]
            niv = mx if p["maxhold"] else [x / nb for x in so]
            for mhz, dbm in zip(f, niv):
                cle = round(mhz, 4)
                donnees[cle] = max(dbm, donnees.get(cle, -999))
        mode = "Max Hold" if p["maxhold"] else "moyenne"
        partielle = len(etat) < n_tr or completes < passe
        resume = f"{completes} passe(s) {mode}" + (" + passe partielle" if partielle else "") + f", {reglages}"
        msg("result", {"donnees": donnees, "resume": resume, "passes": completes, "mode": mode,
                       "duree": int(time.time() - t0)})
        msg("log", f"Scan terminé : {len(donnees)} points, {resume}.")
    except Exception as e:
        if isinstance(e, OSError):
            msg("log", f"ERREUR : liaison USB perdue (câble débranché ?) - {e}")
        else:
            msg("log", f"ERREUR : {e}")
    finally:
        if ser:
            try:
                envoyer(ser, "resume")
            except Exception:
                pass
            try:
                ser.close()
            except Exception:
                pass
        msg("done", "")


# ---------- Fenêtre ----------

FOND = "#101010"          # fond de la fenêtre graphique
GRIS = "#bbbbbb"


def main():
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox, simpledialog, colorchooser
    except ImportError:
        sys.exit("Tkinter indisponible. Installe Python depuis python.org (Tkinter inclus).")

    cfg = charger_config()
    if cfg.get("v") != 7:                       # anciens réglages d'affichage : on repart des nouveaux défauts
        for cle in ("k", "brut", "off"):
            cfg.pop(cle, None)
        cfg["v"] = 7

    def c_str(cle, defaut):
        return str(cfg.get(cle, defaut))

    def c_bool(cle, defaut):
        return bool(cfg.get(cle, defaut))

    sess = {"s": nouvelle_session(), "chemin": None, "modifie": False, "selection": None}
    q = queue.Queue()
    stop = threading.Event()
    reset = threading.Event()
    live = {"plan": [], "acc": {}, "now": ([], []), "courant": None, "dirty": True, "scan": False}
    vue = {"auto": True, "interne": False}
    seuil = {"y": None, "drag": False}
    pan = {"actif": False, "x": 0, "xlim": (0, 1)}
    lignes = {}          # id du scan -> (ligne brute, ligne lissée)
    cache = {}           # (id, lissage, gain, correction) -> courbe
    pistes = []          # courbes affichées (pour la bulle au survol d'un pic)
    etiquettes = {}      # id du scan -> étiquette de détails dans la liste
    gain_sync = {"actif": False}

    root = tk.Tk()
    root.title("tinySA -> WWB")
    try:
        root.geometry(f"{root.winfo_screenwidth()}x{max(700, root.winfo_screenheight() - 90)}+0+0")
    except Exception:
        root.geometry("1450x860")
    root.minsize(1100, 680)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)

    # ---- variables
    var_deb = tk.StringVar(value=c_str("deb", DEBUT_DEFAUT))
    var_fin = tk.StringVar(value=c_str("fin", FIN_DEFAUT))
    var_passes = tk.StringVar(value=c_str("passes", "6"))
    var_inf = tk.BooleanVar(value=c_bool("inf", False))
    var_max = tk.BooleanVar(value=c_bool("max", True))
    var_nom = tk.StringVar(value=c_str("nom_scan", ""))
    var_appl = tk.BooleanVar(value=c_bool("appl", True))
    var_rbw = tk.StringVar(value=c_str("rbw", "300"))
    var_att = tk.StringVar(value=c_str("att", "auto"))
    var_lna = tk.BooleanVar(value=c_bool("lna", True))
    var_demo = tk.BooleanVar(value=False)
    var_liss_ex = tk.BooleanVar(value=c_bool("liss_export", True))
    var_k = tk.IntVar(value=int(cfg.get("k", LISSAGE_K_DEFAUT)))
    var_brut = tk.BooleanVar(value=c_bool("brut", False))
    var_tnt = tk.BooleanVar(value=c_bool("tnt", True))
    var_canaux = tk.StringVar(value=c_str("canaux", ""))
    var_seuil = tk.StringVar(value="")
    var_statut = tk.StringVar(value="Prêt.")
    var_curseur = tk.StringVar(value="")
    var_liss_txt = tk.StringVar(value="")
    var_session = tk.StringVar(value="")
    var_gain_txt = tk.StringVar(value="Aucun scan sélectionné")

    # ---- barre du haut (3 lignes)
    haut = ttk.Frame(root, padding=(10, 6, 10, 2))
    haut.grid(row=0, column=0, sticky="we")
    haut.columnconfigure(0, weight=1)

    la = ttk.Frame(haut)
    la.grid(row=0, column=0, sticky="we")
    la.columnconfigure(5, weight=1)
    ttk.Label(la, textvariable=var_session, font=("Helvetica", 14, "bold"), width=26).grid(
        row=0, column=0, padx=(0, 10), sticky="w")
    btn_nouv = ttk.Button(la, text="Nouvelle")
    btn_nouv.grid(row=0, column=1, padx=(0, 4))
    btn_ouvrir = ttk.Button(la, text="Ouvrir...")
    btn_ouvrir.grid(row=0, column=2, padx=(0, 4))
    btn_enreg = ttk.Button(la, text="Enregistrer")
    btn_enreg.grid(row=0, column=3, padx=(0, 4))
    btn_sauver = ttk.Button(la, text="Enregistrer sous...")
    btn_sauver.grid(row=0, column=4, padx=(0, 14))
    ttk.Label(la, textvariable=var_statut, width=40).grid(row=0, column=5, sticky="e", padx=(0, 8))
    prog = ttk.Progressbar(la, mode="determinate", maximum=100, length=220)
    prog.grid(row=0, column=6, sticky="e")

    lb = ttk.Frame(haut)
    lb.grid(row=1, column=0, sticky="we", pady=(4, 0))
    ttk.Label(lb, text="Plage (MHz) :").grid(row=0, column=0)
    ttk.Entry(lb, textvariable=var_deb, width=6).grid(row=0, column=1, padx=(4, 2))
    ttk.Label(lb, text="à").grid(row=0, column=2)
    ttk.Entry(lb, textvariable=var_fin, width=6).grid(row=0, column=3, padx=(2, 14))
    ttk.Label(lb, text="Passes :").grid(row=0, column=4)
    ent_passes = ttk.Entry(lb, textvariable=var_passes, width=4)
    ent_passes.grid(row=0, column=5, padx=(4, 4))
    ttk.Checkbutton(lb, text="jusqu'à l'arrêt", variable=var_inf).grid(row=0, column=6, padx=(0, 14))
    ttk.Checkbutton(lb, text="Max Hold", variable=var_max).grid(row=0, column=7, padx=(0, 14))
    ttk.Label(lb, text="Nom du scan :").grid(row=0, column=8)
    ttk.Entry(lb, textvariable=var_nom, width=20).grid(row=0, column=9, padx=(4, 14))
    btn_go = ttk.Button(lb, text="Lancer le scan")
    btn_go.grid(row=0, column=10, padx=(0, 4))
    btn_view = ttk.Button(lb, text="Visualiser en direct")
    btn_view.grid(row=0, column=11, padx=(0, 4))
    btn_stop = ttk.Button(lb, text="Arrêter", state="disabled")
    btn_stop.grid(row=0, column=12, padx=(0, 4))
    btn_reset = ttk.Button(lb, text="Effacer le cumul", state="disabled")
    btn_reset.grid(row=0, column=13)

    lc = ttk.Frame(haut)
    lc.grid(row=2, column=0, sticky="we", pady=(4, 2))
    ttk.Checkbutton(lc, text="Appliquer ces réglages à l'appareil :", variable=var_appl).grid(row=0, column=0)
    ttk.Label(lc, text="RBW (kHz)").grid(row=0, column=1, padx=(8, 2))
    ttk.Combobox(lc, textvariable=var_rbw, values=["auto", "10", "30", "100", "300", "600"], width=6).grid(
        row=0, column=2)
    ttk.Label(lc, text="Atténuation (dB)").grid(row=0, column=3, padx=(8, 2))
    ttk.Combobox(lc, textvariable=var_att, values=["auto", "0", "10", "20", "30"], width=6).grid(row=0, column=4)
    ttk.Checkbutton(lc, text="LNA (désactive l'atténuation)", variable=var_lna).grid(row=0, column=5, padx=(10, 0))

    # ---- corps : Scan data à gauche, graphique pleine largeur à droite
    corps = ttk.Frame(root)
    corps.grid(row=1, column=0, sticky="nsew")
    corps.columnconfigure(1, weight=1)
    corps.rowconfigure(0, weight=1)

    sd = ttk.LabelFrame(corps, text="Scan data", padding=6)
    sd.grid(row=0, column=0, sticky="ns", padx=(10, 4), pady=4)
    sd.rowconfigure(0, weight=1)
    zone = tk.Canvas(sd, width=300, height=300, highlightthickness=0)
    zone.grid(row=0, column=0, sticky="nsew")
    asc = ttk.Scrollbar(sd, orient="vertical", command=zone.yview)
    asc.grid(row=0, column=1, sticky="ns")
    zone.configure(yscrollcommand=asc.set)
    liste = ttk.Frame(zone)
    zone.create_window((0, 0), window=liste, anchor="nw")
    liste.bind("<Configure>", lambda e: zone.configure(scrollregion=zone.bbox("all")))

    act = ttk.Frame(sd)
    act.grid(row=1, column=0, columnspan=2, sticky="we", pady=(6, 0))
    act.columnconfigure((0, 1, 2), weight=1)
    btn_export = ttk.Button(act, text="Exporter le scan sélectionné (CSV WWB)...")
    btn_export.grid(row=0, column=0, columnspan=3, sticky="we")
    btn_ren = ttk.Button(act, text="Renommer")
    btn_ren.grid(row=1, column=0, pady=(4, 0), sticky="we")
    btn_sup = ttk.Button(act, text="Supprimer")
    btn_sup.grid(row=1, column=1, pady=(4, 0), sticky="we")
    btn_imp = ttk.Button(act, text="Importer...")
    btn_imp.grid(row=1, column=2, pady=(4, 0), sticky="we")
    ttk.Checkbutton(act, text="Appliquer le lissage du fader à l'export", variable=var_liss_ex).grid(
        row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

    gf = ttk.LabelFrame(sd, text="Gain du scan sélectionné", padding=6)
    gf.grid(row=2, column=0, columnspan=2, sticky="we", pady=(8, 0))
    gf.columnconfigure(0, weight=1)
    ttk.Label(gf, textvariable=var_gain_txt, font=("Helvetica", 11, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
    curseur_gain = ttk.Scale(gf, from_=-40, to=40, orient="horizontal")
    curseur_gain.grid(row=1, column=0, columnspan=3, sticky="we", pady=(4, 4))
    btn_auto = ttk.Button(gf, text="Auto")
    btn_auto.grid(row=2, column=0, sticky="we", padx=(0, 3))
    btn_bande = ttk.Button(gf, text="Auto par bande")
    btn_bande.grid(row=2, column=1, sticky="we", padx=(0, 3))
    btn_rz = ttk.Button(gf, text="Réinit.")
    btn_rz.grid(row=2, column=2, sticky="we")
    ttk.Label(gf, text="Auto cale le scan sur celui coché « Réf. ».", foreground="gray").grid(
        row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

    graph_frame = ttk.Frame(corps)
    graph_frame.grid(row=0, column=1, sticky="nsew", pady=4, padx=(0, 10))
    graph_frame.columnconfigure(0, weight=1)
    graph_frame.rowconfigure(0, weight=1)

    graphe = None
    try:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.ticker import FuncFormatter
        fig = Figure(figsize=(12, 6), dpi=100, facecolor=FOND)
        gs = fig.add_gridspec(2, 1, height_ratios=[24, 1.5], hspace=0.16,
                              left=0.062, right=0.992, top=0.955, bottom=0.03)
        ax = fig.add_subplot(gs[0])
        ax_tnt = fig.add_subplot(gs[1], sharex=ax)
        ax.set_facecolor("black")
        ax_tnt.set_facecolor(FOND)
        for a in (ax, ax_tnt):
            for sp in a.spines.values():
                sp.set_color("#888888")
        ax.tick_params(colors=GRIS, labelsize=10)
        ax.grid(True, color="#3a3a3a", linewidth=0.6)
        ax.set_ylabel("Amplitude [dBm]", color=GRIS, fontsize=11)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.3f}".replace(".", ",") + " MHz"))
        ax_tnt.set_yticks([])
        ax_tnt.tick_params(labelbottom=False, bottom=False)
        ax_tnt.set_ylim(0, 1)
        ax_tnt.set_ylabel("TV", rotation=0, color=GRIS, fontsize=10, labelpad=12, va="center")
        ax.set_title("En attente d'un scan", color="#dddddd", fontsize=12)
        ax.set_xlim(float(DEBUT_DEFAUT), float(FIN_DEFAUT))
        ax.set_ylim(-120, -20)
        canvas = FigureCanvasTkAgg(fig, master=graph_frame)
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        ligne_live_raw, = ax.plot([], [], color="white", alpha=0.3, linewidth=0.7)
        ligne_live, = ax.plot([], [], color="white", linewidth=1.8, label="Scan en cours")
        ligne_now, = ax.plot([], [], color="#ff9f40", alpha=0.7, linewidth=0.7)
        ligne_seuil = ax.axhline(-100, color="#ff4d4d", linewidth=1.2, visible=False)
        txt_seuil = ax.text(0.01, 0.0, "", transform=ax.get_yaxis_transform(), color="#ff4d4d",
                            fontsize=9, va="bottom", visible=False)
        marque_pic, = ax.plot([], [], "o", markersize=8, markerfacecolor="none",
                              markeredgecolor="white", markeredgewidth=1.5, visible=False)
        bulle = ax.annotate("", xy=(0, 0), xytext=(14, 14), textcoords="offset points", color="white",
                            fontsize=10, annotation_clip=False, visible=False, zorder=10,
                            bbox=dict(boxstyle="round,pad=0.4", facecolor="#1e1e1e", edgecolor="#9a9a9a"))
        deco = []        # repères TNT, marqueur de tranche, remplissages (effacés et redessinés)
        graphe = True
    except Exception as err:
        ttk.Label(graph_frame, text=f"Visualisation indisponible ({err}).\nInstalle matplotlib :  pip3 install matplotlib").grid()

    # ---- barre du bas : lissage, options, journal
    bas = ttk.Frame(root, padding=(10, 2, 10, 6))
    bas.grid(row=2, column=0, sticky="we")
    bas.columnconfigure(1, weight=1)
    ttk.Label(bas, text="Lissage :", font=("Helvetica", 12, "bold")).grid(row=0, column=0, padx=(0, 8))
    curseur = ttk.Scale(bas, from_=0, to=LISSAGE_K_MAX, orient="horizontal")
    curseur.set(var_k.get())
    curseur.grid(row=0, column=1, sticky="we")
    ttk.Label(bas, textvariable=var_liss_txt, width=22).grid(row=0, column=2, padx=(8, 0))

    opt = ttk.Frame(bas)
    opt.grid(row=1, column=0, columnspan=3, sticky="we", pady=(4, 0))
    ttk.Checkbutton(opt, text="Courbe brute en pâle", variable=var_brut).grid(row=0, column=0, padx=(0, 12))
    ttk.Checkbutton(opt, text="Canaux TNT", variable=var_tnt).grid(row=0, column=1, padx=(0, 4))
    ttk.Label(opt, text="surligner :").grid(row=0, column=2)
    ttk.Entry(opt, textvariable=var_canaux, width=14).grid(row=0, column=3, padx=(4, 12))
    ttk.Label(opt, text="Seuil (dBm) :").grid(row=0, column=4)
    ttk.Entry(opt, textvariable=var_seuil, width=6).grid(row=0, column=5, padx=(4, 12))
    btn_zm = ttk.Button(opt, text="−", width=3)
    btn_zm.grid(row=0, column=6, padx=(0, 2))
    btn_zp = ttk.Button(opt, text="+", width=3)
    btn_zp.grid(row=0, column=7, padx=(0, 6))
    btn_vue = ttk.Button(opt, text="Vue complète")
    btn_vue.grid(row=0, column=8, padx=(0, 12))
    btn_img = ttk.Button(opt, text="Enregistrer l'image...")
    btn_img.grid(row=0, column=9, padx=(0, 12))
    btn_debug = ttk.Button(opt, text="Debug")
    btn_debug.grid(row=0, column=10, padx=(0, 12))
    ttk.Label(opt, textvariable=var_curseur, width=44).grid(row=0, column=11, sticky="e")

    log = tk.Text(bas, height=3, state="disabled")
    log.grid(row=2, column=0, columnspan=3, sticky="we", pady=(6, 0))

    # ---- utilitaires
    def ecrire(texte):
        log.config(state="normal")
        log.insert("end", texte + "\n")
        log.see("end")
        log.config(state="disabled")

    def maj_titre():
        marque = " *" if sess["modifie"] else ""
        nom = sess["s"]["nom"]
        var_session.set(f"{nom}{marque}")
        root.title(f"tinySA -> WWB  -  {nom}{marque}")

    def marquer_modifie():
        sess["modifie"] = True
        maj_titre()

    def sauver_sous():
        nom = sess["s"]["nom"]
        init = (nom if nom != "Sans titre" else "Session") + EXTENSION
        chemin = filedialog.asksaveasfilename(
            title="Enregistrer la session",
            defaultextension=EXTENSION,
            initialfile=init,
            initialdir=cfg.get("dossier", os.path.expanduser("~/Documents")),
            filetypes=[("Session tinySA", "*" + EXTENSION)],
        )
        if not chemin:
            return False
        sess["s"]["nom"] = os.path.splitext(os.path.basename(chemin))[0]
        try:
            sauver_session(sess["s"], chemin)
        except Exception as e:
            ecrire(f"ERREUR d'enregistrement : {e}")
            return False
        sess["chemin"] = chemin
        sess["modifie"] = False
        cfg["dossier"] = os.path.dirname(chemin)
        cfg["session"] = chemin
        ecrire(f"Session enregistrée : {chemin}")
        maj_titre()
        return True

    def sauver():
        if not sess["chemin"]:
            return sauver_sous()
        try:
            sauver_session(sess["s"], sess["chemin"])
        except Exception as e:
            ecrire(f"ERREUR d'enregistrement : {e}")
            return False
        sess["modifie"] = False
        ecrire(f"Session enregistrée : {sess['chemin']}")
        maj_titre()
        return True

    def confirmer_perte():
        """True si on peut quitter la session en cours (enregistrée, ou abandon voulu)."""
        if not (sess["s"]["scans"] and sess["modifie"]):
            return True
        rep = messagebox.askyesnocancel("Session non enregistrée",
                                        f"Enregistrer la session « {sess['s']['nom']} » avant de continuer ?")
        if rep is None:
            return False
        return sauver() if rep else True

    def scan_selectionne():
        for sc in sess["s"]["scans"]:
            if sc["id"] == sess["selection"]:
                return sc
        return None

    def scan_ref():
        for sc in sess["s"]["scans"]:
            if sc["id"] == sess["s"].get("ref"):
                return sc
        return None

    def installer_session(s, chemin):
        for ident in list(lignes):
            for l in lignes.pop(ident):
                l.remove()
        cache.clear()
        sess["s"] = s
        sess["chemin"] = chemin
        sess["modifie"] = False
        sess["selection"] = s["scans"][-1]["id"] if s["scans"] else None
        vue["auto"] = True
        live["acc"].clear()
        live["dirty"] = True
        rebuild_liste()
        maj_gain_ui()
        maj_titre()

    def nouvelle():
        if not confirmer_perte():
            return
        installer_session(nouvelle_session(), None)
        sess["chemin"] = None
        ecrire("Nouvelle session.")

    def ouvrir():
        if not confirmer_perte():
            return
        chemin = filedialog.askopenfilename(
            title="Ouvrir une session",
            initialdir=cfg.get("dossier", os.path.expanduser("~/Documents")),
            filetypes=[("Session tinySA", "*" + EXTENSION), ("Tous les fichiers", "*.*")],
        )
        if not chemin:
            return
        try:
            s = charger_session(chemin)
        except Exception as e:
            ecrire(f"Impossible d'ouvrir la session : {e}")
            return
        cfg["dossier"] = os.path.dirname(chemin)
        cfg["session"] = chemin
        installer_session(s, chemin)
        ecrire(f"Session ouverte : {chemin}  ({len(s['scans'])} scan(s))")

    # ---- liste des scans
    def selectionner(ident):
        sess["selection"] = ident
        rebuild_liste()
        maj_gain_ui()

    def basculer(sc, var):
        sc["visible"] = var.get()
        live["dirty"] = True
        vue["auto"] = True
        marquer_modifie()

    def definir_ref(sc, var):
        sess["s"]["ref"] = sc["id"] if var.get() else None
        marquer_modifie()
        rebuild_liste()

    def choisir_couleur(sc):
        res = colorchooser.askcolor(color=sc["couleur"], title=f"Couleur de « {sc['nom']} »")
        if res and res[1]:
            sc["couleur"] = res[1]
            live["dirty"] = True
            rebuild_liste()
            marquer_modifie()

    def rebuild_liste():
        for w in liste.winfo_children():
            w.destroy()
        etiquettes.clear()
        if not sess["s"]["scans"]:
            ttk.Label(liste, text="Aucun scan. Lance un scan ou importe un fichier.",
                      foreground="gray").grid(row=0, column=0, padx=4, pady=8)
        for i, sc in enumerate(sess["s"]["scans"]):
            choisi = sc["id"] == sess["selection"]
            fond = "#d7e6fb" if choisi else "#f2f2f2"
            ligne = tk.Frame(liste, bg=fond, padx=3, pady=3)
            ligne.grid(row=i, column=0, sticky="we", pady=1)
            var = tk.BooleanVar(value=sc.get("visible", True))
            ttk.Checkbutton(ligne, variable=var, command=lambda s=sc, v=var: basculer(s, v)).grid(
                row=0, column=0, rowspan=2, padx=(0, 4))
            sw = tk.Canvas(ligne, width=18, height=18, highlightthickness=0, bg=fond, cursor="hand2")
            sw.create_rectangle(1, 1, 17, 17, fill=sc["couleur"], outline="gray")
            sw.bind("<Button-1>", lambda e, s=sc: choisir_couleur(s))
            sw.grid(row=0, column=1, rowspan=2, padx=(0, 6))
            nom = tk.Label(ligne, text=sc["nom"], bg=fond, anchor="w", font=("Helvetica", 12, "bold"))
            nom.grid(row=0, column=2, sticky="we")
            det = tk.Label(ligne, text=decrire_scan(sc), bg=fond, anchor="w", justify="left",
                           font=("Helvetica", 9), fg="#444444")
            det.grid(row=1, column=2, sticky="we")
            etiquettes[sc["id"]] = det
            var_ref = tk.BooleanVar(value=sess["s"].get("ref") == sc["id"])
            ttk.Checkbutton(ligne, text="Réf.", variable=var_ref,
                            command=lambda s=sc, v=var_ref: definir_ref(s, v)).grid(row=0, column=3, rowspan=2, padx=(6, 0))
            ligne.columnconfigure(2, weight=1)
            for w in (ligne, nom, det):
                w.bind("<Button-1>", lambda e, ident=sc["id"]: selectionner(ident))

    def renommer():
        sc = scan_selectionne()
        if not sc:
            ecrire("Sélectionne d'abord un scan (clic sur son nom).")
            return
        nouveau = simpledialog.askstring("Renommer le scan", "Nom du scan :", initialvalue=sc["nom"])
        if nouveau and nouveau.strip():
            sc["nom"] = nouveau.strip()
            live["dirty"] = True
            rebuild_liste()
            maj_gain_ui()
            marquer_modifie()

    def supprimer():
        sc = scan_selectionne()
        if not sc:
            ecrire("Sélectionne d'abord un scan (clic sur son nom).")
            return
        if not messagebox.askyesno("Supprimer", f"Supprimer le scan « {sc['nom']} » de la session ?"):
            return
        sess["s"]["scans"].remove(sc)
        if sess["s"].get("ref") == sc["id"]:
            sess["s"]["ref"] = None
        sess["selection"] = sess["s"]["scans"][-1]["id"] if sess["s"]["scans"] else None
        live["dirty"] = True
        rebuild_liste()
        maj_gain_ui()
        marquer_modifie()
        ecrire(f"Scan « {sc['nom']} » supprimé.")

    def importer():
        chemin = filedialog.askopenfilename(
            title="Importer un scan (Shure .sdb3, CSV MHz,dBm)",
            initialdir=cfg.get("dossier", os.path.expanduser("~/Documents")),
            filetypes=[("Scans", "*.sdb3 *.csv *.txt"), ("Tous les fichiers", "*.*")],
        )
        if not chemin:
            return
        try:
            donnees = lire_shure(chemin)
        except Exception as e:
            ecrire(f"Import impossible : {e}")
            return
        nom = os.path.splitext(os.path.basename(chemin))[0]
        source = "Shure" if chemin.lower().endswith(".sdb3") else "import"
        sc = creer_scan(sess["s"], nom, donnees, resume=f"importé ({source})", source=source)
        sess["selection"] = sc["id"]
        vue["auto"] = True
        live["dirty"] = True
        rebuild_liste()
        maj_gain_ui()
        marquer_modifie()
        ecrire(f"Scan importé : {sc['nom']}  ({len(donnees)} points)")

    # ---- gain du scan sélectionné
    def maj_gain_ui():
        sc = scan_selectionne()
        gain_sync["actif"] = True
        try:
            if sc:
                curseur_gain.set(max(-40, min(40, sc.get("gain", 0.0))))
                var_gain_txt.set(texte_gain(sc))
            else:
                curseur_gain.set(0)
                var_gain_txt.set("Aucun scan sélectionné")
        finally:
            gain_sync["actif"] = False

    def texte_gain(sc):
        t = f"{sc['nom']} : {sc.get('gain', 0.0):+.1f} dB".replace(".", ",")
        if sc.get("corr_f"):
            t += " + correction par bande"
        return t

    def apres_changement_gain(sc):
        sc["corr_ver"] = sc.get("corr_ver", 0)
        var_gain_txt.set(texte_gain(sc))
        det = etiquettes.get(sc["id"])
        if det is not None:
            det.config(text=decrire_scan(sc))
        live["dirty"] = True
        marquer_modifie()

    def sur_gain(valeur):
        if gain_sync["actif"]:
            return
        sc = scan_selectionne()
        if not sc:
            return
        g = round(float(valeur) * 2) / 2
        if g != sc.get("gain", 0.0):
            sc["gain"] = g
            apres_changement_gain(sc)

    curseur_gain.config(command=sur_gain)

    def prerequis_calage():
        sc, ref = scan_selectionne(), scan_ref()
        if not sc:
            ecrire("Sélectionne d'abord le scan à corriger (clic sur son nom).")
            return None
        if not ref:
            ecrire("Coche « Réf. » sur le scan de référence (par exemple ton scan Shure).")
            return None
        if ref is sc:
            ecrire("Le scan sélectionné est aussi la référence : choisis un autre scan à corriger.")
            return None
        return sc, ref

    def donnees_sans_gain(sc):
        """Scan avec sa correction par bande mais sans son gain (le gain est ce qu'on calcule)."""
        f = np.asarray(sc["f"], dtype=float)
        d = np.asarray(sc["d"], dtype=float)
        if sc.get("corr_f"):
            d = d + np.interp(f, sc["corr_f"], sc["corr_d"])
        return dict(zip(f.tolist(), d.tolist()))

    def auto_gain():
        r = prerequis_calage()
        if not r:
            return
        sc, ref = r
        try:
            c = comparer(donnees_sans_gain(sc), donnees_effectives(ref))
        except Exception as e:
            ecrire(f"Calage impossible : {e}")
            return
        g = round(c["offset"], 1)
        if abs(g) > 40:
            ecrire(f"Écart de {g:+.1f} dB : au-delà du fader (±40 dB). Vérifie les deux scans.")
            g = max(-40.0, min(40.0, g))
        sc["gain"] = g
        ecrire(f"Auto : « {sc['nom']} » calé sur « {ref['nom']} » : gain {g:+.1f} dB "
               f"({c['plage'][0]:g} à {c['plage'][1]:g} MHz, {c['n']} points communs).")
        if c["off_signal"] is not None:
            ecrire(f"  écart sur les signaux ({c['n_signal']} pts) : {c['off_signal']:+.1f} dB, "
                   f"sur tout le scan : {c['off_tout']:+.1f} dB")
        ecrire("  par sous-bande : " + " ; ".join(f"{a:g}-{b:g} : {d:+.1f}" for a, b, d in c["bandes"]))
        etendue = max(d for _, _, d in c["bandes"]) - min(d for _, _, d in c["bandes"])
        if etendue > 3:
            ecrire(f"  Attention : l'écart varie de {etendue:.1f} dB selon la bande. Un seul chiffre ne suffit pas : "
                   "essaie « Auto par bande ».")
        maj_gain_ui()
        apres_changement_gain(sc)

    def auto_bande():
        r = prerequis_calage()
        if not r:
            return
        sc, ref = r
        brut = dict(zip(sc["f"], sc["d"]))
        try:
            fr, co, infos = correction_par_bande(brut, donnees_effectives(ref))
        except Exception as e:
            ecrire(f"Calage impossible : {e}")
            return
        sc["corr_f"], sc["corr_d"] = fr, co
        sc["corr_ver"] = sc.get("corr_ver", 0) + 1
        sc["gain"] = 0.0
        ecrire(f"Auto par bande : « {sc['nom']} » corrigé sur « {ref['nom']} » : {infos['fenetres']} fenêtres de 4 MHz "
               f"({infos['avec_signaux']} calées sur des signaux), correction de {infos['min']:+.1f} à {infos['max']:+.1f} dB.")
        maj_gain_ui()
        apres_changement_gain(sc)

    def reinit_gain():
        sc = scan_selectionne()
        if not sc:
            ecrire("Sélectionne d'abord un scan (clic sur son nom).")
            return
        sc["gain"] = 0.0
        sc.pop("corr_f", None)
        sc.pop("corr_d", None)
        sc["corr_ver"] = sc.get("corr_ver", 0) + 1
        maj_gain_ui()
        apres_changement_gain(sc)
        ecrire(f"Gain et correction de « {sc['nom']} » remis à zéro.")

    # ---- export et image
    def exporter():
        sc = scan_selectionne()
        if not sc:
            ecrire("Sélectionne d'abord un scan dans la liste (clic sur son nom).")
            return
        chemin = filedialog.asksaveasfilename(
            title="Exporter le CSV pour WWB",
            defaultextension=".csv",
            initialfile=nom_fichier(f"{sess['s']['nom']}_{sc['nom']}", "csv"),
            initialdir=cfg.get("dossier_export", cfg.get("dossier", os.path.expanduser("~/Desktop"))),
            filetypes=[("Fichier CSV", "*.csv")],
        )
        if not chemin:
            return
        donnees = donnees_effectives(sc)
        n = n_lissage(var_k.get())
        lisse = var_liss_ex.get() and n > 1
        if lisse:
            donnees = lisser(donnees, n)
        ecrire_csv(donnees, chemin)
        cfg["dossier_export"] = os.path.dirname(chemin)
        details = []
        if lisse:
            details.append(f"lissage {n} points")
        if sc.get("gain"):
            details.append(f"gain {sc['gain']:+.1f} dB")
        if sc.get("corr_f"):
            details.append("correction par bande")
        ecrire(f"CSV exporté : {chemin}  [{sc['nom']}]" + (f"  ({', '.join(details)})" if details else ""))

    def enregistrer_image():
        if not graphe:
            return
        chemin = filedialog.asksaveasfilename(
            title="Enregistrer l'image de l'afficheur",
            defaultextension=".png",
            initialfile=nom_fichier(sess["s"]["nom"], "png"),
            initialdir=cfg.get("dossier_export", cfg.get("dossier", os.path.expanduser("~/Desktop"))),
            filetypes=[("Image PNG", "*.png")],
        )
        if chemin:
            fig.savefig(chemin, dpi=150, facecolor=fig.get_facecolor())
            ecrire(f"Image enregistrée : {chemin}")

    # ---- lancement des scans
    def demarrer(viewer):
        try:
            tranches = generer_tranches(float(var_deb.get().replace(",", ".")),
                                        float(var_fin.get().replace(",", ".")))
        except ValueError:
            ecrire("Vérifie la plage (ex : 470 à 670, fin > début).")
            return
        try:
            passes = 0 if var_inf.get() else max(1, int(var_passes.get()))
        except ValueError:
            ecrire("Le nombre de passes doit être un entier (ou coche « jusqu'à l'arrêt »).")
            return
        rbw = var_rbw.get().strip().lower()
        att = var_att.get().strip().lower()
        try:
            if rbw != "auto":
                rbw = str(int(float(rbw)))
                assert 3 <= int(rbw) <= 850
            if att != "auto":
                att = str(int(float(att)))
                assert 0 <= int(att) <= 31
        except (ValueError, AssertionError):
            ecrire("Vérifie le RBW (auto ou 3 à 850) et l'atténuation (auto ou 0 à 31).")
            return
        p = {
            "tranches": tranches, "passes": passes, "maxhold": var_max.get(),
            "demo": var_demo.get(), "appliquer": var_appl.get(), "rbw": rbw, "att": att,
            "lna": var_lna.get(), "viewer": viewer, "reset": reset,
        }
        sauver_reglages()
        info = f"{len(tranches)} tranche(s) : {tranches[0][0]:g} à {tranches[-1][1]:g} MHz, pas 25 kHz"
        if viewer:
            info += ". Astuce : une plage courte (ex : 610 à 650) se rafraîchit plus vite."
        else:
            info += f", {passes} passe(s)" if passes else ", passes jusqu'à l'arrêt"
        ecrire(info)
        stop.clear()
        reset.clear()
        live["scan"] = True
        live["acc"].clear()
        live["courant"] = None
        vue["auto"] = True
        prog["value"] = 0
        btn_go.config(state="disabled")
        btn_view.config(state="disabled")
        btn_stop.config(state="normal")
        btn_reset.config(state="normal" if viewer else "disabled")
        threading.Thread(target=travail, args=(p, lambda k, t: q.put((k, t)), stop), daemon=True).start()

    def lancer():
        demarrer(False)

    def visualiser():
        demarrer(True)

    def effacer_cumul():
        reset.set()
        ecrire("Cumul effacé.")

    def arreter():
        stop.set()
        ecrire("Arrêt demandé, la tranche en cours se termine (ce qui est mesuré est conservé)...")

    def terminer_scan(res):
        donnees = res["donnees"]
        sc = creer_scan(sess["s"], var_nom.get().strip(), donnees, resume=res["resume"], passes=res["passes"],
                        mode=res["mode"], source="tinySA", duree=res["duree"])
        sess["selection"] = sc["id"]
        live["acc"].clear()
        live["scan"] = False
        live["dirty"] = True
        vue["auto"] = True
        rebuild_liste()
        maj_gain_ui()
        marquer_modifie()
        ecrire(f"Scan « {sc['nom']} » ajouté à la session (pas encore enregistrée : bouton Enregistrer).")
        top = pics(donnees, 5)
        ecrire("Pics les plus forts (lissés) : " + " ; ".join(
            f"{f:.2f} MHz {v:.1f} dBm" + (f" (canal {c})" if c else "") for f, v, c in top))

    # ---- réglages mémorisés
    def sauver_reglages():
        cfg.update({
            "v": 7, "deb": var_deb.get(), "fin": var_fin.get(), "passes": var_passes.get(), "inf": var_inf.get(),
            "max": var_max.get(), "nom_scan": var_nom.get(), "appl": var_appl.get(), "rbw": var_rbw.get(),
            "att": var_att.get(), "lna": var_lna.get(), "liss_export": var_liss_ex.get(),
            "k": int(var_k.get()), "brut": var_brut.get(), "tnt": var_tnt.get(), "canaux": var_canaux.get(),
        })
        sauver_config(cfg)

    def fermer():
        if not confirmer_perte():
            return
        stop.set()
        sauver_reglages()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", fermer)
    for combo in ("<Command-s>", "<Control-s>"):
        root.bind(combo, lambda e: sauver())

    # ---- lissage, seuil, souris, zoom
    def maj_texte_lissage():
        n = n_lissage(var_k.get())
        var_liss_txt.set("aucun" if n == 1 else f"{n} points ({n * PAS_MHZ * 1000:g} kHz)")

    def sur_curseur(valeur):
        k = int(round(float(valeur)))
        if k != var_k.get():
            var_k.set(k)
            maj_texte_lissage()
            live["dirty"] = True

    curseur.config(command=sur_curseur)
    maj_texte_lissage()

    def sur_seuil(*_):
        txt = var_seuil.get().strip().replace(",", ".")
        try:
            seuil["y"] = float(txt) if txt else None
        except ValueError:
            seuil["y"] = None
        if graphe:
            maj_seuil()
            canvas.draw_idle()

    def maj_seuil():
        if seuil["y"] is None:
            ligne_seuil.set_visible(False)
            txt_seuil.set_visible(False)
            return
        ligne_seuil.set_ydata([seuil["y"], seuil["y"]])
        ligne_seuil.set_visible(True)
        txt_seuil.set_position((0.01, seuil["y"]))
        txt_seuil.set_text(f" seuil {seuil['y']:.1f} dBm")
        txt_seuil.set_visible(True)

    def sur_limites(_):
        if not vue["interne"]:
            vue["auto"] = False          # l'utilisateur a zoomé / déplacé la vue
            live["dirty"] = True

    def vue_complete():
        vue["auto"] = True
        live["dirty"] = True

    def fixer_limites(x=None, y=None):
        vue["interne"] = True
        try:
            if x:
                ax.set_xlim(*x)
            if y:
                ax.set_ylim(*y)
        finally:
            vue["interne"] = False

    def zoomer(facteur, centre=None):
        xmin, xmax = ax.get_xlim()
        c = (xmin + xmax) / 2 if centre is None else centre
        largeur = max(0.2, (xmax - xmin) * facteur)
        gauche = c - (c - xmin) * (largeur / (xmax - xmin))
        ax.set_xlim(gauche, gauche + largeur)       # déclenche sur_limites : la vue n'est plus automatique

    def trouver_pic(ev):
        """Pic le plus proche du curseur parmi les courbes affichées : (distance, piste, x, y) ou None."""
        xmin, xmax = ax.get_xlim()
        tol = (xmax - xmin) * 10 / max(1.0, ax.bbox.width)          # fenêtre de 10 pixels
        meilleur = None
        for p in pistes:
            xs, ys = p["x"], p["y"]
            m = (xs >= ev.xdata - tol) & (xs <= ev.xdata + tol) & ~np.isnan(ys)
            if not m.any():
                continue
            idx = np.where(m)[0]
            k = int(idx[np.argmax(ys[idx])])
            for _ in range(400):                                  # on monte jusqu'au sommet du pic
                if k + 1 < len(ys) and ys[k + 1] > ys[k]:
                    k += 1
                elif k > 0 and ys[k - 1] > ys[k]:
                    k -= 1
                else:
                    break
            px = ax.transData.transform((xs[k], ys[k]))
            dist = abs(px[1] - ev.y)
            if dist < 45 and (meilleur is None or dist < meilleur[0]):
                meilleur = (dist, p, float(xs[k]), float(ys[k]))
        return meilleur

    def cacher_bulle():
        if bulle.get_visible() or marque_pic.get_visible():
            bulle.set_visible(False)
            marque_pic.set_visible(False)
            canvas.draw_idle()

    def sur_clic(ev):
        if ev.inaxes is not ax or ev.xdata is None or ev.button not in (1, None):
            return
        if seuil["y"] is not None:
            bas_y, haut_y = ax.get_ylim()
            if abs(ev.ydata - seuil["y"]) < 0.02 * (haut_y - bas_y):
                seuil["drag"] = True
                return
        pan.update(actif=True, x=ev.x, xlim=ax.get_xlim())

    def sur_mouvement(ev):
        if pan["actif"]:
            x0, x1 = pan["xlim"]
            dx = (ev.x - pan["x"]) * (x1 - x0) / max(1.0, ax.bbox.width)
            ax.set_xlim(x0 - dx, x1 - dx)
            return
        if ev.inaxes is not ax or ev.xdata is None:
            var_curseur.set("")
            cacher_bulle()
            return
        if seuil["drag"]:
            seuil["y"] = round(ev.ydata, 1)
            var_seuil.set(f"{seuil['y']:.1f}")
            maj_seuil()
            canvas.draw_idle()
        c = canal_de(ev.xdata)
        var_curseur.set(f"{ev.xdata:.3f} MHz   {ev.ydata:.1f} dBm" + (f"   (canal {c})" if c else ""))
        pic = trouver_pic(ev) if pistes and not seuil["drag"] else None
        if pic is None:
            cacher_bulle()
            return
        _, p, x, y = pic
        cp = canal_de(x)
        bulle.xy = (x, y)
        bulle.set_text(f"{x:.3f} MHz\n{y:.1f} dBm" + (f"\ncanal {cp}" if cp else "") + f"\n{p['nom']}")
        xmin, xmax = ax.get_xlim()
        bulle.set_position((-120, 14) if x > xmin + 0.7 * (xmax - xmin) else (14, 14))
        bulle.set_visible(True)
        marque_pic.set_data([x], [y])
        marque_pic.set_visible(True)
        canvas.draw_idle()

    def sur_relache(ev):
        seuil["drag"] = False
        pan["actif"] = False

    def sur_molette(ev):
        if ev.inaxes not in (ax, ax_tnt) or ev.xdata is None:
            return
        haut = getattr(ev, "step", 0) > 0 or getattr(ev, "button", "") == "up"
        zoomer(0.8 if haut else 1.25, ev.xdata)

    if graphe:
        canvas.mpl_connect("button_press_event", sur_clic)
        canvas.mpl_connect("motion_notify_event", sur_mouvement)
        canvas.mpl_connect("button_release_event", sur_relache)
        canvas.mpl_connect("scroll_event", sur_molette)
        ax.callbacks.connect("xlim_changed", sur_limites)

    for v in (var_brut, var_tnt, var_canaux):
        v.trace_add("write", lambda *_: live.update(dirty=True))
    var_seuil.trace_add("write", sur_seuil)

    def maj_passes(*_):
        ent_passes.config(state="disabled" if var_inf.get() else "normal")

    var_inf.trace_add("write", maj_passes)
    maj_passes()

    btn_nouv.config(command=nouvelle)
    btn_ouvrir.config(command=ouvrir)
    btn_enreg.config(command=sauver)
    btn_sauver.config(command=sauver_sous)
    btn_go.config(command=lancer)
    btn_view.config(command=visualiser)
    btn_reset.config(command=effacer_cumul)
    btn_stop.config(command=arreter)
    btn_export.config(command=exporter)
    btn_ren.config(command=renommer)
    btn_sup.config(command=supprimer)
    btn_imp.config(command=importer)
    btn_auto.config(command=auto_gain)
    btn_bande.config(command=auto_bande)
    btn_rz.config(command=reinit_gain)
    btn_vue.config(command=vue_complete)
    btn_zp.config(command=lambda: zoomer(0.5))
    btn_zm.config(command=lambda: zoomer(2.0))
    btn_img.config(command=enregistrer_image)

    # ---- file de messages du thread de scan
    def surveiller():
        try:
            while True:
                kind, texte = q.get_nowait()
                try:
                    traiter(kind, texte)
                except Exception:
                    noter_erreur(f"message « {kind} »")
        except queue.Empty:
            pass
        root.after(100, surveiller)

    def traiter(kind, texte):
        if True:
            if True:
                if kind == "log":
                    ecrire(texte)
                elif kind == "result":
                    terminer_scan(texte)
                elif kind == "plan":
                    live["plan"] = texte
                    live["acc"].clear()
                    live["now"] = ([], [])
                    live["dirty"] = True
                elif kind == "live":
                    idx, f, now, acc, _passe = texte
                    live["acc"][idx] = (f, acc)
                    live["now"] = (f, now)
                    if idx < len(live["plan"]):
                        live["courant"] = live["plan"][idx]
                    live["dirty"] = True
                elif kind == "status":
                    var_statut.set(texte)
                    live["titre"] = texte
                    live["dirty"] = True
                elif kind == "progress":
                    prog["value"] = 100 * texte
                elif kind == "done":
                    btn_go.config(state="normal")
                    btn_view.config(state="normal")
                    btn_stop.config(state="disabled")
                    btn_reset.config(state="disabled")
                    var_statut.set("Terminé.")
                    prog["value"] = 0
                    live["now"] = ([], [])
                    live["courant"] = None
                    live["scan"] = False
                    live["dirty"] = True

    # ---- dessin de l'afficheur
    def courbe_scan(sc, n):
        cle = (sc["id"], n, sc.get("gain", 0.0), sc.get("corr_ver", 0), len(sc.get("corr_f", [])))
        if cle not in cache:
            if len(cache) > 60:
                cache.clear()
            f, d = scan_effectif(sc)
            cache[cle] = courbe(f, d, n)
        return cache[cle]

    erreurs = []          # historique des erreurs d'affichage (pour le bouton Debug)
    deja = set()

    def noter_erreur(lieu):
        import traceback
        trace = traceback.format_exc()
        erreurs.append(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {lieu}\n{trace}")
        del erreurs[:-30]
        cle = trace.strip().splitlines()[-1]
        if cle not in deja:
            deja.add(cle)
            ecrire(f"ERREUR d'affichage ({lieu}) : {cle}  -> bouton Debug pour le détail. L'affichage continue.")

    def rapport_debug():
        import platform
        L = [f"tinySA -> WWB : rapport de debug  {datetime.datetime.now().isoformat(timespec='seconds')}",
             f"Python {platform.python_version()}  {platform.platform()}"]
        try:
            import matplotlib
            L.append(f"matplotlib {matplotlib.__version__}  numpy {np.__version__}")
        except Exception:
            pass
        L.append(f"Session : {sess['s']['nom']}  modifiée={sess['modifie']}  fichier={sess['chemin']}")
        L.append(f"Scan en cours : {live['scan']}  tranches dans le plan : {len(live['plan'])}  "
                 f"tranches reçues : {len(live['acc'])}  dessin en attente : {live['dirty']}")
        L.append(f"Lissage : {n_lissage(var_k.get())} points  brut={var_brut.get()}  vue auto={vue['auto']}")
        if graphe:
            L.append(f"Axes : x={tuple(round(v, 3) for v in ax.get_xlim())}  y={tuple(round(v, 1) for v in ax.get_ylim())}")
        for sc in sess["s"]["scans"]:
            d = np.asarray(sc["d"], dtype=float)
            bad = int((~np.isfinite(d)).sum()) if d.size else 0
            L.append(f"  - {sc['nom']} : {len(sc['f'])} pts, visible={sc.get('visible', True)}, gain={sc.get('gain', 0)}, "
                     f"corr={'oui' if sc.get('corr_f') else 'non'}, valeurs invalides={bad}"
                     + (f", min/max={np.nanmin(d):.1f}/{np.nanmax(d):.1f}" if d.size and bad < d.size else ""))
        L.append("")
        L.append(f"Erreurs d'affichage enregistrées : {len(erreurs)}")
        L += erreurs[-10:] or ["(aucune)"]
        L.append("")
        L.append("Journal de l'appli :")
        L.append(log.get("1.0", "end") if hasattr(log, "get") else "")
        return "\n".join(L)

    def ouvrir_debug():
        texte = rapport_debug()
        fen = tk.Toplevel(root)
        fen.title("Debug")
        zone_t = tk.Text(fen, width=110, height=32)
        zone_t.pack(fill="both", expand=True)
        zone_t.insert("1.0", texte)

        def enreg():
            chemin = filedialog.asksaveasfilename(
                title="Enregistrer le rapport", defaultextension=".txt",
                initialfile=nom_fichier("debug_tinysa", "txt"),
                initialdir=cfg.get("dossier_export", os.path.expanduser("~/Desktop")),
                filetypes=[("Texte", "*.txt")])
            if chemin:
                with open(chemin, "w", encoding="utf-8") as f:
                    f.write(texte)
                ecrire(f"Rapport enregistré : {chemin}")
        ttk.Button(fen, text="Enregistrer le rapport...", command=enreg).pack(pady=6)

    btn_debug.config(command=ouvrir_debug)

    def rafraichir():
        """Redessine l'afficheur (au plus 4 fois par seconde). Une erreur ne l'arrête jamais."""
        try:
            dessiner()
        except Exception:
            noter_erreur("dessin du graphe")
            live["dirty"] = True
        root.after(250, rafraichir)

    def dessiner():
        if graphe and live["dirty"]:
            live["dirty"] = False
            n = n_lissage(var_k.get())
            bornes_x = []
            poignees = []
            pistes.clear()
            vus = set()
            courbes = []                    # (xs, lissée, couleur) pour les remplissages
            for sc in sess["s"]["scans"]:
                vus.add(sc["id"])
                l = lignes.get(sc["id"])
                if l is None:
                    l = lignes[sc["id"]] = (ax.plot([], [], linewidth=0.7, alpha=0.3)[0], ax.plot([], [], linewidth=1.8)[0])
                brut, lis = l
                visible = bool(sc.get("visible", True)) and bool(sc["f"])
                brut.set_color(sc["couleur"])
                lis.set_color(sc["couleur"])
                brut.set_visible(visible and var_brut.get() and n > 1)
                lis.set_visible(visible)
                lis.set_label(sc["nom"] + (" (réf.)" if sess["s"].get("ref") == sc["id"] else ""))
                if visible:
                    xs, b, li = courbe_scan(sc, n)
                    brut.set_data(xs, b)
                    lis.set_data(xs, li)
                    courbes.append((xs, li, sc["couleur"]))
                    pistes.append({"nom": sc["nom"], "x": np.asarray(xs, dtype=float), "y": np.asarray(li, dtype=float)})
                    bornes_x += [sc["f"][0], sc["f"][-1]]
                    poignees.append(lis)
            for ident in list(lignes):
                if ident not in vus:
                    for l in lignes.pop(ident):
                        l.remove()

            # scan en cours (même lissage que le reste)
            if live["acc"]:
                fs, vs = [], []
                for idx in sorted(live["acc"]):
                    f, a = live["acc"][idx]
                    fs += f
                    vs += a
                xs, b, li = courbe(fs, vs, n)
                ligne_live_raw.set_data(xs, b)
                ligne_live.set_data(xs, li)
                ligne_live_raw.set_visible(var_brut.get() and n > 1)
                ligne_live.set_visible(True)
                pistes.append({"nom": "Scan en cours", "x": np.asarray(xs, dtype=float), "y": np.asarray(li, dtype=float)})
                bornes_x += [fs[0], fs[-1]]
                poignees.append(ligne_live)
            else:
                ligne_live_raw.set_visible(False)
                ligne_live.set_visible(False)
            if live["now"][0]:
                ligne_now.set_data(*live["now"])
                ligne_now.set_visible(True)
            else:
                ligne_now.set_visible(False)

            if live["plan"] and (live["scan"] or not bornes_x):
                bornes_x += [live["plan"][0][0], live["plan"][-1][1]]

            # limites : x automatique tant que l'utilisateur n'a pas zoomé ; y toujours ajusté à ce qu'on voit
            if vue["auto"] and bornes_x:
                fixer_limites(x=(min(bornes_x), max(bornes_x)))
            xmin, xmax = ax.get_xlim()
            vals = []
            for p in pistes:
                m = (p["x"] >= xmin) & (p["x"] <= xmax) & ~np.isnan(p["y"])
                if m.any():
                    vals.append((float(p["y"][m].min()), float(p["y"][m].max())))
            if vals:
                lo = min(v[0] for v in vals)
                hi = max(v[1] for v in vals)
                fixer_limites(y=(math.floor((lo - 4) / 5) * 5, math.ceil((hi + 4) / 5) * 5))

            # remplissages, repères TNT, tranche en cours
            for a in deco:
                try:
                    a.remove()
                except Exception:
                    pass
            deco.clear()
            for xs, li, couleur in courbes:
                deco.append(ax.fill_between(xs, li, -300, color=couleur, alpha=0.18, linewidth=0))
            deco.extend(dessiner_tnt(ax, ax_tnt, xmin, xmax, lire_canaux(var_canaux.get()), var_tnt.get()))
            if live["scan"] and live["courant"]:
                a, b = live["courant"]
                deco.append(ax.axvspan(a, b, color="gold", alpha=0.15, linewidth=0))
            maj_seuil()

            ax.set_title(live.get("titre", "") if live["scan"] else
                         (sess["s"]["nom"] + (f"  -  {len(poignees)} scan(s) affiché(s)" if poignees else "")),
                         color="#dddddd", fontsize=12)
            if poignees:
                leg = ax.legend(handles=poignees, loc="upper right", fontsize=9, facecolor="#1e1e1e",
                                edgecolor="#555555", labelcolor="white")
            elif ax.get_legend():
                ax.get_legend().remove()
            canvas.draw_idle()

    # ---- démarrage
    chemin_prec = cfg.get("session")
    if chemin_prec and os.path.exists(chemin_prec):
        try:
            installer_session(charger_session(chemin_prec), chemin_prec)
            ecrire(f"Session rouverte : {chemin_prec}  ({len(sess['s']['scans'])} scan(s))")
        except Exception as e:
            ecrire(f"Dernière session non rouverte : {e}")
    rebuild_liste()
    maj_gain_ui()
    maj_titre()
    sur_seuil()
    rafraichir()
    surveiller()
    root.mainloop()


if __name__ == "__main__":
    main()
