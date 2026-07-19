# Operations map asset

`world-map.png` is a 1280 px raster rendering of the CC0
`BlankMap-Equirectangular.svg` map from Wikimedia Commons. The source map uses
Natural Earth data and is distributed under the CC0 1.0 public-domain
dedication:

- https://commons.wikimedia.org/wiki/File:BlankMap-Equirectangular.svg
- https://creativecommons.org/publicdomain/zero/1.0/

The equirectangular projection is intentional: the dashboard can place node
telemetry using a direct longitude/latitude to x/y conversion without shipping
a GIS runtime or loading map tiles.
