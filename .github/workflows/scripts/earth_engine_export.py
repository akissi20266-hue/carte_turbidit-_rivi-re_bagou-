"""
Automatisation du calcul et de l'export du NDTI (turbidité)
pour le suivi du bassin de la Bagoué.

Produit exactement ce qu'attend la carte :
  - un PNG géoréférencé classé en 5 niveaux de turbidité, avec la même palette
    de couleurs que la légende (data/TURBIDITE_<SAISON>_<ANNEE>.png)
  - une entrée ajoutée à data/turbidite_manifest.json (lu dynamiquement par index.html)

Aucune modification du HTML n'est nécessaire pour ajouter une nouvelle année :
la carte lit le manifeste et affiche automatiquement toute nouvelle entrée.

À ADAPTER avant utilisation (cherche les commentaires "TODO") :
  - la géométrie exacte du bassin (asset EE, ou fichier GeoJSON local)
  - les seuils de classification du NDTI (les 4 valeurs qui séparent les 5 classes
    de la légende : très faible / faible / modérée / élevée / très élevée)
"""

import datetime
import json
import os
import sys

import ee

# Bornes exactes déjà utilisées par toutes les couches existantes (data/*.png)
# -> nécessaire pour que la nouvelle image s'aligne parfaitement sur les précédentes.
IMAGE_BOUNDS = [[10.209588873396282, -6.493252182341659],
                 [10.851556932016974, -6.055363531729509]]

# Palette identique à la légende affichée dans le panneau latéral de la carte
LEGEND_PALETTE = ["2ec4e6", "ffe066", "ffb84d", "ff5c33", "1a1a1a"]

# Seuils de classification NDTI (bornes entre les 5 classes de la légende) :
#   Très faible : -0.10 à -0.04
#   Faible      : -0.04 à  0.02
#   Moyenne     :  0.02 à  0.08
#   Élevée      :  0.08 à  0.15
#   Très élevée :  0.15 à  0.20
NDTI_BREAKS = [-0.04, 0.02, 0.08, 0.15]  # 4 seuils => 5 classes

MANIFEST_PATH = "data/turbidite_manifest.json"


def init_earth_engine():
    """Authentification via compte de service (utilisable sans login interactif)."""
    service_account = os.environ["GEE_SERVICE_ACCOUNT_EMAIL"]
    key_file = os.environ.get("GEE_KEY_FILE", "gee_key.json")
    project_id = os.environ.get("GEE_PROJECT_ID", "modern-ally-486918-k3")
    credentials = ee.ServiceAccountCredentials(service_account, key_file)
    ee.Initialize(credentials, project=project_id)


def mask_s2_clouds(image):
    qa = image.select("QA60")
    cloud_bit_mask = 1 << 10
    cirrus_bit_mask = 1 << 11
    mask = qa.bitwiseAnd(cloud_bit_mask).eq(0).And(qa.bitwiseAnd(cirrus_bit_mask).eq(0))
    return image.updateMask(mask).divide(10000).copyProperties(image, ["system:time_start"])


def compute_ndti(image, basin_geometry):
    ndti = image.normalizedDifference(["B3", "B4"]).rename("NDTI")  # (B3-B4)/(B3+B4)
    mndwi = image.normalizedDifference(["B3", "B11"]).rename("MNDWI")
    water_mask = mndwi.gt(0)
    return ndti.updateMask(water_mask).clip(basin_geometry)


def get_period_composite(basin_geometry, start, end, cloud_pct=20):
    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(basin_geometry)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloud_pct))
        .map(mask_s2_clouds)
    )
    count = collection.size().getInfo()
    if count == 0:
        raise RuntimeError(f"Aucune image Sentinel-2 utilisable entre {start} et {end}.")
    print(f"{count} images Sentinel-2 utilisées pour {start} → {end}")
    return compute_ndti(collection.median(), basin_geometry)


def classify_and_colorize(ndti_image):
    """Reclasse le NDTI continu en 5 classes (0-4) puis applique la palette légende."""
    classified = ee.Image(0)
    for i, threshold in enumerate(NDTI_BREAKS, start=1):
        classified = classified.where(ndti_image.gt(threshold), i)
    classified = classified.updateMask(ndti_image.mask())
    return classified.visualize(min=0, max=len(LEGEND_PALETTE) - 1, palette=LEGEND_PALETTE)


def current_season(today: datetime.date):
    """TODO: ajuste ces bornes à la saisonnalité réelle du bassin de la Bagoué."""
    month = today.month
    year = today.year
    if month in (11, 12, 1, 2, 3):
        season = "seche"
        start_year = year if month >= 11 else year - 1
        start, end = f"{start_year}-11-01", f"{start_year + 1}-03-31"
    else:
        season = "pluie"
        start, end = f"{year}-04-01", f"{year}-10-31"
    return season, start, end


def export_png(rgb_image, out_path, region):
    """Télécharge une miniature RGBA géoréférencée en PNG (synchrone, pas de passage par Drive)."""
    url = rgb_image.getThumbURL({
        "region": region,
        "dimensions": 1024,
        "format": "png",
    })
    import urllib.request
    urllib.request.urlretrieve(url, out_path)
    print(f"Couche exportée : {out_path}")


def update_manifest(season, year, filename):
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH) as f:
            manifest = json.load(f)
    else:
        manifest = []

    manifest = [e for e in manifest if not (e["season"] == season and e["year"] == year)]
    manifest.append({"season": season, "year": year, "file": filename})
    manifest.sort(key=lambda e: (e["season"], e["year"]))

    os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"Manifeste mis à jour : {season} {year} -> {filename}")


def main():
    init_earth_engine()

    # Géométrie du bassin de la Bagoué (extraite de data/BASSIN_1.js, format QGIS2web)
    with open("data/bassin_bagoue.geojson") as f:
        geojson = json.load(f)
    basin = ee.Geometry(geojson["features"][0]["geometry"])

    today = datetime.date.today()

    # Permet de forcer une période précise en argument, utile pour rattraper
    # la saison des pluies 2026 :
    #   python earth_engine_export.py 2026-04-01 2026-10-31 pluie 2026
    if len(sys.argv) >= 5:
        start, end, season, year = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    else:
        season, start, end = current_season(today)
        year = int(start[:4]) if season == "pluie" else int(end[:4])

    ndti = get_period_composite(basin, start, end)
    rgb = classify_and_colorize(ndti)

    filename = f"TURBIDITE_{season.upper()}_{year}.png"
    out_path = f"data/{filename}"
    export_png(rgb, out_path, region=basin)
    update_manifest(season, year, filename)


if __name__ == "__main__":
    main()
