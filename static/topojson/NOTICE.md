# Plotly's U.S. base map, served by this app

`usa_110m.json` is Plotly's own U.S. base map (state shapes, coastlines, lakes), the file
Plotly.js would otherwise download from `cdn.plot.ly` every time a map is drawn. It is served
from this folder (Streamlit static serving, `.streamlit/config.toml`) and the viewer's maps set
Plotly's `topojsonURL` to `app/static/topojson/`, so a map draws even when a phone, network or
content blocker cannot reach the CDN, and visitors' browsers make no third-party request for it.
Plotly.js draws nothing on a geo map, not even the packaged zone polygons, until this file has
loaded.

- File: `usa_110m.json`, unchanged, 68,514 bytes,
  sha256 `fc83e70129aa4911c0ab6e42faadd854e4ec1da71a095785c294e999e34c4d58`.
- Retrieved 2026-10-19 from https://cdn.plot.ly/un/usa_110m.json (Plotly.js's default
  `topojsonURL`); byte-identical to `dist/topojson/usa_110m.json` in the plotly.js 3.1.1 npm
  package (https://cdn.jsdelivr.net/npm/plotly.js@3.1.1/dist/topojson/usa_110m.json).
- Made by plotly.js's `topojson/` build: state and province shapes from Natural Earth
  `admin_1_states_provinces_lakes` (public domain), lakes and rivers from Natural Earth, and
  coastlines, countries, land and ocean from UN geodata
  (https://geoportal.un.org/). The boundaries and names shown do not imply official endorsement
  or acceptance by the United Nations.
- License: distributed with plotly.js under the MIT License:

> MIT License
>
> Copyright (c) 2016-2024 Plotly Technologies Inc.
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software
> and associated documentation files (the "Software"), to deal in the Software without
> restriction, including without limitation the rights to use, copy, modify, merge, publish,
> distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
> Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or
> substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
> INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
> PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR
> ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
> ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

To refresh it (only when Streamlit ships a Plotly.js that expects a different file):
`curl -o static/topojson/usa_110m.json https://cdn.plot.ly/un/usa_110m.json`, then update the
size and checksum here (`tests/test_config.py` checks them).
