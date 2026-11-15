# Koshi Watch

**An environmental observatory for the Sapta Koshi River basin.** Koshi Watch brings satellite imagery, river change, rainfall, and modeled flow into one place to help people explore how water moves across eastern Nepal and northern Bihar.

The observatory is designed for visual comparison and scientific context. Its maps and indicators support exploration; they are not a substitute for field measurements, official warnings, or operational flood forecasts.

## Explore

### Observatory

Compare dated satellite observations over four study areas: Koshi Tappu, Koshi Barrage, the Kusaha breach corridor, and the Chatara gorge. The map combines optical and radar imagery with historical water layers, river pathways, boundaries, and landmarks. Where imagery permits, an NDWI-based estimate summarizes water area inside the selected region.

### Rivers & Wetlands

Inspect annual imagery, compare river courses across years, and explore long-term surface-water occurrence and channel transitions across the Koshi floodplain.

### Flood & Rain

Review modeled river discharge and historical rainfall comparisons for the Koshi catchment.

### Data & Methodology

Find the sources, spatial and temporal coverage, calculations, and limitations associated with the maps and measurements.

## Imagery and scale

The available detail depends on the selected sensor and product. Sentinel-2 optical imagery is provided at 10 m resolution; Landsat surface-reflectance imagery at 30 m; and NASA MODIS imagery at approximately 250 m. Radar products have their own acquisition dates and product resolutions. A map zoom level does not increase the underlying image resolution.

Historical requests use the archive available for that period. Landsat provides the longer optical record, while Sentinel-2 supplies finer detail for its coverage period. Satellite observations are intermittent, so an exact requested date may have no acquisition for the selected region or sensor.

## Interpreting the results

- **Water-area estimate:** The NDWI method identifies image pixels above a selected threshold inside a study boundary and scales the valid-pixel share to the boundary area. It is an image-derived estimate for a particular acquisition, not a surveyed flood outline or water-depth measurement.
- **River pathways:** Image-derived pathways help compare broad channel patterns. They are not surveyed banks, bathymetry, or navigable river centerlines.
- **Radar imagery:** SAR can provide visual context when clouds limit optical observations. Archive browse imagery is for visual inspection and is not, by itself, a flood classification.
- **Discharge and rainfall:** GloFAS discharge is modeled flow, not a direct gauge reading. ERA5 rainfall is a gridded reanalysis, not a local rain-gauge observation.
- **Historical water occurrence:** JRC occurrence layers summarize water detections across their observation record; they do not show current water level.

Clouds, shadows, vegetation, wet soil, mixed shoreline pixels, acquisition timing, sensor resolution, study boundaries, and analysis thresholds can all affect results. Compare observations with similar sensors and conditions, and consult the methodology before interpreting differences as real-world change.

## Data sources and attribution

- [Copernicus Sentinel-2](https://dataspace.copernicus.eu/data-collections/copernicus-sentinel-missions/sentinel-2) and [USGS Landsat Collection 2](https://www.usgs.gov/landsat-missions/landsat-collection-2-surface-reflectance), accessed through the [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/docs/).
- [NASA-ISRO NISAR](https://nisar.jpl.nasa.gov/) and [Sentinel-1](https://dataspace.copernicus.eu/data-collections/copernicus-sentinel-missions/sentinel-1), with archive discovery through the [Alaska Satellite Facility](https://asf.alaska.edu/).
- [NASA GIBS](https://nasa-gibs.github.io/gibs-api-docs/) imagery, including MODIS products.
- [EC JRC Global Surface Water](https://developers.google.com/earth-engine/datasets/catalog/JRC_GSW1_4_Metadata) occurrence and transition products.
- [Copernicus GloFAS](https://global-flood.emergency.copernicus.eu/general-information/about-glofas/) discharge and [ECMWF ERA5](https://confluence.ecmwf.int/pages/viewpage.action?pageId=669811810) rainfall reanalysis.
- Administrative boundary linework from [Natural Earth](https://www.naturalearthdata.com/downloads/10m-cultural-vectors/10m-admin-0-boundary-lines/).

Source attribution should be retained when imagery or results are reused. Each provider's terms apply to its data and products.

## Project architecture

The server is a Flask application that serves the browser interface and provides API endpoints for imagery, hydrology, and image processing. The client is distributed as browser-ready assets; no separate frontend build is required for the deployed application. Satellite and map products are fetched from their upstream providers and rendered for the selected study area.

- `app.py` — Flask routes, upstream data access, image rendering, and runtime caching.
- `processing.py` — channel and rainfall processing utilities.
- `dist/` — browser application and its release assets.
- `public/` — static asset set for the hosting platform.
- `requirements.txt` — Python runtime dependencies.

## Limitations

Satellite passes are intermittent, and cloud-free optical observations may not exist for a requested date. Optical indices can be affected by cloud and shadow contamination, vegetation, mixed pixels, and threshold selection. Radar browse images provide visual context but do not classify flood extent. Modeled discharge and reanalysis rainfall should be treated as estimates and checked against local observations when decisions require measured conditions.

## License

This repository does not currently include a project-level license. Data and imagery remain subject to their respective provider terms. Review those terms before redistributing source data or derived products.
