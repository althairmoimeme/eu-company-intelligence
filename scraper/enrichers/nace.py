"""NACE Rev.2 / NAF / ISIC / SIC code hierarchy, normalisation et recherche sémantique.

Nomenclatures supportées :
  - NACE Rev.2   : "46.47", "46.47Z" (lettre NAF ignorée)
  - NAF / APE    : "46.47Z" → normalisé en "46.47"
  - ISIC Rev.4   : "4647"  → converti en NACE
  - SIC (UK 2007): "5190"  → mappé vers division NACE

Niveaux hiérarchiques :
  Section  (1 lettre)   : A … U
  Division (2 chiffres) : 01 … 99
  Groupe   (4 chars)    : 01.1 … 99.9
  Classe   (5 chars)    : 01.11 … 99.00
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from functools import lru_cache

# ── Sections ─────────────────────────────────────────────────────────────────
SECTIONS: dict[str, str] = {
    "A": "Agriculture, sylviculture et pêche",
    "B": "Industries extractives",
    "C": "Industrie manufacturière",
    "D": "Production et distribution d'électricité, de gaz",
    "E": "Production et distribution d'eau ; assainissement, gestion des déchets",
    "F": "Construction",
    "G": "Commerce ; réparation d'automobiles et de motocycles",
    "H": "Transports et entreposage",
    "I": "Hébergement et restauration",
    "J": "Information et communication",
    "K": "Activités financières et d'assurance",
    "L": "Activités immobilières",
    "M": "Activités spécialisées, scientifiques et techniques",
    "N": "Activités de services administratifs et de soutien",
    "O": "Administration publique et défense",
    "P": "Enseignement",
    "Q": "Santé humaine et action sociale",
    "R": "Arts, spectacles et activités récréatives",
    "S": "Autres activités de services",
    "T": "Activités des ménages en tant qu'employeurs",
    "U": "Activités extra-territoriales",
}

# ── Section → divisions (ranges) ─────────────────────────────────────────────
_SECTION_RANGES = [
    ("A", range(1, 4)),
    ("B", range(5, 10)),
    ("C", range(10, 34)),
    ("D", range(35, 36)),
    ("E", range(36, 40)),
    ("F", range(41, 44)),
    ("G", range(45, 48)),
    ("H", range(49, 54)),
    ("I", range(55, 57)),
    ("J", range(58, 64)),
    ("K", range(64, 67)),
    ("L", range(68, 69)),
    ("M", range(69, 76)),
    ("N", range(77, 83)),
    ("O", range(84, 85)),
    ("P", range(85, 86)),
    ("Q", range(86, 89)),
    ("R", range(90, 94)),
    ("S", range(94, 97)),
    ("T", range(97, 99)),
    ("U", range(99, 100)),
]
DIVISION_TO_SECTION: dict[str, str] = {}
SECTION_TO_DIVISIONS: dict[str, list[str]] = {s: [] for s in SECTIONS}
for _sec, _rng in _SECTION_RANGES:
    for _d in _rng:
        _key = f"{_d:02d}"
        DIVISION_TO_SECTION[_key] = _sec
        SECTION_TO_DIVISIONS[_sec].append(_key)

# ── Full NACE Rev.2 labels (FR) — divisions + groupes + classes clés ─────────
# Format: code → label_fr
# Divisions = 2 chiffres ("46"), Groupes = 4 chars ("46.4"), Classes = 5 chars ("46.47")
NACE_LABELS: dict[str, str] = {
    # ── A Agriculture ─────────────────────────────────────────────────────────
    "01": "Cultures et productions animales, chasse et services annexes",
    "01.1": "Cultures non permanentes",
    "01.11": "Culture de céréales, légumineuses et graines oléagineuses",
    "01.12": "Culture du riz",
    "01.13": "Culture de légumes, melons, racines et tubercules",
    "01.14": "Culture de la canne à sucre",
    "01.15": "Culture du tabac",
    "01.16": "Culture de plantes à fibres",
    "01.19": "Autres cultures non permanentes",
    "01.2": "Cultures permanentes",
    "01.21": "Culture de la vigne",
    "01.22": "Culture de fruits tropicaux et subtropicaux",
    "01.23": "Culture d'agrumes",
    "01.24": "Culture de fruits à pépins et à noyaux",
    "01.25": "Culture d'autres fruits d'arbres ou arbustes et de fruits à coque",
    "01.26": "Culture de fruits oléagineux",
    "01.27": "Culture de plantes à boissons",
    "01.28": "Culture de plantes à épices, aromatiques, médicinales et pharmaceutiques",
    "01.29": "Autres cultures permanentes",
    "01.3": "Reproduction de plantes",
    "01.30": "Reproduction de plantes",
    "01.4": "Production animale",
    "01.41": "Élevage de vaches laitières",
    "01.42": "Élevage d'autres bovins et buffles",
    "01.43": "Élevage de chevaux et d'autres équidés",
    "01.44": "Élevage de chameaux et camélidés",
    "01.45": "Élevage d'ovins et de caprins",
    "01.46": "Élevage de porcins",
    "01.47": "Élevage de volailles",
    "01.49": "Élevage d'autres animaux",
    "01.5": "Culture et élevage associés",
    "01.50": "Culture et élevage associés",
    "01.6": "Activités de soutien à l'agriculture et traitement primaire des récoltes",
    "01.61": "Activités de soutien aux cultures",
    "01.62": "Activités de soutien à la production animale",
    "01.63": "Traitement primaire des récoltes",
    "01.64": "Traitement des semences",
    "01.7": "Chasse, piégeage et services annexes",
    "01.70": "Chasse, piégeage et services annexes",
    "02": "Sylviculture et exploitation forestière",
    "02.1": "Sylviculture et autres activités forestières",
    "02.10": "Sylviculture et autres activités forestières",
    "02.2": "Exploitation forestière",
    "02.20": "Exploitation forestière",
    "02.3": "Récolte de produits forestiers non ligneux",
    "02.30": "Récolte de produits forestiers non ligneux",
    "02.4": "Services de soutien à l'exploitation forestière",
    "02.40": "Services de soutien à l'exploitation forestière",
    "03": "Pêche et aquaculture",
    "03.1": "Pêche",
    "03.11": "Pêche en mer",
    "03.12": "Pêche en eau douce",
    "03.2": "Aquaculture",
    "03.21": "Aquaculture en mer",
    "03.22": "Aquaculture en eau douce",

    # ── B Industries extractives ───────────────────────────────────────────────
    "05": "Extraction de houille et de lignite",
    "05.1": "Extraction de houille",
    "05.10": "Extraction de houille",
    "05.2": "Extraction de lignite",
    "05.20": "Extraction de lignite",
    "06": "Extraction d'hydrocarbures",
    "06.1": "Extraction de pétrole brut",
    "06.10": "Extraction de pétrole brut",
    "06.2": "Extraction de gaz naturel",
    "06.20": "Extraction de gaz naturel",
    "07": "Extraction de minerais métalliques",
    "07.1": "Extraction de minerais de fer",
    "07.10": "Extraction de minerais de fer",
    "07.2": "Extraction de minerais de métaux non ferreux",
    "07.21": "Extraction de minerais d'uranium et de thorium",
    "07.29": "Extraction d'autres minerais de métaux non ferreux",
    "08": "Autres industries extractives",
    "08.1": "Extraction de pierres, de sables et d'argiles",
    "08.11": "Extraction de pierres ornementales et de construction, de calcaire, etc.",
    "08.12": "Exploitation de gravières et sablières, extraction d'argiles",
    "08.9": "Activités extractives n.c.a.",
    "08.91": "Extraction des minéraux chimiques et d'engrais minéraux",
    "08.92": "Extraction de tourbe",
    "08.93": "Production de sel",
    "08.99": "Autres activités extractives n.c.a.",
    "09": "Services de soutien aux industries extractives",
    "09.1": "Activités de soutien à l'extraction d'hydrocarbures",
    "09.10": "Activités de soutien à l'extraction d'hydrocarbures",
    "09.9": "Activités de soutien aux autres industries extractives",
    "09.90": "Activités de soutien aux autres industries extractives",

    # ── C Industrie manufacturière ─────────────────────────────────────────────
    "10": "Industries alimentaires",
    "10.1": "Transformation et conservation de la viande et préparation de produits à base de viande",
    "10.11": "Transformation et conservation de la viande de boucherie",
    "10.12": "Transformation et conservation de la viande de volaille",
    "10.13": "Préparation de produits à base de viande",
    "10.2": "Transformation et conservation de poisson, de crustacés et de mollusques",
    "10.20": "Transformation et conservation de poisson, crustacés et mollusques",
    "10.3": "Transformation et conservation de fruits et légumes",
    "10.31": "Transformation et conservation de pommes de terre",
    "10.32": "Préparation de jus de fruits et légumes",
    "10.39": "Autre transformation et conservation de fruits et légumes",
    "10.4": "Fabrication d'huiles et graisses végétales et animales",
    "10.41": "Fabrication d'huiles et graisses",
    "10.42": "Fabrication de margarine et graisses comestibles similaires",
    "10.5": "Fabrication de produits laitiers",
    "10.51": "Exploitation de laiteries et fabrication de fromages",
    "10.52": "Fabrication de glaces et sorbets",
    "10.6": "Travail des grains ; fabrication de produits amylacés",
    "10.61": "Travail des grains",
    "10.62": "Fabrication de produits amylacés",
    "10.7": "Fabrication de produits de boulangerie-pâtisserie et de pâtes alimentaires",
    "10.71": "Fabrication de pain et de pâtisserie fraîche",
    "10.72": "Fabrication de biscuits, biscottes et pâtisseries de conservation",
    "10.73": "Fabrication de pâtes alimentaires",
    "10.8": "Fabrication d'autres produits alimentaires",
    "10.81": "Fabrication de sucre",
    "10.82": "Fabrication de cacao, chocolat et de produits de confiserie",
    "10.83": "Transformation du thé et du café",
    "10.84": "Fabrication de condiments, sauces, préparations pour soupes et bouillons",
    "10.85": "Fabrication de plats préparés",
    "10.86": "Fabrication d'aliments homogénéisés et diététiques",
    "10.89": "Fabrication d'autres produits alimentaires n.c.a.",
    "10.9": "Fabrication d'aliments pour animaux",
    "10.91": "Fabrication d'aliments pour animaux de ferme",
    "10.92": "Fabrication d'aliments pour animaux de compagnie",
    "11": "Fabrication de boissons",
    "11.01": "Production de boissons alcooliques distillées",
    "11.02": "Production de vin (de raisin)",
    "11.03": "Fabrication de cidre et autres vins de fruits",
    "11.04": "Production d'autres boissons fermentées non distillées",
    "11.05": "Fabrication de bière",
    "11.06": "Fabrication de malt",
    "11.07": "Industrie des eaux minérales et autres eaux embouteillées et des boissons rafraîchissantes",
    "12": "Fabrication de produits à base de tabac",
    "12.00": "Fabrication de produits à base de tabac",
    "13": "Fabrication de textiles",
    "13.1": "Préparation et filature de fibres textiles",
    "13.10": "Préparation et filature de fibres textiles",
    "13.2": "Tissage de textiles",
    "13.20": "Tissage de textiles",
    "13.3": "Ennoblissement textile",
    "13.30": "Ennoblissement textile",
    "13.9": "Fabrication d'autres textiles",
    "13.91": "Fabrication d'étoffes à maille",
    "13.92": "Fabrication d'articles textiles, sauf habillement",
    "13.93": "Fabrication de tapis et moquettes",
    "13.94": "Fabrication de ficelles, cordes et filets",
    "13.95": "Fabrication de non-tissés, sauf habillement",
    "13.96": "Fabrication d'autres textiles techniques et industriels",
    "13.99": "Fabrication d'autres textiles n.c.a.",
    "14": "Industrie de l'habillement",
    "14.1": "Fabrication de vêtements, autres qu'en fourrure",
    "14.11": "Fabrication de vêtements en cuir",
    "14.12": "Fabrication de vêtements de travail",
    "14.13": "Fabrication d'autres vêtements de dessus",
    "14.14": "Fabrication de vêtements de dessous",
    "14.19": "Fabrication d'autres vêtements et accessoires",
    "14.2": "Fabrication d'articles en fourrure",
    "14.20": "Fabrication d'articles en fourrure",
    "14.3": "Fabrication d'articles à maille",
    "14.31": "Fabrication d'articles chaussants à maille",
    "14.39": "Fabrication d'autres articles à maille",
    "15": "Industrie du cuir et de la chaussure",
    "15.1": "Apprêt et tannage des cuirs ; préparation et teinture des fourrures",
    "15.11": "Apprêt et tannage des cuirs ; préparation et teinture des fourrures",
    "15.12": "Fabrication d'articles de voyage, de maroquinerie et de sellerie",
    "15.2": "Fabrication de chaussures",
    "15.20": "Fabrication de chaussures",
    "16": "Travail du bois et fabrication d'articles en bois et en liège",
    "16.1": "Sciage et rabotage du bois",
    "16.10": "Sciage et rabotage du bois",
    "16.2": "Fabrication de produits en bois, liège, vannerie et sparterie",
    "16.21": "Fabrication de placages et de panneaux de bois",
    "16.22": "Fabrication de parquets assemblés",
    "16.23": "Fabrication de charpentes et d'autres menuiseries",
    "16.24": "Fabrication d'emballages en bois",
    "16.29": "Fabrication d'objets divers en bois ; fabrication d'articles en liège, vannerie",
    "17": "Industrie du papier et du carton",
    "17.1": "Fabrication de pâte à papier, de papier et de carton",
    "17.11": "Fabrication de pâte à papier",
    "17.12": "Fabrication de papier et de carton",
    "17.2": "Fabrication d'articles en papier ou en carton",
    "17.21": "Fabrication de papier et carton ondulés et d'emballages en papier ou carton",
    "17.22": "Fabrication d'articles en papier à usage sanitaire ou domestique",
    "17.23": "Fabrication d'articles de papeterie",
    "17.24": "Fabrication de papiers peints",
    "17.29": "Fabrication d'autres articles en papier ou carton",
    "18": "Imprimerie et reproduction d'enregistrements",
    "18.1": "Imprimerie et services annexes",
    "18.11": "Imprimerie de journaux",
    "18.12": "Autre imprimerie (labeur)",
    "18.13": "Activités de prépresse",
    "18.14": "Reliure et activités connexes",
    "18.2": "Reproduction d'enregistrements",
    "18.20": "Reproduction d'enregistrements",
    "19": "Cokéfaction et raffinage",
    "19.1": "Cokéfaction",
    "19.10": "Cokéfaction",
    "19.2": "Raffinage du pétrole",
    "19.20": "Raffinage du pétrole",
    "20": "Industrie chimique",
    "20.1": "Fabrication de produits chimiques de base, d'engrais et composés azotés",
    "20.11": "Fabrication de gaz industriels",
    "20.12": "Fabrication de colorants et de pigments",
    "20.13": "Fabrication d'autres produits chimiques inorganiques de base",
    "20.14": "Fabrication d'autres produits chimiques organiques de base",
    "20.15": "Fabrication de produits azotés et d'engrais",
    "20.16": "Fabrication de matières plastiques de base",
    "20.17": "Fabrication de caoutchouc synthétique",
    "20.2": "Fabrication de pesticides et d'autres produits agrochimiques",
    "20.20": "Fabrication de pesticides et d'autres produits agrochimiques",
    "20.3": "Fabrication de peintures, vernis, encres et mastics",
    "20.30": "Fabrication de peintures, vernis, encres d'imprimerie et mastics",
    "20.4": "Fabrication de savons, détergents et produits d'entretien",
    "20.41": "Fabrication de savons, détergents et produits d'entretien",
    "20.42": "Fabrication de parfums et de produits pour la toilette",
    "20.5": "Fabrication d'autres produits chimiques",
    "20.51": "Fabrication de produits explosifs",
    "20.52": "Fabrication de colles",
    "20.53": "Fabrication d'huiles essentielles",
    "20.59": "Fabrication d'autres produits chimiques n.c.a.",
    "20.6": "Fabrication de fibres artificielles ou synthétiques",
    "20.60": "Fabrication de fibres artificielles ou synthétiques",
    "21": "Industrie pharmaceutique",
    "21.1": "Fabrication de produits pharmaceutiques de base",
    "21.10": "Fabrication de produits pharmaceutiques de base",
    "21.2": "Fabrication de préparations pharmaceutiques",
    "21.20": "Fabrication de préparations pharmaceutiques",
    "22": "Fabrication de produits en caoutchouc et en plastique",
    "22.1": "Fabrication de produits en caoutchouc",
    "22.11": "Fabrication et rechapage de pneumatiques",
    "22.19": "Fabrication d'autres articles en caoutchouc",
    "22.2": "Fabrication de produits en matières plastiques",
    "22.21": "Fabrication de plaques, feuilles, tubes et profilés en matières plastiques",
    "22.22": "Fabrication d'emballages en matières plastiques",
    "22.23": "Fabrication d'éléments en matières plastiques pour la construction",
    "22.29": "Fabrication d'autres articles en matières plastiques",
    "23": "Fabrication d'autres produits minéraux non métalliques",
    "23.1": "Fabrication de verre et d'articles en verre",
    "23.11": "Fabrication de verre plat",
    "23.12": "Façonnage et transformation du verre plat",
    "23.13": "Fabrication de verre creux",
    "23.14": "Fabrication de fibres de verre",
    "23.19": "Fabrication et façonnage d'autres articles en verre",
    "23.2": "Fabrication de produits réfractaires",
    "23.20": "Fabrication de produits réfractaires",
    "23.3": "Fabrication de matériaux de construction en terre cuite",
    "23.31": "Fabrication de carreaux en céramique",
    "23.32": "Fabrication de briques, tuiles et produits de construction en terre cuite",
    "23.4": "Fabrication d'autres produits céramiques",
    "23.41": "Fabrication d'articles céramiques à usage domestique ou ornemental",
    "23.42": "Fabrication d'appareils sanitaires en céramique",
    "23.43": "Fabrication d'isolateurs et pièces isolantes en céramique",
    "23.44": "Fabrication d'autres produits céramiques à usage technique",
    "23.49": "Fabrication d'autres produits céramiques",
    "23.5": "Fabrication de ciment, chaux et plâtre",
    "23.51": "Fabrication de ciment",
    "23.52": "Fabrication de chaux et plâtre",
    "23.6": "Fabrication d'ouvrages en béton, en ciment ou en plâtre",
    "23.61": "Fabrication d'éléments en béton pour la construction",
    "23.62": "Fabrication d'éléments en plâtre pour la construction",
    "23.63": "Fabrication de béton prêt à l'emploi",
    "23.64": "Fabrication de mortiers et bétons secs",
    "23.65": "Fabrication de produits en fibre-ciment",
    "23.69": "Fabrication d'autres ouvrages en béton, en ciment ou plâtre",
    "23.7": "Taille, façonnage et finissage de pierres",
    "23.70": "Taille, façonnage et finissage de pierres",
    "23.9": "Fabrication de produits abrasifs et de produits minéraux non métalliques n.c.a.",
    "23.91": "Fabrication de produits abrasifs",
    "23.99": "Fabrication d'autres produits minéraux non métalliques n.c.a.",
    "24": "Métallurgie",
    "24.1": "Sidérurgie",
    "24.10": "Sidérurgie",
    "24.2": "Fabrication de tubes, tuyaux, profilés creux et accessoires correspondants en acier",
    "24.20": "Fabrication de tubes, tuyaux, profilés creux et accessoires",
    "24.3": "Fabrication d'autres produits de première transformation de l'acier",
    "24.31": "Étirage à froid de barres",
    "24.32": "Laminage à froid de feuillards",
    "24.33": "Profilage à froid par formage ou pliage",
    "24.34": "Tréfilage à froid",
    "24.4": "Production de métaux précieux et d'autres métaux non ferreux",
    "24.41": "Production de métaux précieux",
    "24.42": "Métallurgie de l'aluminium",
    "24.43": "Métallurgie du plomb, du zinc ou de l'étain",
    "24.44": "Métallurgie du cuivre",
    "24.45": "Métallurgie des autres métaux non ferreux",
    "24.46": "Élaboration et transformation de matières nucléaires",
    "24.5": "Fonderie",
    "24.51": "Fonderie de fonte",
    "24.52": "Fonderie d'acier",
    "24.53": "Fonderie de métaux légers",
    "24.54": "Fonderie d'autres métaux non ferreux",
    "25": "Fabrication de produits métalliques, sauf machines et équipements",
    "25.1": "Fabrication d'éléments en métal pour la construction",
    "25.11": "Fabrication de structures métalliques et de parties de structures",
    "25.12": "Fabrication de portes et fenêtres en métal",
    "25.2": "Fabrication de réservoirs, citernes et conteneurs métalliques",
    "25.21": "Fabrication de radiateurs et de chaudières pour le chauffage central",
    "25.29": "Fabrication d'autres réservoirs, citernes et conteneurs métalliques",
    "25.3": "Fabrication de générateurs de vapeur, sauf chaudières pour le chauffage central",
    "25.30": "Fabrication de générateurs de vapeur",
    "25.4": "Fabrication d'armes et de munitions",
    "25.40": "Fabrication d'armes et de munitions",
    "25.5": "Forge, emboutissage, estampage ; métallurgie des poudres",
    "25.50": "Forge, emboutissage, estampage ; métallurgie des poudres",
    "25.6": "Traitement et revêtement des métaux ; usinage",
    "25.61": "Traitement et revêtement des métaux",
    "25.62": "Décolletage, frappe, estampage",
    "25.7": "Fabrication de coutellerie, d'outillage et de quincaillerie",
    "25.71": "Fabrication de coutellerie",
    "25.72": "Fabrication de serrures et de ferrures",
    "25.73": "Fabrication d'outillage",
    "25.9": "Fabrication d'autres produits métalliques",
    "25.91": "Fabrication de fûts et emballages métalliques similaires",
    "25.92": "Fabrication d'emballages métalliques légers",
    "25.93": "Fabrication d'articles en fils métalliques, de chaînes et de ressorts",
    "25.94": "Fabrication de vis et de boulons",
    "25.99": "Fabrication d'autres produits métalliques n.c.a.",
    "26": "Fabrication de produits informatiques, électroniques et optiques",
    "26.1": "Fabrication de composants et cartes électroniques",
    "26.11": "Fabrication de composants électroniques",
    "26.12": "Fabrication de cartes électroniques assemblées",
    "26.2": "Fabrication d'ordinateurs et d'équipements périphériques",
    "26.20": "Fabrication d'ordinateurs et d'équipements périphériques",
    "26.3": "Fabrication d'équipements de communication",
    "26.30": "Fabrication d'équipements de communication",
    "26.4": "Fabrication de produits électroniques grand public",
    "26.40": "Fabrication de produits électroniques grand public",
    "26.5": "Fabrication d'instruments et d'appareils de mesure, d'essai et de navigation",
    "26.51": "Fabrication d'instruments et appareils de mesure, d'essai et de navigation",
    "26.52": "Horlogerie",
    "26.6": "Fabrication d'équipements d'irradiation médicale et d'équipements électro-médicaux",
    "26.60": "Fabrication d'équipements d'irradiation médicale et électro-médicaux",
    "26.7": "Fabrication de matériels optique et photographique",
    "26.70": "Fabrication de matériels optique et photographique",
    "26.8": "Fabrication de supports magnétiques et optiques",
    "26.80": "Fabrication de supports magnétiques et optiques",
    "27": "Fabrication d'équipements électriques",
    "27.1": "Fabrication de moteurs, génératrices et transformateurs électriques",
    "27.11": "Fabrication de moteurs, génératrices et transformateurs électriques",
    "27.12": "Fabrication de matériel de distribution et de commande électrique",
    "27.2": "Fabrication de piles et accumulateurs électriques",
    "27.20": "Fabrication de piles et accumulateurs électriques",
    "27.3": "Fabrication de fils et câbles et de matériel d'installation électrique",
    "27.31": "Fabrication de câbles de fibres optiques",
    "27.32": "Fabrication d'autres fils et câbles électroniques ou électriques",
    "27.33": "Fabrication de matériel d'installation électrique",
    "27.4": "Fabrication d'appareils d'éclairage électrique",
    "27.40": "Fabrication d'appareils d'éclairage électrique",
    "27.5": "Fabrication d'appareils ménagers",
    "27.51": "Fabrication d'appareils électroménagers",
    "27.52": "Fabrication d'appareils ménagers non électriques",
    "27.9": "Fabrication d'autres matériels électriques",
    "27.90": "Fabrication d'autres matériels électriques",
    "28": "Fabrication de machines et équipements n.c.a.",
    "28.1": "Fabrication de machines d'usage général",
    "28.11": "Fabrication de moteurs et turbines, sauf moteurs d'avions et de véhicules",
    "28.12": "Fabrication d'équipements hydrauliques et pneumatiques",
    "28.13": "Fabrication d'autres pompes et compresseurs",
    "28.14": "Fabrication d'autres articles de robinetterie",
    "28.15": "Fabrication d'engrenages et d'organes mécaniques de transmission",
    "28.2": "Fabrication d'autres machines d'usage général",
    "28.21": "Fabrication de fours et brûleurs",
    "28.22": "Fabrication de matériels de levage et de manutention",
    "28.23": "Fabrication de machines et d'équipements de bureau (sauf ordinateurs)",
    "28.24": "Fabrication d'outillage portatif à moteur incorporé",
    "28.25": "Fabrication d'équipements aérauliques et frigorifiques industriels",
    "28.29": "Fabrication d'autres machines d'usage général n.c.a.",
    "28.3": "Fabrication de machines agricoles et forestières",
    "28.30": "Fabrication de machines agricoles et forestières",
    "28.4": "Fabrication de machines de formage des métaux et de machines-outils",
    "28.41": "Fabrication de machines de formage des métaux",
    "28.49": "Fabrication d'autres machines-outils",
    "28.9": "Fabrication d'autres machines d'usage spécifique",
    "28.91": "Fabrication de machines pour la métallurgie",
    "28.92": "Fabrication de machines pour l'extraction ou la construction",
    "28.93": "Fabrication de machines pour l'industrie agro-alimentaire",
    "28.94": "Fabrication de machines pour les industries textiles",
    "28.95": "Fabrication de machines pour les industries du papier et du carton",
    "28.96": "Fabrication de machines pour les industries du plastique et du caoutchouc",
    "28.99": "Fabrication d'autres machines d'usage spécifique n.c.a.",
    "29": "Industrie automobile",
    "29.1": "Construction de véhicules automobiles",
    "29.10": "Construction de véhicules automobiles",
    "29.2": "Fabrication de carrosseries et remorques",
    "29.20": "Fabrication de carrosseries et remorques",
    "29.3": "Fabrication d'équipements automobiles",
    "29.31": "Fabrication d'équipements électriques et électroniques automobiles",
    "29.32": "Fabrication d'autres équipements automobiles",
    "30": "Fabrication d'autres matériels de transport",
    "30.1": "Construction navale",
    "30.11": "Construction de navires et de structures flottantes",
    "30.12": "Construction de bateaux de plaisance",
    "30.2": "Construction de locomotives et d'autre matériel ferroviaire roulant",
    "30.20": "Construction de locomotives et d'autre matériel ferroviaire roulant",
    "30.3": "Construction aéronautique et spatiale",
    "30.30": "Construction aéronautique et spatiale et machinerie connexe",
    "30.4": "Construction de véhicules militaires de combat",
    "30.40": "Construction de véhicules militaires de combat",
    "30.9": "Fabrication de matériels de transport n.c.a.",
    "30.91": "Fabrication de motocycles",
    "30.92": "Fabrication de bicyclettes et de véhicules pour invalides",
    "30.99": "Fabrication d'autres équipements de transport n.c.a.",
    "31": "Fabrication de meubles",
    "31.0": "Fabrication de meubles",
    "31.01": "Fabrication de meubles de bureau et de magasin",
    "31.02": "Fabrication de meubles de cuisine",
    "31.03": "Fabrication de matelas",
    "31.09": "Fabrication d'autres meubles",
    "32": "Autres industries manufacturières",
    "32.1": "Fabrication d'articles de joaillerie, bijouterie et articles similaires",
    "32.11": "Frappe de monnaie",
    "32.12": "Fabrication d'articles de joaillerie et bijouterie",
    "32.13": "Fabrication d'articles de bijouterie fantaisie et articles similaires",
    "32.2": "Fabrication d'instruments de musique",
    "32.20": "Fabrication d'instruments de musique",
    "32.3": "Fabrication d'articles de sport",
    "32.30": "Fabrication d'articles de sport",
    "32.4": "Fabrication de jeux et jouets",
    "32.40": "Fabrication de jeux et jouets",
    "32.5": "Fabrication d'instruments et de fournitures à usage médical et dentaire",
    "32.50": "Fabrication d'instruments et de fournitures à usage médical et dentaire",
    "32.9": "Activités manufacturières n.c.a.",
    "32.91": "Fabrication d'articles de brosserie",
    "32.99": "Autres activités manufacturières n.c.a.",
    "33": "Réparation et installation de machines et d'équipements",
    "33.1": "Réparation d'ouvrages en métaux, de machines et équipements",
    "33.11": "Réparation d'ouvrages en métaux",
    "33.12": "Réparation de machines et équipements mécaniques",
    "33.13": "Réparation de matériels électroniques et optiques",
    "33.14": "Réparation d'équipements électriques",
    "33.15": "Réparation et maintenance de navires et bateaux",
    "33.16": "Réparation et maintenance d'aéronefs et d'engins spatiaux",
    "33.17": "Réparation et maintenance d'autres équipements de transport",
    "33.19": "Réparation d'autres équipements",
    "33.2": "Installation de machines et d'équipements industriels",
    "33.20": "Installation de machines et d'équipements industriels",

    # ── D Énergie ───────────────────────────────────────────────────────────────
    "35": "Production et distribution d'électricité, de gaz, de vapeur et d'air conditionné",
    "35.1": "Production, transport et distribution d'électricité",
    "35.11": "Production d'électricité",
    "35.12": "Transport d'électricité",
    "35.13": "Distribution d'électricité",
    "35.14": "Commerce d'électricité",
    "35.2": "Production et distribution de combustibles gazeux",
    "35.21": "Production de gaz",
    "35.22": "Distribution de combustibles gazeux par conduites",
    "35.23": "Commerce de gaz par conduites",
    "35.3": "Production et distribution de vapeur et d'air conditionné",
    "35.30": "Production et distribution de vapeur et d'air conditionné",

    # ── E Eau, déchets ──────────────────────────────────────────────────────────
    "36": "Captage, traitement et distribution d'eau",
    "36.00": "Captage, traitement et distribution d'eau",
    "37": "Collecte et traitement des eaux usées",
    "37.00": "Collecte et traitement des eaux usées",
    "38": "Collecte, traitement et élimination des déchets ; récupération",
    "38.1": "Collecte des déchets",
    "38.11": "Collecte des déchets non dangereux",
    "38.12": "Collecte des déchets dangereux",
    "38.2": "Traitement et élimination des déchets",
    "38.21": "Traitement et élimination des déchets non dangereux",
    "38.22": "Traitement et élimination des déchets dangereux",
    "38.3": "Récupération de déchets",
    "38.31": "Démantèlement d'épaves",
    "38.32": "Récupération de déchets triés",
    "39": "Dépollution et autres services de gestion des déchets",
    "39.00": "Dépollution et autres services de gestion des déchets",

    # ── F Construction ─────────────────────────────────────────────────────────
    "41": "Construction de bâtiments",
    "41.1": "Promotion immobilière",
    "41.10": "Promotion immobilière",
    "41.2": "Construction de bâtiments résidentiels et non résidentiels",
    "41.20": "Construction de bâtiments résidentiels et non résidentiels",
    "42": "Génie civil",
    "42.1": "Construction de routes et de voies ferrées",
    "42.11": "Construction de routes et autoroutes",
    "42.12": "Construction de voies ferrées de surface et souterraines",
    "42.13": "Construction de ponts et tunnels",
    "42.2": "Construction de réseaux et de lignes de communication",
    "42.21": "Construction de réseaux pour fluides",
    "42.22": "Construction de réseaux électriques et de télécommunications",
    "42.9": "Construction d'autres ouvrages de génie civil",
    "42.91": "Construction d'ouvrages maritimes et fluviaux",
    "42.99": "Construction d'autres ouvrages de génie civil n.c.a.",
    "43": "Travaux de construction spécialisés",
    "43.1": "Démolition et préparation des sites",
    "43.11": "Travaux de démolition",
    "43.12": "Travaux de préparation des sites",
    "43.13": "Forages et sondages",
    "43.2": "Travaux d'installation électrique, plomberie et autres travaux d'installation",
    "43.21": "Installation électrique",
    "43.22": "Travaux de plomberie et installation de chauffage et de conditionnement d'air",
    "43.29": "Autres travaux d'installation",
    "43.3": "Travaux de finition",
    "43.31": "Travaux de plâtrerie",
    "43.32": "Travaux de menuiserie",
    "43.33": "Travaux de revêtement des sols et des murs",
    "43.34": "Travaux de peinture et vitrerie",
    "43.39": "Autres travaux de finition",
    "43.9": "Autres travaux de construction spécialisés",
    "43.91": "Travaux de couverture",
    "43.99": "Autres travaux de construction spécialisés n.c.a.",

    # ── G Commerce ─────────────────────────────────────────────────────────────
    "45": "Commerce et réparation d'automobiles et de motocycles",
    "45.1": "Commerce de véhicules automobiles",
    "45.11": "Commerce de voitures et de véhicules automobiles légers",
    "45.19": "Commerce d'autres véhicules automobiles",
    "45.2": "Entretien et réparation de véhicules automobiles",
    "45.20": "Entretien et réparation de véhicules automobiles",
    "45.3": "Commerce d'équipements automobiles",
    "45.31": "Commerce de gros d'équipements automobiles",
    "45.32": "Commerce de détail d'équipements automobiles",
    "45.4": "Commerce et réparation de motocycles",
    "45.40": "Commerce et réparation de motocycles",
    "46": "Commerce de gros, à l'exception des automobiles et des motocycles",
    "46.1": "Intermédiaires du commerce de gros",
    "46.11": "Intermédiaires du commerce en matières premières agricoles, animaux vivants, etc.",
    "46.12": "Intermédiaires du commerce en combustibles, métaux, minéraux et produits chimiques",
    "46.13": "Intermédiaires du commerce en bois et matériaux de construction",
    "46.14": "Intermédiaires du commerce en machines, équipements industriels, etc.",
    "46.15": "Intermédiaires du commerce en meubles, articles ménagers et quincaillerie",
    "46.16": "Intermédiaires du commerce en textiles, habillement, chaussures et articles en cuir",
    "46.17": "Intermédiaires du commerce en denrées alimentaires, boissons et tabac",
    "46.18": "Intermédiaires spécialisés dans le commerce d'autres produits spécifiques",
    "46.19": "Intermédiaires du commerce en produits divers",
    "46.2": "Commerce de gros de matières premières agricoles et d'animaux vivants",
    "46.21": "Commerce de gros de céréales, de tabac non manufacturé et d'aliments pour bétail",
    "46.22": "Commerce de gros de fleurs et plantes",
    "46.23": "Commerce de gros d'animaux vivants",
    "46.24": "Commerce de gros de cuirs et peaux",
    "46.3": "Commerce de gros de produits alimentaires, de boissons et de tabac",
    "46.31": "Commerce de gros de fruits et légumes",
    "46.32": "Commerce de gros de viandes et de produits à base de viande",
    "46.33": "Commerce de gros de produits laitiers, œufs, huiles et graisses comestibles",
    "46.34": "Commerce de gros de boissons",
    "46.35": "Commerce de gros de produits à base de tabac",
    "46.36": "Commerce de gros de sucre, chocolat et confiserie",
    "46.37": "Commerce de gros de café, thé, cacao et épices",
    "46.38": "Commerce de gros d'autres produits alimentaires",
    "46.39": "Commerce de gros non spécialisé de produits alimentaires, de boissons et de tabac",
    "46.4": "Commerce de gros de biens de consommation non alimentaires",
    "46.41": "Commerce de gros de textiles",
    "46.42": "Commerce de gros d'habillement et de chaussures",
    "46.43": "Commerce de gros d'appareils électroménagers",
    "46.44": "Commerce de gros de vaisselle, verrerie et produits d'entretien",
    "46.45": "Commerce de gros de parfumerie et de produits de beauté",
    "46.46": "Commerce de gros de produits pharmaceutiques",
    "46.47": "Commerce de gros de meubles, de tapis et d'appareils d'éclairage",
    "46.48": "Commerce de gros d'articles d'horlogerie et de bijouterie",
    "46.49": "Commerce de gros d'autres biens domestiques",
    "46.5": "Commerce de gros d'équipements de l'information et de la communication",
    "46.51": "Commerce de gros d'ordinateurs, d'équipements informatiques périphériques et de logiciels",
    "46.52": "Commerce de gros de composants et d'équipements électroniques et de télécommunication",
    "46.6": "Commerce de gros d'autres machines, équipements et fournitures",
    "46.61": "Commerce de gros de matériel agricole",
    "46.62": "Commerce de gros de machines-outils",
    "46.63": "Commerce de gros de machines pour les industries extractives, la construction",
    "46.64": "Commerce de gros de machines pour l'industrie textile",
    "46.65": "Commerce de gros de mobilier de bureau",
    "46.66": "Commerce de gros d'autres machines et équipements de bureau",
    "46.69": "Commerce de gros d'autres machines et équipements",
    "46.7": "Autres commerces de gros spécialisés",
    "46.71": "Commerce de gros de combustibles et de produits annexes",
    "46.72": "Commerce de gros de minerais et métaux",
    "46.73": "Commerce de gros de bois, de matériaux de construction et d'appareils sanitaires",
    "46.74": "Commerce de gros de quincaillerie et fournitures pour plomberie et chauffage",
    "46.75": "Commerce de gros de produits chimiques",
    "46.76": "Commerce de gros d'autres produits intermédiaires",
    "46.77": "Commerce de gros de déchets et débris",
    "46.9": "Commerce de gros non spécialisé",
    "46.90": "Commerce de gros non spécialisé",
    "47": "Commerce de détail, à l'exception des automobiles et des motocycles",
    "47.1": "Commerce de détail en magasin non spécialisé",
    "47.11": "Commerce de détail en magasin non spécialisé à prédominance alimentaire",
    "47.19": "Autre commerce de détail en magasin non spécialisé",
    "47.2": "Commerce de détail alimentaire en magasin spécialisé",
    "47.21": "Commerce de détail de fruits et légumes en magasin spécialisé",
    "47.22": "Commerce de détail de viandes et produits à base de viande",
    "47.23": "Commerce de détail de poissons, crustacés et mollusques",
    "47.24": "Commerce de détail de pain, pâtisserie et confiserie",
    "47.25": "Commerce de détail de boissons",
    "47.26": "Commerce de détail de produits à base de tabac",
    "47.29": "Autre commerce de détail alimentaire en magasin spécialisé",
    "47.3": "Commerce de détail de carburants en magasin spécialisé",
    "47.30": "Commerce de détail de carburants en magasin spécialisé",
    "47.4": "Commerce de détail d'équipements de l'information et de la communication",
    "47.41": "Commerce de détail d'ordinateurs, d'unités périphériques et de logiciels",
    "47.42": "Commerce de détail de matériels de télécommunication",
    "47.43": "Commerce de détail de matériels audio et vidéo",
    "47.5": "Commerce de détail d'autres équipements du foyer",
    "47.51": "Commerce de détail de textiles",
    "47.52": "Commerce de détail de quincaillerie, peintures et verres",
    "47.53": "Commerce de détail de tapis, moquettes et revêtements de murs et de sols",
    "47.54": "Commerce de détail d'appareils électroménagers",
    "47.59": "Commerce de détail de meubles, appareils d'éclairage et autres articles de ménage",
    "47.6": "Commerce de détail de produits culturels et de loisirs en magasin spécialisé",
    "47.61": "Commerce de détail de livres",
    "47.62": "Commerce de détail de journaux et papeterie",
    "47.63": "Commerce de détail d'enregistrements musicaux et vidéo",
    "47.64": "Commerce de détail d'articles de sport",
    "47.65": "Commerce de détail de jeux et jouets",
    "47.7": "Commerce de détail d'autres articles en magasin spécialisé",
    "47.71": "Commerce de détail d'habillement",
    "47.72": "Commerce de détail de chaussures et articles en cuir",
    "47.73": "Commerce de détail de produits pharmaceutiques",
    "47.74": "Commerce de détail d'articles médicaux et orthopédiques",
    "47.75": "Commerce de détail de parfumerie et de produits de beauté",
    "47.76": "Commerce de détail de fleurs, plantes, semences, engrais",
    "47.77": "Commerce de détail d'articles d'horlogerie et de bijouterie",
    "47.78": "Autre commerce de détail de produits neufs en magasin spécialisé",
    "47.79": "Commerce de détail d'articles d'occasion en magasin",
    "47.8": "Commerce de détail sur éventaires et marchés",
    "47.81": "Commerce de détail alimentaire sur éventaires et marchés",
    "47.82": "Commerce de détail de textiles, habillement sur éventaires",
    "47.89": "Autres commerces de détail sur éventaires et marchés",
    "47.9": "Commerce de détail hors magasin, éventaires ou marchés",
    "47.91": "Vente à distance",
    "47.99": "Autres commerces de détail hors magasin",

    # ── H Transport ────────────────────────────────────────────────────────────
    "49": "Transports terrestres et transport par conduites",
    "49.1": "Transport ferroviaire interurbain de voyageurs",
    "49.10": "Transport ferroviaire interurbain de voyageurs",
    "49.2": "Transports ferroviaires de fret",
    "49.20": "Transports ferroviaires de fret",
    "49.3": "Autres transports terrestres de voyageurs",
    "49.31": "Transports urbains et suburbains de voyageurs",
    "49.32": "Transports de voyageurs par taxis",
    "49.39": "Autres transports terrestres de voyageurs n.c.a.",
    "49.4": "Transports routiers de fret et services de déménagement",
    "49.41": "Transports routiers de fret",
    "49.42": "Services de déménagement",
    "49.5": "Transport par conduites",
    "49.50": "Transport par conduites",
    "50": "Transports par eau",
    "50.1": "Transports maritimes et côtiers de passagers",
    "50.10": "Transports maritimes et côtiers de passagers",
    "50.2": "Transports maritimes et côtiers de fret",
    "50.20": "Transports maritimes et côtiers de fret",
    "50.3": "Transports fluviaux de passagers",
    "50.30": "Transports fluviaux de passagers",
    "50.4": "Transports fluviaux de fret",
    "50.40": "Transports fluviaux de fret",
    "51": "Transports aériens",
    "51.1": "Transports aériens de passagers",
    "51.10": "Transports aériens de passagers",
    "51.2": "Transports aériens de fret et transports spatiaux",
    "51.21": "Transports aériens de fret",
    "51.22": "Transports spatiaux",
    "52": "Entreposage et services auxiliaires des transports",
    "52.1": "Entreposage et stockage",
    "52.10": "Entreposage et stockage",
    "52.2": "Services auxiliaires des transports",
    "52.21": "Services auxiliaires des transports terrestres",
    "52.22": "Services auxiliaires des transports par eau",
    "52.23": "Services auxiliaires des transports aériens",
    "52.24": "Manutention",
    "52.29": "Autres services auxiliaires des transports",
    "53": "Activités de poste et de courrier",
    "53.1": "Activités de poste dans le cadre d'une obligation de service universel",
    "53.10": "Activités de poste dans le cadre d'une obligation de service universel",
    "53.2": "Autres activités de poste et de courrier",
    "53.20": "Autres activités de poste et de courrier",

    # ── I Hôtellerie-Restauration ──────────────────────────────────────────────
    "55": "Hébergement",
    "55.1": "Hôtels et hébergement similaire",
    "55.10": "Hôtels et hébergement similaire",
    "55.2": "Hébergement touristique et autre hébergement de courte durée",
    "55.20": "Hébergement touristique et autre hébergement de courte durée",
    "55.3": "Terrains de camping et parcs pour caravanes ou véhicules de loisirs",
    "55.30": "Terrains de camping et parcs pour caravanes ou véhicules de loisirs",
    "55.9": "Autres hébergements",
    "55.90": "Autres hébergements",
    "56": "Restauration",
    "56.1": "Restaurants et services de restauration mobile",
    "56.10": "Restaurants et services de restauration mobile",
    "56.2": "Traiteurs et autres services de restauration",
    "56.21": "Services des traiteurs",
    "56.29": "Autres services de restauration",
    "56.3": "Débits de boissons",
    "56.30": "Débits de boissons",

    # ── J Information et communication ────────────────────────────────────────
    "58": "Édition",
    "58.1": "Édition de livres, périodiques et autres activités d'édition",
    "58.11": "Édition de livres",
    "58.12": "Édition de répertoires et de fichiers d'adresses",
    "58.13": "Édition de journaux",
    "58.14": "Édition de revues et périodiques",
    "58.19": "Autres activités d'édition",
    "58.2": "Édition de logiciels",
    "58.21": "Édition de jeux électroniques",
    "58.29": "Édition d'autres logiciels",
    "59": "Production de films cinématographiques, de vidéo et de programmes de télévision",
    "59.1": "Activités cinématographiques, vidéo et télévisées",
    "59.11": "Production de films cinématographiques, de vidéo et de programmes de télévision",
    "59.12": "Post-production de films cinématographiques, de vidéo et de programmes de télévision",
    "59.13": "Distribution de films cinématographiques, de vidéo et de programmes de télévision",
    "59.14": "Projection de films cinématographiques",
    "59.2": "Enregistrement sonore et édition musicale",
    "59.20": "Enregistrement sonore et édition musicale",
    "60": "Programmation et diffusion",
    "60.1": "Édition et diffusion de programmes radio",
    "60.10": "Édition et diffusion de programmes radio",
    "60.2": "Programmation de télévision et diffusion de programmes",
    "60.20": "Programmation de télévision et diffusion de programmes",
    "61": "Télécommunications",
    "61.1": "Télécommunications filaires",
    "61.10": "Télécommunications filaires",
    "61.2": "Télécommunications sans fil",
    "61.20": "Télécommunications sans fil",
    "61.3": "Télécommunications par satellite",
    "61.30": "Télécommunications par satellite",
    "61.9": "Autres activités de télécommunication",
    "61.90": "Autres activités de télécommunication",
    "62": "Programmation informatique, conseil et autres activités informatiques",
    "62.0": "Programmation informatique, conseil et autres activités informatiques",
    "62.01": "Programmation informatique",
    "62.02": "Conseil informatique",
    "62.03": "Gestion d'installations informatiques",
    "62.09": "Autres activités informatiques",
    "63": "Services d'information",
    "63.1": "Traitement de données, hébergement et activités connexes ; portails internet",
    "63.11": "Traitement de données, hébergement et activités connexes",
    "63.12": "Portails internet",
    "63.9": "Autres services d'information",
    "63.91": "Activités des agences de presse",
    "63.99": "Autres services d'information n.c.a.",

    # ── K Finance et assurance ─────────────────────────────────────────────────
    "64": "Activités des services financiers, hors assurance et caisses de retraite",
    "64.1": "Intermédiation monétaire",
    "64.11": "Activités de banque centrale",
    "64.19": "Autres activités d'intermédiation monétaire",
    "64.2": "Activités des sociétés holding",
    "64.20": "Activités des sociétés holding",
    "64.3": "Fonds de placement et entités financières similaires",
    "64.30": "Fonds de placement et entités financières similaires",
    "64.9": "Autres activités des services financiers, hors assurance et caisses de retraite",
    "64.91": "Crédit-bail",
    "64.92": "Autre octroi de crédit",
    "64.99": "Autres activités des services financiers n.c.a.",
    "65": "Assurance",
    "65.1": "Assurance",
    "65.11": "Assurance vie",
    "65.12": "Autres assurances",
    "65.2": "Réassurance",
    "65.20": "Réassurance",
    "65.3": "Caisses de retraite",
    "65.30": "Caisses de retraite",
    "66": "Activités auxiliaires de services financiers et d'assurance",
    "66.1": "Activités auxiliaires de services financiers, hors assurance et caisses de retraite",
    "66.11": "Administration de marchés financiers",
    "66.12": "Courtage de valeurs mobilières et de marchandises",
    "66.19": "Autres activités auxiliaires de services financiers",
    "66.2": "Activités auxiliaires d'assurance et de caisses de retraite",
    "66.21": "Évaluation des risques et dommages",
    "66.22": "Activités des agents et courtiers d'assurances",
    "66.29": "Autres activités auxiliaires d'assurance et de caisses de retraite",
    "66.3": "Gestion de fonds",
    "66.30": "Gestion de fonds",

    # ── L Immobilier ───────────────────────────────────────────────────────────
    "68": "Activités immobilières",
    "68.1": "Activités des marchands de biens immobiliers",
    "68.10": "Activités des marchands de biens immobiliers",
    "68.2": "Location et exploitation de biens immobiliers propres ou loués",
    "68.20": "Location et exploitation de biens immobiliers propres ou loués",
    "68.3": "Activités immobilières pour compte de tiers",
    "68.31": "Agences immobilières",
    "68.32": "Administration de biens immobiliers",

    # ── M Activités spécialisées ───────────────────────────────────────────────
    "69": "Activités juridiques et comptables",
    "69.1": "Activités juridiques",
    "69.10": "Activités juridiques",
    "69.2": "Activités comptables",
    "69.20": "Activités comptables",
    "70": "Activités des sièges sociaux ; conseil de gestion",
    "70.1": "Activités des sièges sociaux",
    "70.10": "Activités des sièges sociaux",
    "70.2": "Conseil de gestion",
    "70.21": "Conseil en relations publiques et en communication",
    "70.22": "Conseil pour les affaires et autres conseils de gestion",
    "71": "Activités d'architecture et d'ingénierie ; activités de contrôle et analyses techniques",
    "71.1": "Activités d'architecture et d'ingénierie",
    "71.11": "Activités d'architecture",
    "71.12": "Activités d'ingénierie",
    "71.2": "Activités de contrôle et analyses techniques",
    "71.20": "Activités de contrôle et analyses techniques",
    "72": "Recherche-développement scientifique",
    "72.1": "Recherche-développement en sciences physiques et naturelles",
    "72.11": "Recherche-développement en biotechnologie",
    "72.19": "Autres activités de recherche-développement en sciences physiques et naturelles",
    "72.2": "Recherche-développement en sciences humaines et sociales",
    "72.20": "Recherche-développement en sciences humaines et sociales",
    "73": "Publicité et études de marché",
    "73.1": "Publicité",
    "73.11": "Création et placement de publicité",
    "73.12": "Régie publicitaire de médias",
    "73.2": "Études de marché et sondages",
    "73.20": "Études de marché et sondages",
    "74": "Autres activités spécialisées, scientifiques et techniques",
    "74.1": "Activités spécialisées de design",
    "74.10": "Activités spécialisées de design",
    "74.2": "Activités photographiques",
    "74.20": "Activités photographiques",
    "74.3": "Traduction et interprétation",
    "74.30": "Traduction et interprétation",
    "74.9": "Autres activités spécialisées, scientifiques et techniques n.c.a.",
    "74.90": "Autres activités spécialisées, scientifiques et techniques n.c.a.",
    "75": "Activités vétérinaires",
    "75.00": "Activités vétérinaires",

    # ── N Services administratifs ──────────────────────────────────────────────
    "77": "Activités de location et location-bail",
    "77.1": "Location et location-bail de véhicules automobiles",
    "77.11": "Location et location-bail de voitures et véhicules automobiles légers",
    "77.12": "Location et location-bail de camions",
    "77.2": "Location et location-bail de biens personnels et domestiques",
    "77.21": "Location et location-bail d'articles de loisirs et de sport",
    "77.22": "Location de vidéocassettes et disques vidéo",
    "77.29": "Location et location-bail d'autres biens personnels et domestiques",
    "77.3": "Location et location-bail d'autres machines, équipements et biens",
    "77.31": "Location et location-bail de machines et équipements agricoles",
    "77.32": "Location et location-bail de machines et équipements de construction",
    "77.33": "Location et location-bail de machines de bureau et matériel informatique",
    "77.34": "Location et location-bail de matériels de transport par eau",
    "77.35": "Location et location-bail de matériels de transport aérien",
    "77.39": "Location et location-bail d'autres machines, équipements et biens",
    "77.4": "Location-bail de propriété intellectuelle et de produits similaires",
    "77.40": "Location-bail de propriété intellectuelle et de produits similaires",
    "78": "Activités liées à l'emploi",
    "78.1": "Activités des agences de placement de main-d'œuvre",
    "78.10": "Activités des agences de placement de main-d'œuvre",
    "78.2": "Activités des agences de travail temporaire",
    "78.20": "Activités des agences de travail temporaire",
    "78.3": "Autre mise à disposition de ressources humaines",
    "78.30": "Autre mise à disposition de ressources humaines",
    "79": "Activités des agences de voyage, voyagistes, services de réservation",
    "79.1": "Activités des agences de voyage et voyagistes",
    "79.11": "Activités des agences de voyage",
    "79.12": "Activités des voyagistes",
    "79.9": "Autres services de réservation et activités connexes",
    "79.90": "Autres services de réservation et activités connexes",
    "80": "Enquêtes et sécurité",
    "80.1": "Activités de sécurité privée",
    "80.10": "Activités de sécurité privée",
    "80.2": "Activités des systèmes de sécurité",
    "80.20": "Activités des systèmes de sécurité",
    "80.3": "Activités d'enquête",
    "80.30": "Activités d'enquête",
    "81": "Services relatifs aux bâtiments et aménagement paysager",
    "81.1": "Activités combinées de soutien lié aux bâtiments",
    "81.10": "Activités combinées de soutien lié aux bâtiments",
    "81.2": "Activités de nettoyage",
    "81.21": "Nettoyage courant des bâtiments",
    "81.22": "Autres activités de nettoyage des bâtiments et nettoyage industriel",
    "81.29": "Autres activités de nettoyage",
    "81.3": "Services d'aménagement paysager",
    "81.30": "Services d'aménagement paysager",
    "82": "Activités administratives et autres activités de soutien aux entreprises",
    "82.1": "Activités administratives",
    "82.11": "Services administratifs combinés de bureau",
    "82.19": "Photocopie, préparation de documents et autres activités de soutien bureautique",
    "82.2": "Activités de centres d'appels",
    "82.20": "Activités de centres d'appels",
    "82.3": "Organisation de foires, salons professionnels et congrès",
    "82.30": "Organisation de foires, salons professionnels et congrès",
    "82.9": "Activités de soutien aux entreprises n.c.a.",
    "82.91": "Activités des agences de recouvrement de factures et des sociétés d'information financière",
    "82.92": "Activités de conditionnement",
    "82.99": "Autres activités de soutien aux entreprises n.c.a.",

    # ── O Administration publique ──────────────────────────────────────────────
    "84": "Administration publique et défense ; sécurité sociale obligatoire",
    "84.1": "Administration générale, économique et sociale",
    "84.11": "Administration publique générale",
    "84.12": "Administration publique (tutelle) de la santé, de la formation, de la culture",
    "84.13": "Administration publique (tutelle) des activités économiques",
    "84.2": "Services de prérogative publique",
    "84.21": "Affaires étrangères",
    "84.22": "Défense",
    "84.23": "Justice",
    "84.24": "Activités d'ordre public et de sécurité",
    "84.25": "Services du feu et de secours",
    "84.3": "Sécurité sociale obligatoire",
    "84.30": "Sécurité sociale obligatoire",

    # ── P Enseignement ─────────────────────────────────────────────────────────
    "85": "Enseignement",
    "85.1": "Enseignement pré-primaire",
    "85.10": "Enseignement pré-primaire",
    "85.2": "Enseignement primaire",
    "85.20": "Enseignement primaire",
    "85.3": "Enseignement secondaire",
    "85.31": "Enseignement secondaire général",
    "85.32": "Enseignement secondaire technique ou professionnel",
    "85.4": "Enseignement supérieur",
    "85.41": "Enseignement post-secondaire non supérieur",
    "85.42": "Enseignement supérieur",
    "85.5": "Autres activités d'enseignement",
    "85.51": "Enseignement de disciplines sportives et d'activités de loisirs",
    "85.52": "Enseignement culturel",
    "85.53": "Enseignement de la conduite",
    "85.59": "Autres enseignements",
    "85.6": "Activités de soutien à l'enseignement",
    "85.60": "Activités de soutien à l'enseignement",

    # ── Q Santé ────────────────────────────────────────────────────────────────
    "86": "Activités pour la santé humaine",
    "86.1": "Activités hospitalières",
    "86.10": "Activités hospitalières",
    "86.2": "Activité des médecins et des dentistes",
    "86.21": "Activité des médecins généralistes",
    "86.22": "Activité des médecins spécialistes",
    "86.23": "Pratique dentaire",
    "86.9": "Autres activités pour la santé humaine",
    "86.90": "Autres activités pour la santé humaine",
    "87": "Hébergement médico-social et social",
    "87.1": "Hébergement médicalisé",
    "87.10": "Hébergement médicalisé",
    "87.2": "Hébergement social pour personnes handicapées mentales, malades mentales et toxicomanes",
    "87.20": "Hébergement social pour personnes handicapées mentales, malades mentales et toxicomanes",
    "87.3": "Hébergement social pour personnes âgées ou handicapées physiques",
    "87.30": "Hébergement social pour personnes âgées ou handicapées physiques",
    "87.9": "Autres activités d'hébergement social",
    "87.90": "Autres activités d'hébergement social",
    "88": "Action sociale sans hébergement",
    "88.1": "Action sociale sans hébergement pour personnes âgées et pour personnes handicapées",
    "88.10": "Action sociale sans hébergement pour personnes âgées et pour personnes handicapées",
    "88.9": "Autre action sociale sans hébergement",
    "88.91": "Action sociale sans hébergement pour jeunes enfants",
    "88.99": "Autre action sociale sans hébergement n.c.a.",

    # ── R Arts, spectacles ─────────────────────────────────────────────────────
    "90": "Activités créatives, artistiques et de spectacle",
    "90.0": "Activités créatives, artistiques et de spectacle",
    "90.01": "Arts du spectacle vivant",
    "90.02": "Activités de soutien au spectacle vivant",
    "90.03": "Création artistique",
    "90.04": "Gestion de salles de spectacle",
    "91": "Bibliothèques, archives, musées et autres activités culturelles",
    "91.0": "Bibliothèques, archives, musées et autres activités culturelles",
    "91.01": "Gestion des bibliothèques et des archives",
    "91.02": "Gestion des musées",
    "91.03": "Gestion des sites et monuments historiques",
    "91.04": "Gestion des jardins botaniques et zoologiques et des réserves naturelles",
    "92": "Organisation de jeux de hasard et d'argent",
    "92.00": "Organisation de jeux de hasard et d'argent",
    "93": "Activités sportives, récréatives et de loisirs",
    "93.1": "Activités sportives",
    "93.11": "Gestion d'installations sportives",
    "93.12": "Activités de clubs de sports",
    "93.13": "Activités des centres de culture physique",
    "93.19": "Autres activités liées au sport",
    "93.2": "Activités récréatives et de loisirs",
    "93.21": "Activités des parcs d'attractions et parcs à thèmes",
    "93.29": "Autres activités récréatives et de loisirs",

    # ── S Autres services ──────────────────────────────────────────────────────
    "94": "Activités des organisations associatives",
    "94.1": "Activités des organisations économiques, patronales et professionnelles",
    "94.11": "Activités des organisations patronales et économiques",
    "94.12": "Activités des organisations professionnelles",
    "94.2": "Activités des syndicats de salariés",
    "94.20": "Activités des syndicats de salariés",
    "94.9": "Activités des autres organisations associatives",
    "94.91": "Activités des organisations religieuses",
    "94.92": "Activités des organisations politiques",
    "94.99": "Activités des autres organisations associatives n.c.a.",
    "95": "Réparation d'ordinateurs et de biens personnels et domestiques",
    "95.1": "Réparation d'ordinateurs et d'équipements de communication",
    "95.11": "Réparation d'ordinateurs et d'équipements périphériques",
    "95.12": "Réparation d'équipements de communication",
    "95.2": "Réparation de biens personnels et domestiques",
    "95.21": "Réparation de produits électroniques grand public",
    "95.22": "Réparation d'appareils électroménagers et d'équipements pour la maison",
    "95.23": "Réparation de chaussures et d'articles en cuir",
    "95.24": "Réparation de meubles et d'articles d'ameublement",
    "95.25": "Réparation d'articles d'horlogerie et de bijouterie",
    "95.29": "Réparation d'autres biens personnels et domestiques",
    "96": "Autres services personnels",
    "96.0": "Autres services personnels",
    "96.01": "Blanchisserie-teinturerie",
    "96.02": "Coiffure et soins de beauté",
    "96.03": "Services funéraires",
    "96.04": "Entretien corporel",
    "96.09": "Autres services personnels n.c.a.",
}

# Add sections to the flat labels
for _sec_code, _sec_label in SECTIONS.items():
    NACE_LABELS[_sec_code] = _sec_label

# ── UK SIC 2007 → NACE section ────────────────────────────────────────────────
SIC_TO_NACE_SECTION: dict[str, str] = {
    "01": "A", "02": "A", "03": "A",
    "05": "B", "06": "B", "07": "B", "08": "B", "09": "B",
    **{str(i): "C" for i in range(10, 34)},
    "35": "D",
    "36": "E", "37": "E", "38": "E", "39": "E",
    "41": "F", "42": "F", "43": "F",
    "45": "G", "46": "G", "47": "G",
    "49": "H", "50": "H", "51": "H", "52": "H", "53": "H",
    "55": "I", "56": "I",
    "58": "J", "59": "J", "60": "J", "61": "J", "62": "J", "63": "J",
    "64": "K", "65": "K", "66": "K",
    "68": "L",
    "69": "M", "70": "M", "71": "M", "72": "M", "73": "M", "74": "M", "75": "M",
    "77": "N", "78": "N", "79": "N", "80": "N", "81": "N", "82": "N",
    "84": "O",
    "85": "P",
    "86": "Q", "87": "Q", "88": "Q",
    "90": "R", "91": "R", "92": "R", "93": "R",
    "94": "S", "95": "S", "96": "S",
    "97": "T", "98": "T",
    "99": "U",
}

# ── ISIC Rev.4 → NACE Rev.2 (différences uniquement) ─────────────────────────
# La quasi-totalité des codes ISIC à 4 chiffres est identique à NACE.
# On liste ici les cas divergents (NACE subdivise ou regroupe différemment).
ISIC4_EXCEPTIONS: dict[str, str] = {
    # ISIC code → équivalent NACE le plus proche
    "0111": "01.11", "0112": "01.12", "0113": "01.13",
    "0121": "01.21", "0122": "01.22", "0123": "01.23",
    "0130": "01.30", "0141": "01.41", "0142": "01.42",
    "0150": "01.50", "0161": "01.61", "0162": "01.62",
    "0170": "01.70", "0210": "02.10", "0220": "02.20",
    "0311": "03.11", "0312": "03.12", "0321": "03.21",
    "3510": "35.11", "3520": "35.21", "3530": "35.30",
    "6411": "64.11", "6419": "64.19", "6420": "64.20",
    "6491": "64.91", "6492": "64.92", "6499": "64.99",
    "7111": "71.11", "7112": "71.12", "7120": "71.20",
}


# ── Normalisation ─────────────────────────────────────────────────────────────

def normalize_code(raw: str | None) -> str | None:
    """Normalize any activity code to NACE '46.90' format.

    Handles: NAF/APE (with trailing letter), NACE, ISIC (4-digit no dot).
    Examples:
        "46.90Z" → "46.90"   (NAF/APE)
        "4690"   → "46.90"   (ISIC / no-dot)
        "46.90"  → "46.90"   (already OK)
        "46"     → "46"      (division level)
        "G"      → "G"       (section level)
    """
    if not raw:
        return None
    code = str(raw).strip().upper().replace(" ", "").replace("-", ".")
    # Strip trailing NAF letter: "46.90Z" → "46.90"
    code = re.sub(r"[A-Z]$", "", code)
    # Remove trailing dot
    code = code.rstrip(".")
    # "4690" → "46.90"
    if "." not in code and len(code) == 4 and code.isdigit():
        code = code[:2] + "." + code[2:]
    # "469" → "46.9" (group, 3 digits)
    if "." not in code and len(code) == 3 and code.isdigit():
        code = code[:2] + "." + code[2:]
    return code or None


def code_to_section(nace_code: str | None) -> str | None:
    """Return section letter from a NACE code."""
    if not nace_code:
        return None
    code = normalize_code(nace_code) or ""
    division = code.replace(".", "")[:2]
    return DIVISION_TO_SECTION.get(division)


def code_to_sector_label(nace_code: str | None) -> str | None:
    """Return human-readable sector label from section."""
    section = code_to_section(nace_code)
    return SECTIONS.get(section) if section else None


def sic_to_sector_label(sic_code: str | None) -> str | None:
    """Convert UK SIC 2007 code to sector label."""
    if not sic_code:
        return None
    division = str(sic_code).strip()[:2].zfill(2)
    section = SIC_TO_NACE_SECTION.get(division)
    return SECTIONS.get(section) if section else None


# ── Code level detection ──────────────────────────────────────────────────────

def _detect_level(code: str) -> str:
    """Return 'section' | 'division' | 'group' | 'class' | 'unknown'."""
    c = code.strip()
    if len(c) == 1 and c.isalpha():
        return "section"
    if len(c) == 2 and c.isdigit():
        return "division"
    if re.match(r"^\d{2}\.\d$", c):
        return "group"
    if re.match(r"^\d{2}\.\d{2}$", c):
        return "class"
    return "unknown"


# ── Hierarchy expansion ───────────────────────────────────────────────────────

def get_codes_for_section(section: str) -> list[str]:
    """All NACE codes (any level) belonging to a section."""
    divs = SECTION_TO_DIVISIONS.get(section.upper(), [])
    result = []
    for div in divs:
        result.append(div)
        for code in NACE_LABELS:
            if (isinstance(code, str) and len(code) > 2
                    and code.replace(".", "").startswith(div)):
                result.append(code)
    return list(dict.fromkeys(result))


def get_codes_for_division(division: str) -> list[str]:
    """All NACE codes belonging to a 2-digit division."""
    div = division.zfill(2)
    return [c for c in NACE_LABELS if (
        isinstance(c, str) and
        c.replace(".", "").startswith(div) and
        c != div and len(c) > 1 and c[0].isdigit()
    )]


def get_codes_for_group(group: str) -> list[str]:
    """All NACE classes belonging to a group (e.g. '46.4')."""
    prefix = group.replace(".", "")[:3]
    return [c for c in NACE_LABELS if (
        isinstance(c, str) and
        c.replace(".", "").startswith(prefix) and
        len(c.replace(".", "")) > 3
    )]


def expand_nace_code(code: str, depth: str = "close") -> list[str]:
    """Return NACE class codes matching the given code at the requested depth.

    depth='exact'  → only the exact code (and NAF variants)
    depth='close'  → same group (3-digit prefix)
    depth='large'  → same division (2-digit prefix) or section
    """
    norm = normalize_code(code)
    if not norm:
        return []

    level = _detect_level(norm)

    if depth == "exact":
        return [norm] if norm in NACE_LABELS else [norm]

    if depth == "close":
        if level == "class":
            group = norm[:4]  # "46.47" → "46.4"
            return get_codes_for_group(group) or [norm]
        if level == "group":
            return get_codes_for_group(norm) or [norm]
        if level == "division":
            return get_codes_for_division(norm)
        if level == "section":
            return get_codes_for_section(norm)

    if depth == "large":
        if level in ("class", "group"):
            division = norm.replace(".", "")[:2]
            return get_codes_for_division(division)
        if level == "division":
            section = DIVISION_TO_SECTION.get(norm.zfill(2))
            return get_codes_for_section(section) if section else get_codes_for_division(norm)
        if level == "section":
            return get_codes_for_section(norm)

    return [norm]


# ── Dataclass résultat ────────────────────────────────────────────────────────

@dataclass
class NaceMatch:
    code: str           # NACE code normalisé
    label: str          # Libellé français
    score: int          # 0-100
    match_type: str     # "exact" | "equivalent" | "group" | "division" | "section" | "text"
    level: str          # "class" | "group" | "division" | "section"


# ── Recherche principale ──────────────────────────────────────────────────────

@lru_cache(maxsize=512)
def search_nace(query: str, mode: str = "broad", depth: str = "close") -> list[NaceMatch]:
    """Cherche des codes NACE correspondant à une saisie libre ou un code.

    Args:
        query: code (ex: "46.47", "4647", "46.47Z", "G") ou texte libre (ex: "informatique")
        mode:  "strict"  = code exact uniquement
               "broad"   = code + hiérarchie selon depth
               "smart"   = broad + recherche textuelle
        depth: "exact"   = code strictement identique
               "close"   = même groupe (3 chiffres)
               "large"   = même division (2 chiffres) ou section

    Returns:
        list[NaceMatch] triée par score décroissant
    """
    q = query.strip()
    if not q:
        return []

    results: dict[str, NaceMatch] = {}

    # ── 1. Essai en tant que code ─────────────────────────────────────────────
    norm = normalize_code(q)
    if not norm:
        # Essai ISIC 4 chiffres sans point : "4647"
        isic_try = ISIC4_EXCEPTIONS.get(q.zfill(4))
        if isic_try:
            norm = isic_try
    if not norm and re.match(r"^[A-U]$", q.upper()):
        norm = q.upper()  # section letter

    if norm:
        level = _detect_level(norm)
        label = NACE_LABELS.get(norm, "")

        if mode == "strict" or depth == "exact":
            if label:
                results[norm] = NaceMatch(norm, label, 100, "exact", level)
        else:
            # Code exact
            if label:
                results[norm] = NaceMatch(norm, label, 100, "exact", level)

            # Expansion hiérarchique
            expanded = expand_nace_code(norm, depth)
            for exp_code in expanded:
                if exp_code == norm:
                    continue
                exp_label = NACE_LABELS.get(exp_code, "")
                if not exp_label:
                    continue
                exp_level = _detect_level(exp_code)
                exp_depth = len(exp_code.replace(".", ""))
                orig_depth = len(norm.replace(".", ""))
                # Score based on proximity
                if exp_depth == orig_depth:
                    score, mtype = 90, "equivalent"
                elif exp_depth == orig_depth - 1 or exp_depth == orig_depth + 1:
                    score, mtype = 75, "group"
                elif exp_depth == 2:
                    score, mtype = 55, "division"
                else:
                    score, mtype = 40, "section"
                if exp_code not in results or results[exp_code].score < score:
                    results[exp_code] = NaceMatch(exp_code, exp_label, score, mtype, exp_level)

    # ── 2. Recherche textuelle (mode smart ou si aucun code trouvé) ────────────
    if mode == "smart" or (mode == "broad" and not results):
        text_matches = _text_search(q, depth)
        for m in text_matches:
            if m.code not in results or results[m.code].score < m.score:
                results[m.code] = m

    # ── 3. Tri et retour ──────────────────────────────────────────────────────
    return sorted(results.values(), key=lambda x: -x.score)


def _text_search(query: str, depth: str = "close") -> list[NaceMatch]:
    """Fuzzy text search on NACE labels."""
    try:
        from rapidfuzz import fuzz, process
        has_rapidfuzz = True
    except ImportError:
        has_rapidfuzz = False

    q = query.lower()
    matches: dict[str, NaceMatch] = {}

    # Build search corpus: only classes and divisions (not groups to avoid noise)
    corpus = {
        code: label.lower()
        for code, label in NACE_LABELS.items()
        if _detect_level(code) in ("class", "division", "section")
    }

    if has_rapidfuzz:
        # Use rapidfuzz for fast fuzzy matching
        hits = process.extract(q, corpus, scorer=fuzz.partial_ratio, limit=20)
        for code, score, _ in hits:
            if score < 40:
                continue
            label = NACE_LABELS.get(code, "")
            level = _detect_level(code)
            # Boost score if query appears verbatim in label
            if q in label.lower():
                score = min(100, score + 20)
            norm_score = int(score * 0.85)  # cap text matches below exact code matches
            matches[code] = NaceMatch(code, label, norm_score, "text", level)
    else:
        # Fallback: simple substring matching
        for code, label_lower in corpus.items():
            if q in label_lower:
                label = NACE_LABELS.get(code, "")
                level = _detect_level(code)
                # Score based on how well the query fills the label
                score = min(80, int(len(q) / max(len(label_lower), 1) * 100) + 40)
                matches[code] = NaceMatch(code, label, score, "text", level)

    # If depth != exact, expand matching classes to their groups/divisions
    if depth != "exact":
        expanded: dict[str, NaceMatch] = {}
        for m in list(matches.values()):
            if _detect_level(m.code) == "class":
                for exp in expand_nace_code(m.code, depth):
                    if exp not in matches and exp not in expanded:
                        exp_label = NACE_LABELS.get(exp, "")
                        if exp_label:
                            expanded[exp] = NaceMatch(
                                exp, exp_label,
                                max(30, m.score - 15),
                                "text", _detect_level(exp)
                            )
        matches.update(expanded)

    return list(matches.values())


def get_nace_suggestions(partial: str, limit: int = 10) -> list[dict]:
    """Autocomplete: return suggestions for partial code or text input."""
    if not partial or len(partial) < 2:
        return []
    p = partial.strip().lower()
    results = []

    # Code prefix match first
    for code, label in NACE_LABELS.items():
        if isinstance(code, str) and code.lower().startswith(p):
            results.append({
                "code": code,
                "label": label,
                "match": "code",
                "level": _detect_level(code),
            })
            if len(results) >= limit:
                return results

    # Text match
    for code, label in NACE_LABELS.items():
        if p in label.lower() and code not in [r["code"] for r in results]:
            results.append({
                "code": code,
                "label": label,
                "match": "text",
                "level": _detect_level(code),
            })
            if len(results) >= limit:
                break

    return results[:limit]
