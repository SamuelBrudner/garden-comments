# The zone layer, served to browsers by this app

`phzm_2023_simplified.json` is a byte-identical copy of the packaged zone layer
`src/crop_explorer/data/overlays/phzm_2023_simplified.geojson`. The viewer's zone overlay names
its URL (`app/static/overlays/phzm_2023_simplified.json`, Streamlit static serving, see
`.streamlit/config.toml`) instead of embedding the 0.9 MB GeoJSON in every figure, so browsers
download it once and cache it. The app checks at start-up that this copy equals the packaged
file (`crop_explorer.overlays.check_served_copy`) and shows an error instead of the overlay if
it does not; `scripts/build_overlays.py` writes both.

Simplified from the 2023 USDA Plant Hardiness Zone Map GIS data (PRISM Climate Group, Oregon State University). This is not the official USDA Plant Hardiness Zone Map — see planthardiness.ars.usda.gov.

Sources, the PRISM/OSU terms (verbatim), every alteration and the citation:
`src/crop_explorer/data/overlays/NOTICE.md` and `sources.yaml` next to it. No USDA-ARS or OSU
logo is used anywhere (the data are altered, so the terms require the logos to be removed).
