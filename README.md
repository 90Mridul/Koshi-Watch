# Koshi Watch

Koshi Watch is a local web app for exploring satellite imagery, river-channel change, rainfall, and modeled river flow across the Koshi basin in Nepal and India. It brings those records together on an interactive map so users can compare places and dates and inspect how the river corridor has changed.

## Start the app

Requirements: Python 3.10 or later and an internet connection for data that is not already cached.

```powershell
Set-Location -LiteralPath 'path\to\koshi watch translated'
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open the local address printed in the terminal (usually http://127.0.0.1:5000). To use a different port:

```powershell
$env:PORT = '5001'
python app.py
```

## Deploy to Vercel

This repository is prepared for Vercel's Flask runtime. `app.py` exports the Flask application, `requirements.txt` declares its Python dependencies, and `.python-version` selects Python 3.12. Static files are mirrored into `public/` for Vercel's CDN while the Flask development server continues to serve the copy in `dist/`.

For a GitHub deployment:

1. Create a GitHub repository and push this project, including `dist/` and `public/`. Do not commit `data/`, which contains local databases and downloaded imagery.
2. Import the repository at [vercel.com/new](https://vercel.com/new) and leave the framework build and output settings on automatic detection. Vercel detects the Flask app from `app.py` and its dependencies.
3. Deploy a preview, check the Observatory and API routes, then promote it to production from Vercel.

For a CLI preview deployment, install the Vercel CLI and run `vercel` from the project root. Use `vercel --prod` when you are ready to publish.

Vercel Functions use temporary writable storage. This app places its SQLite database and downloaded imagery cache in `/tmp/koshi-watch` on Vercel, so saved analyses and warmed cache files are not durable across cold starts or instances. For persistent production records, configure an external database and object/cache storage before relying on saved analyses. No credentials are required for the current public data sources.

The browser interface is included in `dist/`; Node.js and a separate frontend build are not needed to run the Python app.

## What you can explore

- **Observatory:** Select a study region, browse optical or radar layers, choose dates, compare two acquisitions, and estimate water-covered area from an optical scene. The SAR date slider lists actual archive acquisition dates; a date without a pass remains selectable and is identified as having no scene.
- **Rivers & Wetlands:** Review annual satellite images and image-derived river pathways, compare a target year with a baseline, and inspect long-term water occurrence and channel transitions.
- **Flood & Rain:** View modeled river discharge and compare historical catchment rainfall across years.
- **Data Sources & Methodology:** Review the datasets, calculation, and limits behind the displayed information.

## Reading the map and measurements

The small **i** buttons beside scientific terms open short explanations. In brief:

- **NDWI** compares green and near-infrared reflectance. Water often has a higher index value than dry land, but shadows, wet soil, vegetation, clouds, and mixed shoreline pixels can affect the result. It is an image-based indicator, not a direct measurement of water depth.
- **Water area** estimates the share of valid image pixels inside the selected boundary that exceed the NDWI threshold, then scales that share by the boundary area. Treat it as an estimate for that image date, not a surveyed flood extent.
- **River pathways** are extracted from satellite water-index pixels. They are useful for comparing broad channel patterns, but they are not surveyed channel banks or navigable river centerlines.
- **NISAR and Sentinel-1 SAR** use radar and can provide imagery when optical scenes are obscured by clouds. The NISAR view in this app is a georeferenced archive browse image for visual inspection; it does not classify flooded pixels.
- **GloFAS flow** is a model estimate, not a direct gauge reading. **ERA5 rainfall** is a gridded historical weather estimate, not a local rain-gauge measurement.
- **JRC water occurrence** describes how often water was identified in the historical Landsat record. It is not a current water-level layer.

Clouds, acquisition timing, sensor resolution, region boundaries, and thresholds all affect what can be seen and measured. Compare like dates and layers when possible, and use the Methodology page before interpreting a number as a field measurement.

## Data and attribution

The app queries or displays data from these providers:

- [Copernicus Sentinel-2](https://dataspace.copernicus.eu/data-collections/copernicus-sentinel-missions/sentinel-2) and [USGS Landsat Collection 2](https://www.usgs.gov/landsat-missions/landsat-collection-2-surface-reflectance) optical imagery, accessed through the [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/docs/).
- [NASA-ISRO NISAR and Sentinel-1 archive imagery](https://asf.alaska.edu/notices/nisar-l-band-data-now-publicly-available/) discovered through the Alaska Satellite Facility (ASF).
- [NASA GIBS](https://nasa-gibs.github.io/gibs-api-docs/) MODIS imagery.
- [EC JRC Global Surface Water](https://developers.google.com/earth-engine/datasets/catalog/JRC_GSW1_4_Metadata) historical occurrence and transition products.
- [Copernicus GloFAS](https://global-flood.emergency.copernicus.eu/general-information/about-glofas/) river-flow estimates and [ECMWF ERA5](https://confluence.ecmwf.int/pages/viewpage.action?pageId=669811810) rainfall reanalysis.
- Country-border linework from [Natural Earth](https://www.naturalearthdata.com/downloads/10m-cultural-vectors/10m-admin-0-boundary-lines/).

Please retain provider attribution when reusing imagery or results, and consult each provider's terms for publication or redistribution.

## Caching and local files

The app stores its SQLite database and downloaded map or image cache under `data/`. The Rivers & Wetlands page starts a background image warm-up: it visits years from newest to oldest, caching true-color imagery first and NDWI imagery second. The warm-up does not block the page. Its current phase and progress are available at `/api/rivers/cache-status`. Cached imagery can make later visits faster; uncached requests still need an internet connection.

Keep `data/` if you want to retain local records and cached files. It is runtime data and is not required to install the app.

## Project files

- `app.py` - Flask server, data endpoints, image rendering, and cache management.
- `processing.py` - river-channel and rainfall processing functions.
- `dist/` - browser files served by Flask.
- `public/` - static assets used by the local app.
- `vercel.json` and `.python-version` - Vercel runtime configuration.
- `requirements.txt` - Python dependencies.
- `data/` - local database and caches created or updated while the app runs.

## Data limitations

Satellite passes are intermittent, and cloud-free scenes may not exist for a requested day. Optical water estimates can be affected by cloud and shadow contamination, vegetation, mixed pixels, and the selected threshold. Radar browse images provide visual context but are not a flood classification. Modeled discharge and reanalysis rainfall should be interpreted as estimates and checked against local observations when decisions require measured conditions.

## License

This package does not include a project license. The satellite, hydrology, boundary, and basemap data remain subject to their respective providers' terms; see the links above before redistributing data or derived products.
