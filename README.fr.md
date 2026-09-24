# Bot delta-neutre — funding rate sur Hyperliquid

*[English version](README.md)*

Un bot de trading automatisé qui encaisse le *funding* des contrats perpétuels **sans prendre de pari sur la direction du prix**. Python, API Hyperliquid, exécuté en conditions réelles.

> ### Cadre du projet
> C'est une **expérimentation pédagogique**, et elle n'a **pas vocation à être rentable**. L'objectif était d'apprendre à construire proprement la mécanique d'un bot qui manipule de l'argent : boucle de contrôle, gestion de la marge, réconciliation d'état, garde-fous, validation. La taille des positions est volontairement minuscule — à cette échelle, les frais mangent l'essentiel du rendement, et c'est assumé depuis le début.
>
> Ce dépôt est publié à titre de démonstration technique. Ce n'est pas un conseil en investissement, et ce n'est pas un logiciel à lancer sans l'avoir lu et compris.

---

## 1. La stratégie, en une minute

Sur un contrat perpétuel, un mécanisme appelé **funding** transfère régulièrement de l'argent entre acheteurs et vendeurs pour maintenir le prix du contrat proche du prix réel. Quand le marché est haussier, ce sont les vendeurs à découvert qui sont payés.

L'idée est donc de se mettre vendeur pour encaisser ce flux — mais être vendeur, c'est parier que le prix baisse. On neutralise ce pari en achetant **simultanément la même quantité au comptant** :

| Si le prix du Bitcoin… | Jambe au comptant | Jambe à découvert | Total |
|---|---|---|---|
| monte | gagne | perd autant | ≈ 0 |
| baisse | perd | gagne autant | ≈ 0 |

Le résultat lié au prix s'annule — c'est le sens de **delta-neutre**. Ce qui reste, c'est le funding.

**Là où c'est intéressant :** la neutralité porte sur le prix, pas sur le reste. Trois risques subsistent, et tout le code est construit autour d'eux.

1. **La liquidation.** La jambe vendeuse a besoin d'une garantie déposée. Si le prix monte fort, cette garantie fond, et la plateforme ferme la position d'office. C'est le risque principal.
2. **La parité.** Les deux jambes ne sont pas exactement le même actif : l'une est un jeton qui *représente* du Bitcoin. S'il décroche, la compensation ne fonctionne plus.
3. **Les frictions.** Frais et glissement de prix, d'autant plus visibles que la position est petite.

---

## 2. Comment le bot mesure le risque

Le bot ne mémorise rien entre deux cycles : il relit l'état réel du compte et recalcule tout à partir de six grandeurs.

| Variable | Signification |
|---|---|
| `mark` | Prix officiel du perpétuel calculé par la plateforme — **celui qui déclenche une liquidation** |
| `spot_px` | Prix du jeton sur son propre carnet d'ordres — celui auquel la jambe longue se revendrait |
| `N` | Taille de la position vendeuse, en dollars |
| `M` | Marge déposée côté perpétuel : le matelas qui absorbe les pertes |
| `R` | Réserve d'USDC disponible |
| `mm` | Marge de maintenance imposée par la plateforme, lue à chaque démarrage |

Les deux prix sont lus **séparément**, et c'est un choix de conception : utiliser un prix unique pour les deux jambes rendrait le bot structurellement aveugle à une perte de parité du jeton — précisément le risque n° 2.

### L'indicateur central

```
s = M / N − mm
```

`s` se lit comme un pourcentage : **de combien le prix peut monter avant que la position vendeuse soit liquidée**. Plus le nombre est grand, plus la marge de manœuvre est confortable.

| Zone | `s` | Comportement |
|---|---|---|
| 🟢 Verte | ≥ 22 % | Ne rien faire |
| 🟡 Jaune | 18–22 % | Observer |
| 🟠 Orange | ≤ 18 % | Alerter : la marge doit être renforcée |
| 🔴 Rouge | < 14 % | Réduire les deux jambes, automatiquement et simultanément |

Deux détails qui comptent :

- **La zone jaune est une zone morte volontaire.** Sans écart entre le seuil qui déclenche et la cible qui est visée, le bot réagirait à chaque frémissement de prix. C'est le principe d'un thermostat.
- **`s` ne regarde que la jambe vendeuse.** Un `s` excellent peut parfaitement coexister avec des jambes déséquilibrées. La vérification de la neutralité est donc un contrôle *séparé* — confondre les deux est un piège classique.

### D'où viennent les seuils

D'une analyse de 9 ans de prix du Bitcoin en données horaires : sur les deux dernières années, aucune hausse supérieure à 12 % sur une fenêtre de 8 heures. La fenêtre de 8 heures représente une nuit sans surveillance, et le plancher à 18 % laisse une marge au-delà du pire cas observé. Ces seuils sont calibrés sur le Bitcoin et ne se transposeraient pas tels quels à un actif plus volatil.

---

## 3. La boucle de contrôle

Toutes les 60 secondes, le même cycle :

| # | Étape |
|---|---|
| 1 | Lire l'état réel sur la plateforme — position, marge, soldes, les deux prix |
| 2 | Vérifier que le coupe-circuit n'est pas déclenché |
| 3 | **Réconcilier** : les deux jambes existent-elles ? leur écart dépasse-t-il 25 % ? |
| 4 | **Contrôler la dérive** : l'écart entre les jambes dépasse-t-il 2 % ? (détection précoce d'une perte de parité) |
| 5 | Calculer `s`, en déduire la zone, agir si nécessaire |
| 6 | Réarmer le dispositif « homme mort » |

Le principe directeur : **ne jamais faire confiance à une valeur mémorisée**. Un bot qui raisonne sur son propre état interne finit toujours par diverger de la réalité.

---

## 4. Architecture

| Fichier | Rôle |
|---|---|
| `phase_c_testnet.py` | **Le moteur** — lecture d'état, décisions, exécution, garde-fous |
| `phase_d_mainnet.py` | Lanceur de production : **aucune logique**, uniquement des réglages |
| `paper_sim.py` | Simulateur : fait tourner le vrai moteur contre une fausse plateforme |
| `phase_a_read_only.py`, `phase_b_dry_run.py` | Étapes initiales : lecture seule, puis décisions sans exécution |
| `Transmissions/` | Journal de conception : décisions et faits vérifiés |

**Une seule copie de la logique.** Le lanceur de production ne contient aucune règle : il ne peut donc pas diverger du moteur validé. Dupliquer du code entre deux variantes, c'est garantir qu'un correctif finira par n'être appliqué qu'à l'une des deux.

---

## 5. Les garde-fous

C'est le cœur du projet — davantage que la stratégie elle-même.

| Garde-fou | Ce qu'il empêche |
|---|---|
| **Mode non armé par défaut** | Le bot calcule tout mais n'envoie rien. Les quatre seules fonctions capables d'émettre un ordre commencent toutes par cette vérification, au niveau le plus bas |
| **Confirmation clavier** | En production, chaque démarrage armé exige une saisie manuelle |
| **Contrôle du type de compte** | Refuse de démarrer si la configuration du compte rendrait les mesures fausses |
| **Réconciliation** | Refuse d'agir si une jambe manque ou si l'écart entre les deux dépasse 25 % |
| **Alerte de dérive** | Signale tout écart supérieur à 2 % entre les jambes |
| **Contrôles avant ouverture** | Vérifie marge et liquidités *avant* d'ouvrir, plutôt que d'échouer à mi-chemin |
| **Débouclage automatique** | Si la deuxième jambe échoue, la première est refermée immédiatement — jamais de position à nu |
| **Coupe-circuit** | Après trois échecs d'envoi consécutifs, le bot cesse d'agir et se contente d'observer |
| **Plafond de taille** | Refuse de construire une position au-delà d'une limite fixée |
| **Seuil de poussière** | Considère une jambe comme fermée en dessous de 1 $ : le trading réel laisse toujours des miettes d'arrondi |
| **Identifiant d'ordre unique** | Un renvoi accidentel ne crée pas de doublon |
| **Dispositif « homme mort »** | Réarmé à chaque cycle : si le bot meurt, la plateforme annule d'elle-même les ordres en attente |
| **Clé d'agent uniquement** | La clé utilisée peut négocier mais **ne peut pas retirer de fonds**. Elle vit en variable d'environnement, jamais dans un fichier |

---

## 6. Validation

### Une capacité nouvelle à la fois

| Phase | Ce qu'elle ajoute |
|---|---|
| A | Lecture seule du compte |
| B | Décisions calculées, aucun envoi |
| C | Premiers ordres signés, sur réseau de test |
| D | Production |

Chaque phase n'ajoute qu'une seule capacité : une panne se localise donc immédiatement. Les phases A et B ne peuvent structurellement pas perdre d'argent — le code d'envoi n'y est même pas chargé.

### Le simulateur

Un réseau de test ne permet pas de provoquer une crise à la demande, et attendre qu'un vrai marché bouge n'est ni reproductible ni rapide. D'où `paper_sim.py` : il remplace la plateforme par une fausse plateforme, mais exécute **le vrai moteur** — pas une copie. On écrit un scénario de prix, on vérifie le comportement, en quelques secondes.

| Scénario | Ce qu'il prouve |
|---|---|
| Marché calme | Aucune action, aucune fausse alerte sur 24 h |
| Hausse modérée | L'alerte de renflouement part au bon moment, sans passer d'ordre |
| Réserve épuisée | La réduction coupe bien les deux jambes dans le même cycle |
| Échec d'une jambe | La première est refermée aussitôt, le compte revient à plat |
| Pannes d'envoi en série | Le coupe-circuit s'arrête après trois échecs, sans jamais vendre une jambe seule |
| Perte de parité du jeton | Alerte précoce, puis blocage complet au-delà du seuil |
| Saut de prix nocturne (+20 %) | La position survit et se rééquilibre |
| Saut de prix nocturne (+27 %) | Liquidation, mais perte bornée à la marge ; la jambe au comptant est intacte et le bot se bloque au lieu d'agir sur un état incohérent |

Le modèle du simulateur intègre les frais réels, le glissement, le funding horaire et la règle de liquidation de la plateforme. En production, la distance à la liquidation annoncée par la plateforme a correspondu à la prédiction du simulateur **au dixième de point**.

---

## 7. Contraintes de la plateforme qui ont façonné la conception

Trois contraintes techniques, découvertes en mesurant plutôt qu'en supposant, ont directement modifié l'architecture.

**Le réseau de test ne peut pas accueillir cette stratégie.** Elle exige que le même actif soit négociable des deux côtés avec assez de liquidité. Un balayage complet — 212 contrats perpétuels et 1 261 paires au comptant — montre qu'aucun actif n'y réunit les deux conditions. Le réseau de test a donc servi à valider la chaîne de signature et d'envoi, et le simulateur a pris le relais pour la logique.

**Une clé d'agent ne peut pas déplacer de fonds entre les portefeuilles.** Elle peut passer des ordres, mais les virements internes exigent la signature du compte principal — celle qu'on refuse par principe de confier à un programme automatisé. Le renflouement de la marge est donc une opération manuelle, et le bot alerte au lieu de tenter un virement voué à l'échec. Détail important : s'obstiner déclencherait le coupe-circuit, qui stopperait aussi les réductions — donc la seule protection automatique restante.

**Prix affiché ≠ prix de liquidation.** Les graphiques montrent le prix des transactions, alors que marge et liquidation se calculent sur un prix agrégé distinct. L'écart est infime en temps normal, et s'élargit exactement quand ça compte : pendant un mouvement violent.

---

## 8. Limites connues

Assumées, pas ignorées.

- Les alertes sont des lignes de journal : **personne n'est réveillé**. C'est le manque le plus important.
- Le bot dépend d'un ordinateur allumé. Éteint, la position survit mais n'est plus surveillée.
- Il sait **réduire** une position, jamais l'agrandir : après plusieurs réductions, elle rétrécit jusqu'à une intervention manuelle.
- Le renflouement de la marge est manuel (voir §7).
- L'excédent de marge n'est jamais rapatrié automatiquement.
- À petite taille, les frais d'un aller-retour représentent plusieurs jours de funding.
- Le risque de parité est **signalé**, pas couvert.

---

## 9. Installation et utilisation

```bash
python -m pip install hyperliquid-python-sdk
```

Les identifiants vivent dans le terminal, jamais sur le disque :

```powershell
$env:HL_ACCOUNT_ADDRESS = "0xVotreAdresse"
$env:HL_AGENT_KEY = (Get-Clipboard).Trim()
```

```bash
python phase_d_mainnet.py --once                    # lit et décide, n'envoie rien
python phase_d_mainnet.py --enter <montant> --arm   # ouvre une position neutre
python phase_d_mainnet.py --arm                     # surveillance toutes les 60 s
python phase_d_mainnet.py --flatten --arm           # referme les deux jambes
```

Le mode `--once` emprunte exactement le même chemin de code que le mode armé, mais n'envoie rien : c'est la façon de vérifier une décision avant de l'autoriser.

Le simulateur ne demande ni clé ni connexion :

```bash
python paper_sim.py              # tous les scénarios
python paper_sim.py depeg        # un seul
python paper_sim.py --verbose    # avec les journaux du bot
```

---

## 10. Stack

Python 3 · [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) · `eth-account` pour la signature · aucune dépendance de calcul externe.

Licence MIT.
